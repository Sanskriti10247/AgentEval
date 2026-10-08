from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from agenteval_core.evaluation.taxonomy import DatasetStatus
from agenteval_core.models.base import Base, CreatedAt, UUIDPk, sql_values


class Dataset(UUIDPk, CreatedAt, Base):
    """A versioned set of agent tasks. DRAFT -> FROZEN (D9); evaluations need a
    frozen dataset, so a result is tied to one exact task set."""

    __tablename__ = "datasets"
    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_datasets_name_version"),
        CheckConstraint(f"status IN ({sql_values(DatasetStatus)})", name="status"),
        CheckConstraint("(status = 'FROZEN') = (frozen_at IS NOT NULL)", name="frozen_at"),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    version: Mapped[str] = mapped_column(String(50), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=DatasetStatus.DRAFT)
    frozen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
