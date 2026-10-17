"""The reconciler (F8; PROGRESS.md section 17): recovery of everything a
crashed or stalled worker, or a lost message, left behind.

It runs as an asyncio task in every worker, ticking every `reconcile_interval_s`.
Each Postgres step is its own short transaction that first takes a
transaction-level advisory lock (`RecoveryRepository.begin_step`); a second
reconciler that cannot get the lock skips the step this tick. Postgres is the
truth, so all decisions come from `reliability.policy` and every write is a
guarded UPDATE (recovery.py).

External side effects - XADD, container kills, XACK, DELCONSUMER - happen
strictly after the transaction commits, so a rolled-back step publishes and
kills nothing. Each step is wrapped separately: an exception is logged and the
loop carries on with the remaining steps and the next tick.

Steps (the numbering follows PROGRESS.md section 17):
  1a/1b  recover expired CLAIMED / RUNNING leases   (R4a R4b R5 / A3 R9 R10 R11)
  3      retries whose backoff elapsed              (R12)
  4      redispatch lost QUEUED runs; reclaim idle pending stream entries
  7      mark silent workers lost; delete dead consumers
  8      sweep orphaned containers
"""

import asyncio
import time
import uuid
from collections.abc import Callable, Sequence

from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agenteval_core.logging import get_logger
from agenteval_core.queue.streams import RunQueue
from agenteval_core.repositories.recovery import (
    STEP_CLAIMED,
    STEP_REDISPATCH,
    STEP_RETRIES_DUE,
    STEP_RUNNING,
    STEP_WORKERS,
    RecoveryRepository,
)
from agenteval_core.repositories.runs import RunRepository

log = get_logger(__name__)

RECONCILER_CONSUMER_SUFFIX = "-reconciler"
LOST_AFTER_TTLS = 3  # a worker is lost, a consumer dead, after 3 x lease ttl of silence
RECLAIM_PAGE = 100
RECLAIM_MAX_PAGES = 20  # bounds one tick; the cursor restarts at 0-0 next tick

SM = async_sessionmaker[AsyncSession]


