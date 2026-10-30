import uuid
from dataclasses import dataclass
from datetime import datetime

from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from agenteval_core import pagination
from agenteval_core.config import Settings
from agenteval_core.logging import get_logger
from agenteval_core.models import Run, RunAttempt, TrajectoryEvent
from agenteval_core.queue.streams import RunQueue
from agenteval_core.repositories.agents import AgentVersionRepository
from agenteval_core.repositories.runs import (
    BudgetExceeded,
    CancelDisposition,
    ReplayDisposition,
    RunRepository,
)
from agenteval_core.repositories.tasks import TaskRepository
from agenteval_core.repositories.trajectory import TrajectoryRepository
from agenteval_core.services.dispatch import stamp_dispatched
from agenteval_core.services.errors import (
    AlreadyFinished,
    Conflict,
    DispatchUnavailable,
    IdempotencyConflict,
    InvalidRun,
    NotFound,
)
from agenteval_core.states import RunStatus

log = get_logger(__name__)


@dataclass(frozen=True)
class SubmitResult:
    run: Run
    created: bool


@dataclass(frozen=True)
class CancelResult:
    run: Run
    accepted: bool  # True: this call changed the run (202); False: idempotent repeat (200)


@dataclass(frozen=True)
class ReplayResult:
    run: Run
    accepted: bool  # True: this call replayed the run (202); False: same key repeated (200)


def decode_cursor(after: str) -> tuple[datetime, uuid.UUID]:
    try:
        return pagination.decode_cursor(after)
    except ValueError:
        raise InvalidRun("invalid runs cursor") from None


