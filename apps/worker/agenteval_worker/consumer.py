"""Consume loop: read one message only when a concurrency slot is free, so a
worker never holds messages it can't start yet.

Step 7: a worker consumes two streams, runs and evaluations, with ONE loop and
ONE semaphore. A single blocking XREADGROUP names both streams, so a message on
either wakes the loop at once (two loops sharing the semaphore would each park a
slot inside its own blocking read, delaying the other stream by up to block_ms
when concurrency is 1) and the concurrency limit covers runs and gradings
alike. The price: Redis applies COUNT per stream, so one read can return a
message from each stream; the extra one waits for its own slot (a bounded
over-read of one, which stays pending - and is reclaimed - if shutdown comes
first)."""

import asyncio

from redis.exceptions import RedisError

from agenteval_core.logging import get_logger
from agenteval_core.queue.streams import DispatchMessage, RunQueue, read_any
from agenteval_worker.executor import Executor

log = get_logger(__name__)


class Consumer:
    def __init__(
        self,
        queue: RunQueue,
        executor: Executor,
        *,
        concurrency: int,
        block_ms: int,
        drain_timeout_s: float,
        cancel_grace_s: float = 20.0,
        evaluation_queue: RunQueue | None = None,
    ) -> None:
        self._queue = queue
        self._evaluation_queue = evaluation_queue
        self._queues = [queue] if evaluation_queue is None else [queue, evaluation_queue]
        self._executor = executor
        self._sem = asyncio.Semaphore(concurrency)
        self._block_ms = block_ms
        self._drain_timeout_s = drain_timeout_s
        self._cancel_grace_s = min(cancel_grace_s, drain_timeout_s)
        self._inflight: set[asyncio.Task[None]] = set()

    async def run(self, stop: asyncio.Event) -> None:
        for queue in self._queues:
            await queue.ensure_group()
        consumer = self._executor.worker_id
        backoff = 0.5
        while not stop.is_set():
            if not await self._acquire_slot(stop):
                break
            try:
                messages = await self._read(consumer)
                backoff = 0.5
            except RedisError as exc:
                self._sem.release()
                log.error("queue_read_failed", error=str(exc), retry_in_s=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10)
                continue
            if not messages:
                self._sem.release()
                continue
            self._spawn(*messages[0])
            for extra in messages[1:]:
                # Already read, so it needs a slot of its own before it starts.
                if not await self._acquire_slot(stop):
                    break  # shutdown: left pending, reclaimed and redispatched later
                self._spawn(*extra)
        await self._drain()

    async def _read(self, consumer: str) -> list[tuple[RunQueue, DispatchMessage]]:
        if self._evaluation_queue is None:  # runs only: a plain read of the one stream
            got = await self._queue.read(consumer, count=1, block_ms=self._block_ms)
            return [(self._queue, m) for m in got]
        return await read_any(self._queues, consumer, count=1, block_ms=self._block_ms)

    def _spawn(self, queue: RunQueue, msg: DispatchMessage) -> None:
        task = asyncio.create_task(self._handle(queue, msg))
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    async def _acquire_slot(self, stop: asyncio.Event) -> bool:
        """Wait for a free concurrency slot, unless stop is set first.

        With every slot busy (exactly the situation at shutdown), a plain
        `await sem.acquire()` would block until some run finished, so the
        drain - and its deadline - would not start at SIGTERM.
        """
        acquire = asyncio.ensure_future(self._sem.acquire())
        stopped = asyncio.ensure_future(stop.wait())
        try:
            await asyncio.wait({acquire, stopped}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopped.cancel()
        if stop.is_set():
            if acquire.done() and not acquire.cancelled():
                self._sem.release()  # won the race too; give the slot back
            else:
                acquire.cancel()
            return False
        return True

    async def _handle(self, queue: RunQueue, msg: DispatchMessage) -> None:
        try:
            if queue is self._evaluation_queue:
                await self._executor.handle_evaluation(msg)
            else:
                await self._executor.handle(msg)
        except Exception:
            # Leave the message unacked; Phase 5 recovery picks it up.
            log.exception("handle_failed", message_id=msg.message_id)
        finally:
            self._sem.release()

    async def _drain(self) -> None:
        """Let in-flight runs finish. The total wait is still drain_timeout_s:

        1. wait until drain_timeout - cancel_grace for runs to finish normally
        2. cancel in-flight agent runs (reason worker_shutdown); they record
           run_finished and persist within cancel_grace
        3. hard-cancel whatever is left (as in S1)
        """
        if not self._inflight:
            return
        log.info("draining", inflight=len(self._inflight))
        soft = self._drain_timeout_s - self._cancel_grace_s
        _, pending = await asyncio.wait(self._inflight, timeout=soft)
        if pending:
            cancelled = self._executor.cancel_all("worker_shutdown")
            log.warning("drain_cancelling", inflight=len(pending), agent_runs=cancelled)
            _, pending = await asyncio.wait(pending, timeout=self._cancel_grace_s)
        if pending:
            log.warning("drain_timeout", abandoned=len(pending))
            for t in pending:
                t.cancel()
