import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from agenteval_core.models.base import Base, CreatedAt, UUIDPk

_STATUSES = "'QUEUED','CLAIMED','RUNNING','COMPLETED','FAILED'"
_OUTCOMES = "'SUCCESS','NON_ZERO_EXIT','TIMEOUT','SANDBOX_ERROR','INTERNAL_ERROR'"
_TASK_OUTCOMES = "'SUCCESS','NON_ZERO_EXIT','TIMEOUT'"


class Run(UUIDPk, CreatedAt, Base):
    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint(f"status IN ({_STATUSES})", name="status"),
        CheckConstraint(f"outcome IN ({_OUTCOMES})", name="outcome"),
        # outcome is set exactly when the run is terminal ...
        CheckConstraint(
            "(status IN ('COMPLETED','FAILED')) = (outcome IS NOT NULL)",
            name="outcome_iff_terminal",
        ),
        # ... and agrees with status_for(): task results COMPLETE, platform faults FAIL.
        CheckConstraint(
            f"status <> 'COMPLETED' OR outcome IN ({_TASK_OUTCOMES})", name="completed_outcome"
        ),
        CheckConstraint(
            f"status <> 'FAILED' OR outcome NOT IN ({_TASK_OUTCOMES})", name="failed_outcome"
        ),
        Index("ix_runs_status_created_at", "status", "created_at"),
        Index("ix_runs_task_id", "task_id"),
    )

    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    outcome: Mapped[str | None] = mapped_column(String(32))
    idempotency_key: Mapped[str | None] = mapped_column(String(255), unique=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    worker_id: Mapped[str | None] = mapped_column(String(255))
    error_message: Mapped[str | None] = mapped_column(Text)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunAttempt(UUIDPk, Base):
    __tablename__ = "run_attempts"
    __table_args__ = (
        # Fencing: attempt N of a run can only ever be written once.
        UniqueConstraint("run_id", "attempt_no", name="uq_run_attempts_run_id_attempt_no"),
        CheckConstraint("status IN ('RUNNING','FINISHED')", name="status"),
        CheckConstraint(f"outcome IN ({_OUTCOMES})", name="outcome"),
        CheckConstraint(
            "(status = 'FINISHED') = (outcome IS NOT NULL)", name="outcome_iff_finished"
        ),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id"), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    outcome: Mapped[str | None] = mapped_column(String(32))
    exit_code: Mapped[int | None] = mapped_column(Integer)
    oom_killed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    stdout: Mapped[str] = mapped_column(Text, nullable=False, default="")
    stderr: Mapped[str] = mapped_column(Text, nullable=False, default="")
    stdout_truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    stderr_truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
