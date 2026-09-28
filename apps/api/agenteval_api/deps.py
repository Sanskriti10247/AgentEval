"""Process-wide resources live on app.state (created in the lifespan);
request-scoped sessions and services are FastAPI dependencies."""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from agenteval_core.config import Settings
from agenteval_core.queue.streams import RunQueue
from agenteval_core.services.runs import RunService
from agenteval_core.services.tasks import TaskService


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:
        yield session


def get_queue(request: Request) -> RunQueue:
    return request.app.state.queue


SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
QueueDep = Annotated[RunQueue, Depends(get_queue)]


def get_task_service(session: SessionDep, settings: SettingsDep) -> TaskService:
    return TaskService(session, settings.image_allowlist)


def get_run_service(session: SessionDep, queue: QueueDep) -> RunService:
    return RunService(session, queue)


TaskServiceDep = Annotated[TaskService, Depends(get_task_service)]
RunServiceDep = Annotated[RunService, Depends(get_run_service)]
