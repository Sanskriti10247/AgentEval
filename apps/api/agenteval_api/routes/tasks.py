import uuid

from fastapi import APIRouter, status

from agenteval_api.deps import TaskServiceDep
from agenteval_core.schemas.task_api import TaskCreate, TaskRead

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.post("", status_code=status.HTTP_201_CREATED, response_model=TaskRead)
async def create_task(body: TaskCreate, tasks: TaskServiceDep) -> TaskRead:
    """Agent tasks may join a DRAFT dataset and carry an evaluation spec.
    The spec is write-only: no response ever contains it (D11)."""
    return TaskRead.from_task(await tasks.create(body))


@router.get("/{task_id}", response_model=TaskRead)
async def get_task(task_id: uuid.UUID, tasks: TaskServiceDep) -> TaskRead:
    return TaskRead.from_task(await tasks.get(task_id))
