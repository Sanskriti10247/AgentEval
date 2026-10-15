import uuid

from fastapi import APIRouter, status

from agenteval_api.deps import TaskServiceDep
from agenteval_core.schemas.task import TaskCreate, TaskRead

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.post("", status_code=status.HTTP_201_CREATED, response_model=TaskRead)
async def create_task(body: TaskCreate, tasks: TaskServiceDep) -> TaskRead:
    return TaskRead.model_validate(await tasks.create(body))


@router.get("/{task_id}", response_model=TaskRead)
async def get_task(task_id: uuid.UUID, tasks: TaskServiceDep) -> TaskRead:
    return TaskRead.model_validate(await tasks.get(task_id))
