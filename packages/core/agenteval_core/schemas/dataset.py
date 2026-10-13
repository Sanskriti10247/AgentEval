import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from agenteval_core.evaluation.taxonomy import DatasetStatus


class DatasetCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=50)
    description: str | None = Field(default=None, max_length=10_000)


class DatasetRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    name: str
    version: str
    description: str | None
    status: DatasetStatus
    frozen_at: datetime | None
    created_at: datetime
    task_ids: list[uuid.UUID]
    task_count: int
