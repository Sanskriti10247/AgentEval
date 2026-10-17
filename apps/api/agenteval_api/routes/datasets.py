import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from agenteval_api.auth import READ, WRITE
from agenteval_api.deps import DatasetServiceDep, ReadServiceDep
from agenteval_core.evaluation.taxonomy import DatasetStatus
from agenteval_core.schemas.dataset import DatasetCreate, DatasetRead
from agenteval_core.schemas.read import DatasetPage

router = APIRouter(prefix="/datasets", tags=["datasets"])


@router.post(
    "", status_code=status.HTTP_201_CREATED, response_model=DatasetRead, dependencies=[WRITE]
)
async def create_dataset(body: DatasetCreate, datasets: DatasetServiceDep) -> DatasetRead:
    """A DRAFT dataset. Add agent tasks with POST /tasks and `dataset_id`.
    409 if (name, version) exists."""
    return await datasets.create(body)


@router.get("", response_model=DatasetPage, dependencies=[READ])
async def list_datasets(
    read: ReadServiceDep,
    status: Annotated[DatasetStatus | None, Query()] = None,
    after: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> DatasetPage:
    """Datasets newest first, keyset-paged, with `task_count` (not the id list)."""
    return await read.datasets(status=status, after=after, limit=limit)


@router.get("/{dataset_id}", response_model=DatasetRead, dependencies=[READ])
async def get_dataset(dataset_id: uuid.UUID, datasets: DatasetServiceDep) -> DatasetRead:
    return await datasets.get(dataset_id)


@router.post("/{dataset_id}/freeze", response_model=DatasetRead, dependencies=[WRITE])
async def freeze_dataset(dataset_id: uuid.UUID, datasets: DatasetServiceDep) -> DatasetRead:
    """DRAFT -> FROZEN; idempotent. Frozen datasets are immutable and are
    what evaluations run on. 409 if the dataset has no tasks."""
    return await datasets.freeze(dataset_id)