class Reconciler:
    def __init__(
        self,
        sessionmaker: SM,
        queue: RunQueue,
        worker_id: str,
        *,
        interval_s: float,
        lease_ttl_s: float,
        redispatch_after_s: float,
        orphan_sweep_interval_s: float,
        kill_attempt: Callable[[str], int],
        sweep_orphans: Callable[[], int],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sm = sessionmaker
        self._queue = queue
        self._worker_id = worker_id
        self._interval_s = interval_s
        self._ttl_s = lease_ttl_s
        self._redispatch_after_s = redispatch_after_s
        self._sweep_interval_s = orphan_sweep_interval_s
        self._kill = kill_attempt
        self._sweep = sweep_orphans
        self._clock = clock
        # main() sweeps once at startup, so the first scheduled sweep is one interval away.
        self._last_sweep = clock()
        self.consumer = f"{worker_id}{RECONCILER_CONSUMER_SUFFIX}"

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.tick()
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._interval_s)
            except TimeoutError:
                pass

    async def tick(self) -> None:
        """One pass over every step. Order matters: step 1 and 3 publish and
        stamp their runs before step 4 looks for unstamped ones."""
        for name, step in (
            ("recover_claimed", self.step_recover_claimed),
            ("recover_running", self.step_recover_running),
            ("retries_due", self.step_retries_due),
            ("redispatch", self.step_redispatch),
            ("reclaim_pending", self.step_reclaim_pending),
            ("workers", self.step_workers),
            ("orphan_sweep", self.step_orphan_sweep),
        ):
            try:
                await step()
            except Exception:
                log.exception("reconciler_step_failed", step=name)

    # -- step 1a ----------------------------------------------------------------------------

    async def step_recover_claimed(self) -> list[uuid.UUID]:
        """Returns the run ids published (R4a)."""
        async with self._sm() as s, s.begin():
            repo = RecoveryRepository(s)
            if not await repo.begin_step(STEP_CLAIMED):
                return []
            recovery = await repo.recover_expired_claims()
        await self._publish_and_stamp(recovery.to_publish)
        return recovery.to_publish

    # -- step 1b ----------------------------------------------------------------------------

    async def step_recover_running(self) -> list[uuid.UUID]:
        """Returns the attempt ids whose containers were killed."""
        async with self._sm() as s, s.begin():
            repo = RecoveryRepository(s)
            if not await repo.begin_step(STEP_RUNNING):
                return []
            recovery = await repo.recover_expired_running()
        # Single host: the container (if any) carries the attempt id as a label.
        # Best effort - the owner's own self-stop usually got there first.
        for attempt_id in recovery.attempt_ids:
            try:
                await asyncio.to_thread(self._kill, str(attempt_id))
            except Exception:
                log.exception("recovery_kill_failed", attempt_id=str(attempt_id))
        return recovery.attempt_ids

    # -- step 3 -----------------------------------------------------------------------------

    async def step_retries_due(self) -> list[uuid.UUID]:
        async with self._sm() as s, s.begin():
            repo = RecoveryRepository(s)
            if not await repo.begin_step(STEP_RETRIES_DUE):
                return []
            released = await repo.release_due_retries()
        await self._publish_and_stamp(released)
        return released

    # -- step 4 -----------------------------------------------------------------------------

    async def step_redispatch(self) -> list[uuid.UUID]:
        async with self._sm() as s, s.begin():
            repo = RecoveryRepository(s)
            if not await repo.begin_step(STEP_REDISPATCH):
                return []
            candidates = await repo.redispatch_candidates(self._redispatch_after_s)
        return await self._publish_and_stamp(candidates)

    async def step_reclaim_pending(self) -> int:
        """XAUTOCLAIM entries idle for `redispatch_after_s` to this reconciler's
        consumer and ack every one: Postgres is the truth, and a run still
        QUEUED is covered by redispatch. The ids XAUTOCLAIM reports as deleted
        are already gone from the PEL and are never acked (G8). Returns the
        number acked."""
        cursor = "0-0"
        acked = 0
        for _ in range(RECLAIM_MAX_PAGES):
            claimed = await self._queue.autoclaim(
                self.consumer,
                int(self._redispatch_after_s * 1000),
                start_id=cursor,
                count=RECLAIM_PAGE,
            )
            for msg in claimed.messages:
                await self._queue.ack(msg.message_id)
                acked += 1
            if claimed.deleted_ids:
                log.info("pending_entries_deleted", ids=claimed.deleted_ids)
            cursor = claimed.next_cursor
            if cursor == "0-0":
                break
        if acked:
            log.info("pending_reclaimed", acked=acked, consumer=self.consumer)
        return acked

    # -- step 7 -----------------------------------------------------------------------------

    async def step_workers(self) -> tuple[list[str], list[str]]:
        """Mark silent workers lost, then delete consumers nobody will read
        again. Returns (workers marked lost, consumers deleted)."""
        silence_s = LOST_AFTER_TTLS * self._ttl_s
        # A Redis read, kept out of the transaction so the DB timeouts never wait on it.
        candidates = [
            c.name
            for c in await self._queue.consumers()
            if c.pending == 0
            and c.idle_ms > silence_s * 1000
            and c.name not in (self._worker_id, self.consumer)
        ]
        async with self._sm() as s, s.begin():
            repo = RecoveryRepository(s)
            if not await repo.begin_step(STEP_WORKERS):
                return [], []
            lost = await repo.mark_workers_lost(silence_s)
            dead = await repo.stopped_or_unknown([_worker_of(name) for name in candidates])
        for worker_id in lost:
            log.warning("worker_lost", worker_id=worker_id)
        # After commit. A live worker's consumer (and its reconciler consumer)
        # is never deleted: only stopped or unregistered owners qualify.
        deleted = [name for name in candidates if _worker_of(name) in dead]
        for name in deleted:
            await self._queue.delete_consumer(name)
            log.info("consumer_deleted", consumer=name)
        return lost, deleted

    # -- step 8 -----------------------------------------------------------------------------

    async def step_orphan_sweep(self, *, force: bool = False) -> int | None:
        """Deadline-label based, so it is safe across workers. Runs at most once
        per `orphan_sweep_interval_s`; returns the containers removed, or None
        when not yet due."""
        now = self._clock()
        if not force and now - self._last_sweep < self._sweep_interval_s:
            return None
        self._last_sweep = now
        removed = await asyncio.to_thread(self._sweep)
        if removed:
            log.info("orphans_swept", removed=removed)
        return removed

    # -- helpers ----------------------------------------------------------------------------

    async def _publish_and_stamp(self, run_ids: Sequence[uuid.UUID]) -> list[uuid.UUID]:
        """After commit: XADD each run, then stamp `dispatched_at` on those that
        made it (guarded by QUEUED). A failed publish leaves the stamp NULL, so
        redispatch retries it; nothing here may abort the tick."""
        published: list[uuid.UUID] = []
        for run_id in run_ids:
            try:
                await self._queue.publish(run_id)
            except RedisError as exc:
                log.error("reconciler_publish_failed", run_id=str(run_id), error=str(exc))
                continue
            published.append(run_id)
        if published:
            async with self._sm() as s, s.begin():
                await RunRepository(s).mark_dispatched(published)
        return published


def _worker_of(consumer: str) -> str:
    """A consumer is named after its worker; the reconciler's has a suffix."""
    return consumer.removesuffix(RECONCILER_CONSUMER_SUFFIX)
