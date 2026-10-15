"""Dataset persistence. DRAFT -> FROZEN is a single guarded UPDATE, and
adding a task locks the dataset row FOR SHARE while requiring DRAFT, so a
task can never join a dataset after it is frozen (the two serialise).

Repositories flush but never commit; services own transactions.
"""

import uuid

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from agenteval_core.evaluation.taxonomy import DatasetStatus
from agenteval_core.models import Dataset, Task
from agenteval_core.models.base import new_id


class DatasetRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def create(self, name: str, version: str, description: str | None) -> Dataset | None:
        """Insert a DRAFT dataset; None if (name, version) is taken."""
        stmt = (
            pg_insert(Dataset)
            .values(
                id=new_id(),
                name=name,
                version=version,
                description=description,
                status=DatasetStatus.DRAFT,
            )
            .on_conflict_do_nothing(index_elements=["name", "version"])
            .returning(Dataset)
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def get(self, dataset_id: uuid.UUID) -> Dataset | None:
        return await self._s.get(Dataset, dataset_id, populate_existing=True)

    async def lock_draft(self, dataset_id: uuid.UUID) -> bool:
        """FOR SHARE on a DRAFT dataset, held until the caller's commit:
        freeze() waits for it. False if missing or already frozen."""
        result = await self._s.execute(
            select(Dataset.id)
            .where(Dataset.id == dataset_id, Dataset.status == DatasetStatus.DRAFT)
            .with_for_update(read=True)
        )
        return result.scalar_one_or_none() is not None

    async def freeze(self, dataset_id: uuid.UUID) -> bool:
        """DRAFT -> FROZEN. False if it wasn't DRAFT."""
        stmt = (
            update(Dataset)
            .where(Dataset.id == dataset_id, Dataset.status == DatasetStatus.DRAFT)
            .values(status=DatasetStatus.FROZEN, frozen_at=func.now())
            .returning(Dataset.id)
            .execution_options(synchronize_session=False)
        )
        return (await self._s.execute(stmt)).scalar_one_or_none() is not None

    async def task_ids(self, dataset_id: uuid.UUID) -> list[uuid.UUID]:
        result = await self._s.execute(
            select(Task.id).where(Task.dataset_id == dataset_id).order_by(Task.created_at, Task.id)
        )
        return list(result.scalars())