class RunService:
    def __init__(self, session: AsyncSession, queue: RunQueue, settings: Settings) -> None:
        self._s = session
        self._settings = settings
        self._runs = RunRepository(session)
        self._tasks = TaskRepository(session)
        self._queue = queue

    async def submit(
        self,
        task_id: uuid.UUID,
        idempotency_key: str | None,
        agent_version_id: uuid.UUID | None = None,
        max_attempts: int | None = None,
    ) -> SubmitResult:
        """Persist a QUEUED run, commit, then XADD.

        Order matters: commit first so Postgres (the truth) always knows about
        a run before any worker can see its message. If XADD then fails, the
        run stays QUEUED and a client retry with the same Idempotency-Key
        re-dispatches it. Duplicate messages are harmless (conditional claim).
        """
        task = await self._tasks.get(task_id)
        if task is None:
            raise NotFound(f"task {task_id} not found")
        # Pairing rule (spans two tables, so enforced here, not by a CHECK).
        if task.task_type == "agent" and agent_version_id is None:
            raise InvalidRun("agent tasks need an agent_version_id")
        if task.task_type == "command" and agent_version_id is not None:
            raise InvalidRun("command tasks don't take an agent_version_id")
        if agent_version_id is not None:
            version = await AgentVersionRepository(self._s).get(agent_version_id)
            if version is None:
                raise NotFound(f"agent version {agent_version_id} not found")
            if version.model_provider not in self._settings.enabled_providers():
                raise InvalidRun(
                    f"Provider '{version.model_provider}' is not enabled on this server"
                )

        budget = max_attempts if max_attempts is not None else self._settings.run_max_attempts
        run = await self._runs.create_queued(task_id, idempotency_key, agent_version_id, budget)
        created = run is not None
        if run is None:
            assert idempotency_key is not None  # conflict is only possible with a key
            run = await self._runs.get_by_idempotency_key(idempotency_key)
            assert run is not None
            if (run.task_id, run.agent_version_id, run.max_attempts) != (
                task_id,
                agent_version_id,
                budget,
            ):
                raise IdempotencyConflict(
                    f"Idempotency-Key {idempotency_key!r} was already used for a different "
                    f"request (task {run.task_id}, agent version {run.agent_version_id}, "
                    f"max_attempts {run.max_attempts})"
                )
        await self._s.commit()

        # A new run always needs dispatch; a replayed one only if it never got claimed.
        if created or run.status == RunStatus.QUEUED:
            try:
                await self._queue.publish(run.id)
            except RedisError as exc:
                log.error("dispatch_failed", run_id=str(run.id), error=str(exc))
                raise DispatchUnavailable(run.id) from exc
            await stamp_dispatched(self._s, [run.id])
            log.info("run_dispatched", run_id=str(run.id), task_id=str(task_id), replay=not created)
        return SubmitResult(run=run, created=created)

    async def get(self, run_id: uuid.UUID) -> Run:
        run = await self._runs.get(run_id)
        if run is None:
            raise NotFound(f"run {run_id} not found")
        return run

    async def cancel(self, run_id: uuid.UUID) -> CancelResult:
        """Request cancellation of one run (F5, F15; R2, R13, flag for
        CLAIMED/RUNNING). One short guarded UPDATE on `runs` and a commit; the
        response is the run as the winning state left it. A terminal run
        (COMPLETED/FAILED) is a 409 with nothing written (G7)."""
        disposition = await self._runs.request_cancel(run_id)
        if disposition is CancelDisposition.NOT_FOUND:
            await self._s.rollback()
            raise NotFound(f"run {run_id} not found")
        if disposition is CancelDisposition.FINISHED:
            await self._s.rollback()
            raise AlreadyFinished(f"run {run_id} already finished; it can no longer be cancelled")
        await self._s.commit()
        run = await self.get(run_id)
        accepted = disposition is not CancelDisposition.ALREADY
        if accepted:
            log.info("run_cancel_requested", run_id=str(run_id), status=run.status)
        return CancelResult(run=run, accepted=accepted)

    async def replay(
        self, run_id: uuid.UUID, additional_attempts: int, idempotency_key: str | None
    ) -> ReplayResult:
        """R14 (F10, G4): give a FAILED run more attempts and queue it again.

        One transaction holds the guarded FAILED -> QUEUED update and the
        run_replays row; the stream publish and the dispatched_at stamp come
        after the commit, like submit. If the publish fails the run is QUEUED
        with no stamp, so a retry with the same key (200) publishes it, and the
        reconciler would redispatch it anyway. Errors: 404 unknown, 409 not
        FAILED or key reused, 422 over the cap of 10 attempts."""
        try:
            disposition = await self._runs.replay(run_id, additional_attempts, idempotency_key)
        except BudgetExceeded as exc:
            await self._s.rollback()
            raise InvalidRun(str(exc)) from None
        if disposition is ReplayDisposition.NOT_FOUND:
            await self._s.rollback()
            raise NotFound(f"run {run_id} not found")
        if disposition is ReplayDisposition.KEY_REUSED:
            await self._s.rollback()
            raise IdempotencyConflict(
                f"Idempotency-Key {idempotency_key!r} was already used for a different replay"
            )
        if disposition is ReplayDisposition.NOT_FAILED:
            await self._s.rollback()
            raise Conflict(f"run {run_id} is not FAILED; only a FAILED run can be replayed")
        await self._s.commit()
        run = await self.get(run_id)
        accepted = disposition is ReplayDisposition.REPLAYED
        # A replayed key whose first call died before publishing: QUEUED, never stamped.
        if run.status == RunStatus.QUEUED and (accepted or run.dispatched_at is None):
            try:
                await self._queue.publish(run.id)
            except RedisError as exc:
                log.error("dispatch_failed", run_id=str(run.id), error=str(exc))
                raise DispatchUnavailable(run.id) from exc
            await stamp_dispatched(self._s, [run.id])
            log.info("run_replayed", run_id=str(run_id), replay_count=run.replay_count)
        return ReplayResult(run=run, accepted=accepted)

    async def list_runs(
        self,
        *,
        status: RunStatus | None,
        dead_letter: bool | None,
        evaluation_id: uuid.UUID | None,
        task_id: uuid.UUID | None,
        after: str | None,
        limit: int,
    ) -> tuple[list[Run], str | None]:
        """One page of runs, newest first, and the cursor for the next page (None
        on the last). Fetches one extra row to know whether another page exists."""
        cursor = decode_cursor(after) if after else None
        rows = await self._runs.list_page(
            status=status,
            dead_letter=dead_letter,
            evaluation_id=evaluation_id,
            task_id=task_id,
            after=cursor,
            limit=limit + 1,
        )
        page = rows[:limit]
        more = len(rows) > limit
        return page, pagination.encode_cursor(page[-1].created_at, page[-1].id) if more else None

    async def list_attempts(self, run_id: uuid.UUID) -> list[RunAttempt]:
        await self.get(run_id)
        return await self._runs.list_attempts(run_id)

    async def list_events(
        self, run_id: uuid.UUID, attempt_no: int, *, after_seq: int, limit: int
    ) -> tuple[list[TrajectoryEvent], int | None]:
        """One page of an attempt's trajectory, in seq order. Returns the
        events and the cursor for the next page (None on the last page)."""
        attempt = next(
            (a for a in await self.list_attempts(run_id) if a.attempt_no == attempt_no), None
        )
        if attempt is None:
            raise NotFound(f"run {run_id} has no attempt {attempt_no}")
        # Fetch one extra row to know whether another page exists.
        rows = await TrajectoryRepository(self._s).list_for_attempt(
            attempt.id, after_seq=after_seq, limit=limit + 1
        )
        page = rows[:limit]
        return page, (page[-1].seq if len(rows) > limit else None)
