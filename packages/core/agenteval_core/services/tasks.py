import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from agenteval_core.models import Task
from agenteval_core.repositories.tasks import TaskRepository
from agenteval_core.schemas.task import TaskCreate
from agenteval_core.services.errors import InvalidTask, NotFound


class TaskService:
    def __init__(self, session: AsyncSession, image_allowlist: list[str]) -> None:
        self._s = session
        self._tasks = TaskRepository(session)
        self._allowlist = frozenset(image_allowlist)

    async def create(self, data: TaskCreate) -> Task:
        if data.image not in self._allowlist:
            raise InvalidTask(f"image {data.image!r} is not on the allowlist")
        task = await self._tasks.add(Task(**data.model_dump()))
        await self._s.commit()
        return task

    async def get(self, task_id: uuid.UUID) -> Task:
        task = await self._tasks.get(task_id)
        if task is None:
            raise NotFound(f"task {task_id} not found")
        return task
