import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from agenteval_core.models import Task


class TaskRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, task: Task) -> Task:
        self._s.add(task)
        await self._s.flush()
        return task

    async def get(self, task_id: uuid.UUID) -> Task | None:
        return await self._s.get(Task, task_id)
