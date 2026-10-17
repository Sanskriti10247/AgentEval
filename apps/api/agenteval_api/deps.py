"""Process-wide resources live on app.state (created in the lifespan);
request-scoped sessions and services are FastAPI dependencies."""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from agenteval_core.config import Settings
from agenteval_core.queue.streams import RunQueue
from agenteval_core.services.agents import AgentService
from agenteval_core.services.datasets import DatasetService
from agenteval_core.services.evaluations import EvaluationService
from agenteval_core.services.read import ReadService
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


def get_run_service(session: SessionDep, queue: QueueDep, settings: SettingsDep) -> RunService:
    return RunService(session, queue, settings)


def get_agent_service(session: SessionDep, settings: SettingsDep) -> AgentService:
    return AgentService(session, settings)


def get_dataset_service(session: SessionDep) -> DatasetService:
    return DatasetService(session)


def get_evaluation_service(
    session: SessionDep, queue: QueueDep, settings: SettingsDep
) -> EvaluationService:
    return EvaluationService(session, queue, settings)


def get_read_service(session: SessionDep, settings: SettingsDep) -> ReadService:
    return ReadService(session, settings)


ReadServiceDep = Annotated[ReadService, Depends(get_read_service)]
TaskServiceDep = Annotated[TaskService, Depends(get_task_service)]
AgentServiceDep = Annotated[AgentService, Depends(get_agent_service)]
RunServiceDep = Annotated[RunService, Depends(get_run_service)]
DatasetServiceDep = Annotated[DatasetService, Depends(get_dataset_service)]
EvaluationServiceDep = Annotated[EvaluationService, Depends(get_evaluation_service)]
