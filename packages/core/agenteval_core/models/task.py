from sqlalchemy import CheckConstraint, Float, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from agenteval_core.models.base import Base, CreatedAt, UUIDPk


class Task(UUIDPk, CreatedAt, Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint("task_type IN ('command')", name="task_type"),
        CheckConstraint("timeout_s BETWEEN 1 AND 300", name="timeout_s"),
        CheckConstraint("memory_mb BETWEEN 64 AND 1024", name="memory_mb"),
        CheckConstraint("cpu_limit >= 0.1 AND cpu_limit <= 2.0", name="cpu_limit"),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    task_type: Mapped[str] = mapped_column(String(32), nullable=False, default="command")
    image: Mapped[str] = mapped_column(String(255), nullable=False)
    command: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    files: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False, default=dict)
    timeout_s: Mapped[int] = mapped_column(Integer, nullable=False)
    memory_mb: Mapped[int] = mapped_column(Integer, nullable=False)
    cpu_limit: Mapped[float] = mapped_column(Float, nullable=False)
