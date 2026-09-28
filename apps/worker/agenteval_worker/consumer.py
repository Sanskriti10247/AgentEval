"""Consume loop: read one message only when a concurrency slot is free, so a
worker never holds messages it can't start yet."""

import asyncio

from redis.exceptions import RedisError

from agenteval_core.logging import get_logger
from agenteval_core.queue.streams import DispatchMessage, RunQueue
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
    ) -> None:
        self._queue = queue
        self._executor = executor
        self._sem = asyncio.Semaphore(concurrency)
        self._block_ms = block_ms
        self._drain_timeout_s = drain_timeout_s
        self._inflight: set[asyncio.Task[None]] = set()

    async def run(self, stop: asyncio.Event) -> None:
        await self._queue.ensure_group()
        consumer = self._executor.worker_id
        backoff = 0.5
        while not stop.is_set():
            await self._sem.acquire()
            if stop.is_set():
                self._sem.release()
                break
            try:
                messages = await self._queue.read(consumer, count=1, block_ms=self._block_ms)
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
            task = asyncio.create_task(self._handle(messages[0]))
            self._inflight.add(task)
            task.add_done_callback(self._inflight.discard)
        await self._drain()

    async def _handle(self, msg: DispatchMessage) -> None:
        try:
            await self._executor.handle(msg)
        except Exception:
            # Leave the message unacked; Phase 5 recovery picks it up.
            log.exception("handle_failed", message_id=msg.message_id)
        finally:
            self._sem.release()

    async def _drain(self) -> None:
        if not self._inflight:
            return
        log.info("draining", inflight=len(self._inflight))
        _, pending = await asyncio.wait(self._inflight, timeout=self._drain_timeout_s)
        if pending:
            log.warning("drain_timeout", abandoned=len(pending))
            for t in pending:
                t.cancel()
