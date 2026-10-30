"""The reconciler (F8): recovery of everything a
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

Steps:
  1a/1b  recover expired CLAIMED / RUNNING leases   (R4a R4b R5 / A3 R9 R10 R11)
  2      recover expired RUNNING evaluations         (E9a E9b), and A1: FINISHED agent
         attempts that never got an evaluation row are recorded (its own lock key)
  3      retries whose backoff elapsed              (R12)
  4      redispatch lost QUEUED runs; reclaim idle pending stream entries
         (both streams)
  6      dispatch due PENDING evaluations on the evaluations stream
  7      mark silent workers lost; delete dead consumers
  5      sweep orphaned artifact files (its own timer; ADR-008)
  8      sweep orphaned containers
"""

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING

from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agenteval_core.artifacts import ArtifactStore
from agenteval_core.logging import get_logger
from agenteval_core.observability import metrics as prom
from agenteval_core.observability import tracing
from agenteval_core.queue.streams import RunQueue
from agenteval_core.repositories.evaluations import EvaluationRepository
from agenteval_core.repositories.recovery import (
    STEP_ARTIFACTS,
    STEP_CLAIMED,
    STEP_EVAL_A1,
    STEP_EVAL_DISPATCH,
    STEP_EVAL_RECOVERY,
    STEP_REDISPATCH,
    STEP_RETRIES_DUE,
    STEP_RUNNING,
    STEP_WORKERS,
    RecoveryRepository,
)
from agenteval_core.repositories.runs import RunRepository

