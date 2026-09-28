import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from agenteval_core.models.base import Base, CreatedAt, UUIDPk
from agenteval_core.states import COMPLETED_OUTCOMES, Outcome, sql_in_list

_STATUSES = "'QUEUED','CLAIMED','RUNNING','COMPLETED','FAILED'"
# Derived from states.py so the DB constraints can't drift from status_for().
_OUTCOMES = sql_in_list(Outcome)
_COMPLETED_OUTCOMES = sql_in_list(COMPLETED_OUTCOMES)


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
        # ... and agrees with status_for(): task/agent results COMPLETE, platform faults FAIL.
        CheckConstraint(
            f"status <> 'COMPLETED' OR outcome IN ({_COMPLETED_OUTCOMES})",
            name="completed_outcome",
        ),
        CheckConstraint(
            f"status <> 'FAILED' OR outcome NOT IN ({_COMPLETED_OUTCOMES})",
            name="failed_outcome",
        ),
        Index("ix_runs_status_created_at", "status", "created_at"),
        Index("ix_runs_task_id", "task_id"),
        Index("ix_runs_agent_version_id", "agent_version_id"),
    )

    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"), nullable=False)
    # Set for agent tasks, NULL for command tasks. The pairing rule spans two
    # tables, so the service enforces it (a CHECK can't see tasks.task_type).
    agent_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_versions.id"))
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
        CheckConstraint("steps >= 0", name="steps"),
        CheckConstraint("tokens_in >= 0 AND tokens_out >= 0", name="tokens"),
        CheckConstraint("cost_usd >= 0", name="cost_usd"),
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
    # Agent-attempt aggregates (NULL for command tasks). Denormalized from
    # trajectory_events, written once in the same fenced transaction as the
    # outcome; the trajectory stays authoritative.
    steps: Mapped[int | None] = mapped_column(Integer)
    tokens_in: Mapped[int | None] = mapped_column(BigInteger)  # incl. cache reads/writes
    tokens_out: Mapped[int | None] = mapped_column(BigInteger)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    final_answer: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
