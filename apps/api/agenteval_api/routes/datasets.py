import uuid

from fastapi import APIRouter, status

from agenteval_api.deps import DatasetServiceDep
from agenteval_core.schemas.dataset import DatasetCreate, DatasetRead

router = APIRouter(prefix="/datasets", tags=["datasets"])


@router.post("", status_code=status.HTTP_201_CREATED, response_model=DatasetRead)
async def create_dataset(body: DatasetCreate, datasets: DatasetServiceDep) -> DatasetRead:
    """A DRAFT dataset. Add agent tasks with POST /tasks and `dataset_id`.
    409 if (name, version) exists."""
    return await datasets.create(body)


@router.get("/{dataset_id}", response_model=DatasetRead)
async def get_dataset(dataset_id: uuid.UUID, datasets: DatasetServiceDep) -> DatasetRead:
    return await datasets.get(dataset_id)


@router.post("/{dataset_id}/freeze", response_model=DatasetRead)
async def freeze_dataset(dataset_id: uuid.UUID, datasets: DatasetServiceDep) -> DatasetRead:
    """DRAFT -> FROZEN; idempotent. Frozen datasets are immutable and are
    what evaluations run on. 409 if the dataset has no tasks."""
    return await datasets.freeze(dataset_id)
