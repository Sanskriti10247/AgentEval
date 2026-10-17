import uuid
from dataclasses import dataclass

from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from agenteval_core.logging import get_logger
from agenteval_core.models import Run, RunAttempt, TrajectoryEvent
from agenteval_core.queue.streams import RunQueue
from agenteval_core.repositories.agents import AgentVersionRepository
from agenteval_core.repositories.runs import RunRepository
from agenteval_core.repositories.tasks import TaskRepository
from agenteval_core.repositories.trajectory import TrajectoryRepository
from agenteval_core.services.dispatch import stamp_dispatched
from agenteval_core.services.errors import (
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


class RunService:
    def __init__(self, session: AsyncSession, queue: RunQueue) -> None:
        self._s = session
        self._runs = RunRepository(session)
        self._tasks = TaskRepository(session)
        self._queue = queue

    async def submit(
        self,
        task_id: uuid.UUID,
        idempotency_key: str | None,
        agent_version_id: uuid.UUID | None = None,
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
            if await AgentVersionRepository(self._s).get(agent_version_id) is None:
                raise NotFound(f"agent version {agent_version_id} not found")

        run = await self._runs.create_queued(task_id, idempotency_key, agent_version_id)
        created = run is not None
        if run is None:
            assert idempotency_key is not None  # conflict is only possible with a key
            run = await self._runs.get_by_idempotency_key(idempotency_key)
            assert run is not None
            if (run.task_id, run.agent_version_id) != (task_id, agent_version_id):
                raise IdempotencyConflict(
                    f"Idempotency-Key {idempotency_key!r} was already used for a different "
                    f"request (task {run.task_id}, agent version {run.agent_version_id})"
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
