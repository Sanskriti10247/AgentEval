import json
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agenteval_core.evaluation.spec import validate_against_task
from agenteval_core.evaluation.taxonomy import MAX_EVALUATION_SPEC_BYTES
from agenteval_core.models import Task
from agenteval_core.repositories.datasets import DatasetRepository
from agenteval_core.repositories.tasks import TaskRepository
from agenteval_core.schemas.task_api import TaskCreate
from agenteval_core.services.errors import Conflict, InvalidEvaluation, InvalidTask, NotFound


def spec_size(spec: dict[str, object]) -> int:
    """Bytes of the spec as stored. Postgres renders jsonb::text with ', '
    and ': ' separators and raw UTF-8 - the same as json.dumps with
    ensure_ascii=False - so this matches the ck_tasks_evaluation_size check
    (key order differs, length doesn't)."""
    return len(json.dumps(spec, ensure_ascii=False).encode())


class TaskService:
    def __init__(self, session: AsyncSession, image_allowlist: list[str]) -> None:
        self._s = session
        self._tasks = TaskRepository(session)
        self._allowlist = frozenset(image_allowlist)

    async def create(self, data: TaskCreate) -> Task:
        """Every evaluation check happens here, before anything is written,
        and every error message is fixed text (D11): spec content never
        appears in a response."""
        if data.image not in self._allowlist:
            raise InvalidTask(f"image {data.image!r} is not on the allowlist")
        spec = None
        if data.evaluation is not None:
            try:
                validate_against_task(data.evaluation, data.files)
            except ValueError:
                raise InvalidEvaluation("protected_paths must name task files") from None
            spec = data.evaluation.model_dump(mode="json")
            if spec_size(spec) > MAX_EVALUATION_SPEC_BYTES:
                raise InvalidEvaluation(
                    f"evaluation spec exceeds {MAX_EVALUATION_SPEC_BYTES} bytes"
                )
        if data.dataset_id is not None and not await DatasetRepository(self._s).lock_draft(
            data.dataset_id
        ):
            # Held FOR SHARE until commit, so a concurrent freeze waits for us.
            if await DatasetRepository(self._s).get(data.dataset_id) is None:
                raise NotFound(f"dataset {data.dataset_id} not found")
            raise Conflict(f"dataset {data.dataset_id} is frozen; tasks can't be added")
        try:
            task = await self._tasks.add(
                Task(**data.model_dump(exclude={"evaluation"}), evaluation=spec)
            )
            await self._s.commit()
        except IntegrityError:
            # A database CHECK caught what validation didn't; never echo it.
            await self._s.rollback()
            raise InvalidTask("task violates a database constraint") from None
        return task

    async def get(self, task_id: uuid.UUID) -> Task:
        task = await self._tasks.get(task_id)
        if task is None:
            raise NotFound(f"task {task_id} not found")
        return task