if TYPE_CHECKING:
    from agenteval_worker.evaluation_runner import Recorded

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
        artifact_store: ArtifactStore | None = None,
        artifact_sweep_grace_s: float = 120.0,
        wall_clock: Callable[[], float] = time.time,
        evaluation_queue: RunQueue | None = None,
        record_attempt: "Callable[[uuid.UUID], Awaitable[Recorded | None]] | None" = None,
        a1_grace_s: float = 60.0,
    ) -> None:
        self._sm = sessionmaker
        self._queue = queue
        # Step 7. The evaluations stream, and the worker's own `record()` (the
        # reconciler runs inside the worker, so it is injected, not imported).
        self._evaluation_queue = evaluation_queue
        self._queues = [queue] if evaluation_queue is None else [queue, evaluation_queue]
        self._record_attempt = record_attempt
        self._a1_grace_s = a1_grace_s
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
        self._last_artifact_sweep = clock()
        self._artifacts = artifact_store
        self._artifact_grace_s = artifact_sweep_grace_s
        self._wall_clock = wall_clock  # file mtimes are wall-clock seconds
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
        stamp their runs before step 4 looks for unstamped ones, and step 2
        (which makes evaluations PENDING with a backoff) runs before step 6
        (which publishes them once due)."""
        for name, step in (
            ("recover_claimed", self.step_recover_claimed),
            ("recover_running", self.step_recover_running),
            ("recover_evaluations", self.step_recover_evaluations),
            ("record_missing_evaluations", self.step_record_missing_evaluations),
            ("retries_due", self.step_retries_due),
            ("redispatch", self.step_redispatch),
            ("dispatch_evaluations", self.step_dispatch_evaluations),
            ("reclaim_pending", self.step_reclaim_pending),
            ("workers", self.step_workers),
            ("orphan_sweep", self.step_orphan_sweep),
            ("artifact_sweep", self.step_artifact_sweep),
        ):
            try:
                # Its own root span, so a recovery is one trace, whatever run it
                # touches. A step that found nothing is marked idle and not exported.
                with tracing.span(f"reconciler.{name}", root=True, reconciler_step=name) as sp:
                    done = await step()
                    handled = _handled(done)
                    sp.set(count=handled, idle=handled == 0)
            except Exception:
                prom.reconciler_step_error(name)
                log.exception("reconciler_step_failed", step=name)

    # -- step 1a ----------------------------------------------------------------------------

    async def step_recover_claimed(self) -> list[uuid.UUID]:
        """Returns the run ids published (R4a)."""
        async with self._sm() as s, s.begin():
            repo = RecoveryRepository(s)
            if not await repo.begin_step(STEP_CLAIMED):
                return []
            recovery = await repo.recover_expired_claims()
        for recovered in recovery.recovered:
            prom.run_recovered(recovered.code, recovered.cancel_reason)
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
        for recovered in recovery.recovered:
            prom.run_recovered(recovered.code, recovered.cancel_reason)
        # Single host: the container (if any) carries the attempt id as a label.
        # Best effort - the owner's own self-stop usually got there first.
        for attempt_id in recovery.attempt_ids:
            try:
                await asyncio.to_thread(self._kill, str(attempt_id))
            except Exception:
                log.exception("recovery_kill_failed", attempt_id=str(attempt_id))
        return recovery.attempt_ids

    # -- step 2 -----------------------------------------------------------------------------

    async def step_recover_evaluations(self) -> list[uuid.UUID]:
        """E9a / E9b: RUNNING evaluations whose lease expired go back to PENDING
        with a backoff (published by step 6 once due) or to ERROR. Returns the
        attempt ids whose containers were killed after commit."""
        async with self._sm() as s, s.begin():
            repo = RecoveryRepository(s)
            if not await repo.begin_step(STEP_EVAL_RECOVERY):
                return []
            recovery = await repo.recover_expired_evaluations()
        for recovered in recovery.recovered:
            prom.evaluation_transition(recovered.code)
        for attempt_id in recovery.attempt_ids:
            try:
                await asyncio.to_thread(self._kill, str(attempt_id))
            except Exception:
                log.exception("recovery_kill_failed", attempt_id=str(attempt_id))
        return recovery.attempt_ids

    async def step_record_missing_evaluations(self) -> list[uuid.UUID]:
        """A1 recovery (its own advisory key, 21: the scan only reads): FINISHED
        agent attempts older than `a1_grace_s` with no evaluation row - the
        worker died between the finish commit and the evaluation insert. Each is
        recorded by the worker's own `record()` (the same E1/E2/E3 decision as the
        fast path, ON CONFLICT DO NOTHING, so a race leaves exactly one row), and
        a PENDING one is published and stamped. Returns the evaluation ids
        created."""
        if self._record_attempt is None:
            return []
        async with self._sm() as s, s.begin():
            repo = RecoveryRepository(s)
            if not await repo.begin_step(STEP_EVAL_A1):
                return []
            attempts = await repo.attempts_missing_evaluation(self._a1_grace_s)
        created: list[uuid.UUID] = []
        pending: list[uuid.UUID] = []
        for attempt_id in attempts:
            try:
                recorded = await self._record_attempt(attempt_id)
            except Exception:
                log.exception("a1_record_failed", attempt_id=str(attempt_id))
                continue
            if recorded is None:
                continue
            created.append(recorded.evaluation_id)
            log.info(
                "evaluation_recovered",
                attempt_id=str(attempt_id),
                evaluation_id=str(recorded.evaluation_id),
                transition="A1",
                status=recorded.status,
            )
            if recorded.status == "PENDING":
                pending.append(recorded.evaluation_id)
        await self._publish_evaluations(pending)
        return created

    # -- step 3 -----------------------------------------------------------------------------

    async def step_retries_due(self) -> list[uuid.UUID]:
        async with self._sm() as s, s.begin():
            repo = RecoveryRepository(s)
            if not await repo.begin_step(STEP_RETRIES_DUE):
                return []
            released = await repo.release_due_retries()
        for _ in released:
            prom.run_recovered("R12")
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

    # -- step 6 -----------------------------------------------------------------------------

    async def step_dispatch_evaluations(self) -> list[uuid.UUID]:
        """Publish due PENDING evaluations (never published, lost in transit, or
        a retry whose backoff elapsed) to the evaluations stream, then stamp
        `dispatched_at`. The fast path does not publish: it grades inline, and
        the age threshold keeps this from racing it."""
        if self._evaluation_queue is None:
            return []
        async with self._sm() as s, s.begin():
            repo = RecoveryRepository(s)
            if not await repo.begin_step(STEP_EVAL_DISPATCH):
                return []
            candidates = await repo.evaluation_dispatch_candidates(self._redispatch_after_s)
        return await self._publish_evaluations(candidates)

    async def step_reclaim_pending(self) -> int:
        """XAUTOCLAIM entries idle for `redispatch_after_s` to this reconciler's
        consumer and ack every one: Postgres is the truth, and a run still
        QUEUED (or an evaluation still PENDING) is covered by redispatch. The
        ids XAUTOCLAIM reports as deleted are already gone from the PEL and are
        never acked (G8). Covers both streams. Returns the number acked."""
        return sum([await self._reclaim(queue) for queue in self._queues])

    async def _reclaim(self, queue: RunQueue) -> int:
        cursor = "0-0"
        acked = 0
        for _ in range(RECLAIM_MAX_PAGES):
            claimed = await queue.autoclaim(
                self.consumer,
                int(self._redispatch_after_s * 1000),
                start_id=cursor,
                count=RECLAIM_PAGE,
            )
            for msg in claimed.messages:
                await queue.ack(msg.message_id)
                acked += 1
            if claimed.deleted_ids:
                log.info("pending_entries_deleted", ids=claimed.deleted_ids, stream=queue.stream)
            cursor = claimed.next_cursor
            if cursor == "0-0":
                break
        if acked:
            log.info("pending_reclaimed", acked=acked, consumer=self.consumer, stream=queue.stream)
        return acked

    # -- step 7 -----------------------------------------------------------------------------

    async def step_workers(self) -> tuple[list[str], list[str]]:
        """Mark silent workers lost, then delete consumers nobody will read
        again. Returns (workers marked lost, consumers deleted)."""
        silence_s = LOST_AFTER_TTLS * self._ttl_s
        # Redis reads, kept out of the transaction so the DB timeouts never wait on
        # them. A consumer exists per stream (a worker reads both); one with pending
        # entries on a stream is kept on that stream only.
        candidates: dict[str, list[str]] = {}  # stream -> deletable-looking consumers
        for queue in self._queues:
            candidates[queue.stream] = [
                c.name
                for c in await queue.consumers()
                if c.pending == 0
                and c.idle_ms > silence_s * 1000
                and c.name not in (self._worker_id, self.consumer)
            ]
        names = sorted({name for found in candidates.values() for name in found})
        async with self._sm() as s, s.begin():
            repo = RecoveryRepository(s)
            if not await repo.begin_step(STEP_WORKERS):
                return [], []
            lost = await repo.mark_workers_lost(silence_s)
            dead = await repo.stopped_or_unknown([_worker_of(name) for name in names])
        for worker_id in lost:
            log.warning("worker_lost", worker_id=worker_id)
        # After commit. A live worker's consumer (and its reconciler consumer)
        # is never deleted: only stopped or unregistered owners qualify.
        deleted: list[str] = []
        for queue in self._queues:
            for name in candidates[queue.stream]:
                if _worker_of(name) not in dead:
                    continue
                await queue.delete_consumer(name)
                log.info("consumer_deleted", consumer=name, stream=queue.stream)
                if name not in deleted:
                    deleted.append(name)
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

    # -- step 5 -----------------------------------------------------------------------------

    async def step_artifact_sweep(self, *, force: bool = False) -> int | None:
        """ADR-008 E: delete artifact files no row references. Runs at most once
        per `orphan_sweep_interval_s` (its own timer); returns the files
        removed, or None when not due, no store is configured, or another
        reconciler holds the step.

        Order matters: list the volume FIRST, then read which of the old files
        are referenced, and only after that commit delete. A file is a
        candidate only if its mtime is older than the grace period, which
        exceeds the writers' deadline plus statement timeout, so a save that
        is still about to insert its row never loses its file. A referenced
        file is never deleted, however old; `.tmp` files are never referenced.
        """
        if self._artifacts is None:
            return None
        now = self._clock()
        if not force and now - self._last_artifact_sweep < self._sweep_interval_s:
            return None
        self._last_artifact_sweep = now
        listed = await self._artifacts.list_files()
        cutoff = self._wall_clock() - self._artifact_grace_s
        candidates = [key for key, mtime in listed if mtime < cutoff]
        if not candidates:
            return 0
        async with self._sm() as s, s.begin():
            repo = RecoveryRepository(s)
            if not await repo.begin_step(STEP_ARTIFACTS):
                return None
            referenced = await repo.referenced_storage_keys(candidates)
        removed = 0
        for key in candidates:
            if key in referenced:
                continue
            try:
                await self._artifacts.delete(key)
            except Exception:
                log.exception("artifact_sweep_delete_failed", key=key)
                continue
            removed += 1
        if removed:
            log.info("artifacts_swept", removed=removed)
        return removed

    # -- helpers ----------------------------------------------------------------------------

    async def _publish_evaluations(self, evaluation_ids: Sequence[uuid.UUID]) -> list[uuid.UUID]:
        """After commit: XADD each evaluation id, then stamp `dispatched_at` on
        those that made it (guarded by PENDING). A failed publish leaves the
        stamp as it was, so the dispatch step retries it."""
        queue = self._evaluation_queue
        if queue is None:
            return []
        published: list[uuid.UUID] = []
        for evaluation_id in evaluation_ids:
            try:
                await queue.publish(evaluation_id)
            except RedisError as exc:
                log.error(
                    "reconciler_publish_failed", evaluation_id=str(evaluation_id), error=str(exc)
                )
                continue
            published.append(evaluation_id)
        if published:
            async with self._sm() as s, s.begin():
                await EvaluationRepository(s).mark_dispatched(published)
        return published

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


def _handled(result: object) -> int:
    """How much a step did, from what it returned: a list of ids, a pair of
    lists (step 9), a count, or nothing (the step was skipped or idle)."""
    if isinstance(result, list):
        return len(result)
    if isinstance(result, tuple):
        return sum(len(part) for part in result)
    return result if isinstance(result, int) else 0


def _worker_of(consumer: str) -> str:
    """A consumer is named after its worker; the reconciler's has a suffix."""
    return consumer.removesuffix(RECONCILER_CONSUMER_SUFFIX)
