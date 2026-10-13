"""Datasets: DRAFT (tasks can be added via POST /tasks) -> FROZEN (immutable;
evaluations need it, D9). Tasks without an evaluation spec are allowed and
stay ungraded (R7)."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from agenteval_core.evaluation.taxonomy import DatasetStatus
from agenteval_core.models import Dataset
from agenteval_core.repositories.datasets import DatasetRepository
from agenteval_core.schemas.dataset import DatasetCreate, DatasetRead
from agenteval_core.services.errors import Conflict, NotFound


class DatasetService:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session
        self._datasets = DatasetRepository(session)

    async def create(self, data: DatasetCreate) -> DatasetRead:
        dataset = await self._datasets.create(data.name, data.version, data.description)
        if dataset is None:
            await self._s.rollback()
            raise Conflict(f"dataset {data.name!r} version {data.version!r} already exists")
        await self._s.commit()
        return await self._read(dataset)

    async def get(self, dataset_id: uuid.UUID) -> DatasetRead:
        return await self._read(await self._get(dataset_id))

    async def freeze(self, dataset_id: uuid.UUID) -> DatasetRead:
        """Idempotent: freezing a FROZEN dataset returns it unchanged."""
        dataset = await self._get(dataset_id)
        if dataset.status == DatasetStatus.DRAFT:
            if not await self._datasets.task_ids(dataset_id):
                raise Conflict(f"dataset {dataset_id} has no tasks; add tasks before freezing")
            await self._datasets.freeze(dataset_id)  # False: a concurrent freeze won
            await self._s.commit()
            dataset = await self._get(dataset_id)
        return await self._read(dataset)

    async def _get(self, dataset_id: uuid.UUID) -> Dataset:
        dataset = await self._datasets.get(dataset_id)
        if dataset is None:
            raise NotFound(f"dataset {dataset_id} not found")
        return dataset

    async def _read(self, dataset: Dataset) -> DatasetRead:
        ids = await self._datasets.task_ids(dataset.id)
        return DatasetRead(
            id=dataset.id,
            name=dataset.name,
            version=dataset.version,
            description=dataset.description,
            status=DatasetStatus(dataset.status),
            frozen_at=dataset.frozen_at,
            created_at=dataset.created_at,
            task_ids=ids,
            task_count=len(ids),
        )
