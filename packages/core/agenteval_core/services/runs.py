import uuid
from dataclasses import dataclass

from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from agenteval_core.logging import get_logger
from agenteval_core.models import Run, RunAttempt
from agenteval_core.queue.streams import RunQueue
from agenteval_core.repositories.runs import RunRepository
from agenteval_core.repositories.tasks import TaskRepository
from agenteval_core.services.errors import DispatchUnavailable, IdempotencyConflict, NotFound
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

    async def submit(self, task_id: uuid.UUID, idempotency_key: str | None) -> SubmitResult:
        """Persist a QUEUED run, commit, then XADD.

        Order matters: commit first so Postgres (the truth) always knows about
        a run before any worker can see its message. If XADD then fails, the
        run stays QUEUED and a client retry with the same Idempotency-Key
        re-dispatches it. Duplicate messages are harmless (conditional claim).
        """
        if await self._tasks.get(task_id) is None:
            raise NotFound(f"task {task_id} not found")

        run = await self._runs.create_queued(task_id, idempotency_key)
        created = run is not None
        if run is None:
            assert idempotency_key is not None  # conflict is only possible with a key
            run = await self._runs.get_by_idempotency_key(idempotency_key)
            assert run is not None
            if run.task_id != task_id:
                raise IdempotencyConflict(
                    f"Idempotency-Key {idempotency_key!r} was already used for task {run.task_id}"
                )
        await self._s.commit()

        # A new run always needs dispatch; a replayed one only if it never got claimed.
        if created or run.status == RunStatus.QUEUED:
            try:
                await self._queue.publish(run.id)
            except RedisError as exc:
                log.error("dispatch_failed", run_id=str(run.id), error=str(exc))
                raise DispatchUnavailable(run.id) from exc
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
