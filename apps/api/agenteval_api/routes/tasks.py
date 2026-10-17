import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Query, status

from agenteval_api.deps import ReadServiceDep, TaskServiceDep
from agenteval_core.schemas.read import TaskPage
from agenteval_core.schemas.task_api import TaskCreate, TaskRead

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.post("", status_code=status.HTTP_201_CREATED, response_model=TaskRead)
async def create_task(body: TaskCreate, tasks: TaskServiceDep) -> TaskRead:
    """Agent tasks may join a DRAFT dataset and carry an evaluation spec.
    The spec is write-only: no response ever contains it (D11)."""
    return TaskRead.from_task(await tasks.create(body))


@router.get("", response_model=TaskPage)
async def list_tasks(
    read: ReadServiceDep,
    dataset_id: Annotated[uuid.UUID | None, Query()] = None,
    task_type: Annotated[Literal["command", "agent"] | None, Query()] = None,
    difficulty: Annotated[Literal["easy", "medium", "hard"] | None, Query()] = None,
    after: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> TaskPage:
    """Tasks newest first, keyset-paged. Public fields only: no files,
    instructions, commands or evaluation spec (`has_evaluation` says whether
    one exists). Fetch one task for its full public form."""
    return await read.tasks(
        dataset_id=dataset_id, task_type=task_type, difficulty=difficulty, after=after, limit=limit
    )


@router.get("/{task_id}", response_model=TaskRead)
async def get_task(task_id: uuid.UUID, tasks: TaskServiceDep) -> TaskRead:
    return TaskRead.from_task(await tasks.get(task_id))
