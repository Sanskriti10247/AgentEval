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
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from agenteval_core.models.base import Base, CreatedAt, UUIDPk, sql_values
from agenteval_core.reliability.policy import (
    MAX_ATTEMPTS_CAP,
    MAX_UNSTARTED_CLAIMS,
    DeadLetterReason,
)
from agenteval_core.reliability.taxonomy import (
    ATTEMPT_REASONS,
    RUN_LEVEL_REASONS,
    CancelReason,
    WorkspaceCapture,
)
from agenteval_core.reliability.transitions import AttemptState, RunState
from agenteval_core.states import COMPLETED_OUTCOMES, FAILED_OUTCOMES, Outcome, sql_in_list

# Phase 5 lifecycles, now also states.py's own RunStatus/AttemptStatus (Step 3).
_STATUSES = sql_values(RunState)
_ATTEMPT_STATUSES = sql_values(AttemptState)
# Derived from states.py so the DB constraints can't drift from status_for().
_OUTCOMES = sql_in_list(Outcome)
_COMPLETED_OUTCOMES = sql_in_list(COMPLETED_OUTCOMES)
# An attempt's reason must agree with its outcome (or be the legacy backfill).
_REASON_OUTCOME = (
    "failure_reason IS NULL OR failure_reason = 'unclassified'"
    " OR (failure_reason LIKE 'model\\_%' AND outcome = 'MODEL_ERROR')"
    " OR (failure_reason LIKE 'sandbox\\_%' AND outcome = 'SANDBOX_ERROR')"
    " OR (failure_reason LIKE 'cancelled\\_%' AND outcome IS NULL)"
    " OR (failure_reason NOT LIKE 'model\\_%' AND failure_reason NOT LIKE 'sandbox\\_%'"
    " AND failure_reason NOT LIKE 'cancelled\\_%' AND outcome = 'INTERNAL_ERROR')"
)
# Step 3 / migration 0005: every FINISHED attempt with a failed outcome, plus
# every ABANDONED or CANCELLED attempt, must carry a reason - and no other
# attempt may.
_FAILED_OUTCOMES = sql_in_list(FAILED_OUTCOMES)
_REASON_PRESENCE = (
    "(failure_reason IS NOT NULL) = ("
    "status IN ('ABANDONED','CANCELLED') "
    f"OR (status = 'FINISHED' AND outcome IN ({_FAILED_OUTCOMES})))"
)


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
        Index("ix_runs_evaluation_id", "evaluation_id"),
        # A batch run carries its repetition number; ad-hoc runs carry neither.
        CheckConstraint(
            "(evaluation_id IS NULL) = (repetition IS NULL)", name="evaluation_repetition"
        ),
        CheckConstraint("repetition >= 1", name="repetition"),
        # Exactly one run per (evaluation, task, repetition) cell, whatever the
        # idempotency keys say: concurrent batch creation can't double-book.
        UniqueConstraint(
            "evaluation_id",
            "task_id",
            "repetition",
            name="uq_runs_evaluation_id_task_id_repetition",
        ),
        # Phase 5: budgets, retries, cancellation and dead letters.
        CheckConstraint(f"cancel_reason IN ({sql_values(CancelReason)})", name="cancel_reason"),
        CheckConstraint(f"max_attempts BETWEEN 1 AND {MAX_ATTEMPTS_CAP}", name="max_attempts"),
        CheckConstraint(
            f"dead_letter_reason IN ({sql_values(DeadLetterReason)})", name="dead_letter_reason"
        ),
        CheckConstraint("replay_count >= 0", name="replay_count"),
        CheckConstraint(
            f"unstarted_claims BETWEEN 0 AND {MAX_UNSTARTED_CLAIMS}", name="unstarted_claims"
        ),
        CheckConstraint(
            f"failure_reason IN ({sql_values(RUN_LEVEL_REASONS)})", name="failure_reason_values"
        ),
        CheckConstraint(
            "failure_reason IS NULL OR status = 'FAILED'", name="failure_reason_failed"
        ),
        CheckConstraint("(status = 'RETRYING') = (next_attempt_at IS NOT NULL)", name="retrying"),
        CheckConstraint("dead_letter_reason IS NULL OR status = 'FAILED'", name="dead_letter"),
        CheckConstraint(
            "(cancel_reason IS NULL) = (cancel_requested_at IS NULL)",
            name="cancel_reason_iff_request",
        ),
        # A cancel request only exists where it can still matter, or on the result (G11).
        CheckConstraint(
            "cancel_requested_at IS NULL OR status IN ('CLAIMED','RUNNING','CANCELLED')",
            name="cancel_states",
        ),
        CheckConstraint(
            "status <> 'CANCELLED' OR cancel_requested_at IS NOT NULL",
            name="cancelled_has_request",
        ),
        CheckConstraint("attempt_count <= max_attempts", name="attempts"),
        # A lease exists exactly while the run is CLAIMED or RUNNING (Step 4; migration 0006).
        CheckConstraint(
            "(status IN ('CLAIMED','RUNNING')) = (lease_expires_at IS NOT NULL)", name="lease"
        ),
        Index(
            "ix_runs_lease",
            "lease_expires_at",
            postgresql_where=text("status IN ('CLAIMED','RUNNING')"),
        ),
        Index("ix_runs_retry", "next_attempt_at", postgresql_where=text("status = 'RETRYING'")),
        Index("ix_runs_dispatch", "dispatched_at", postgresql_where=text("status = 'QUEUED'")),
        Index("ix_runs_failed", "created_at", "id", postgresql_where=text("status = 'FAILED'")),
    )

    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"), nullable=False)
    # Set for agent tasks, NULL for command tasks. The pairing rule spans two
    # tables, so the service enforces it (a CHECK can't see tasks.task_type).
    agent_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_versions.id"))
    # Phase 4: set for runs created by an evaluation batch.
    evaluation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("evaluations.id"))
    repetition: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    outcome: Mapped[str | None] = mapped_column(String(32))
    idempotency_key: Mapped[str | None] = mapped_column(String(255), unique=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    worker_id: Mapped[str | None] = mapped_column(String(255))
    error_message: Mapped[str | None] = mapped_column(Text)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Phase 5. lease_epoch is the fencing token: bumped by every claim and recovery.
    lease_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(String(16))
    # Pinned when the run is created; the column default keeps direct inserts at Phase 4 behaviour.
    max_attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="1")
    dead_letter_reason: Mapped[str | None] = mapped_column(String(32))
    replay_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    # Claim leases that expired before any attempt started (G5); never an execution attempt.
    unstarted_claims: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    # Run-level failures that have no attempt (R6, R4b).
    failure_reason: Mapped[str | None] = mapped_column(String(40))


class RunAttempt(UUIDPk, Base):
    __tablename__ = "run_attempts"
    __table_args__ = (
        # Fencing: attempt N of a run can only ever be written once.
        UniqueConstraint("run_id", "attempt_no", name="uq_run_attempts_run_id_attempt_no"),
        CheckConstraint(f"status IN ({_ATTEMPT_STATUSES})", name="status"),
        CheckConstraint(f"outcome IN ({_OUTCOMES})", name="outcome"),
        CheckConstraint(
            "(status IN ('FINISHED','ABANDONED')) = (outcome IS NOT NULL)",
            name="outcome_iff_finished",
        ),
        CheckConstraint(
            f"failure_reason IN ({sql_values(ATTEMPT_REASONS)})", name="failure_reason_values"
        ),
        CheckConstraint(
            f"workspace_capture IN ({sql_values(WorkspaceCapture)})",
            name="workspace_capture_values",
        ),
        CheckConstraint(
            "status <> 'ABANDONED' OR "
            "(outcome = 'INTERNAL_ERROR' AND failure_reason = 'lease_expired')",
            name="abandoned",
        ),
        CheckConstraint(
            "status <> 'CANCELLED' OR failure_reason IN ('cancelled_by_batch','cancelled_by_user')",
            name="cancelled",
        ),
        CheckConstraint("(retryable IS NULL) = (failure_reason IS NULL)", name="retryable"),
        CheckConstraint(_REASON_OUTCOME, name="reason_outcome"),
        # Step 3 / migration 0005: the reason is present exactly for a FINISHED
        # attempt with a failed outcome, or an ABANDONED/CANCELLED one.
        CheckConstraint(_REASON_PRESENCE, name="reason"),
        # A capture value only on a FINISHED attempt with a completed outcome (G10).
        CheckConstraint(
            "workspace_capture IS NULL OR "
            f"(status = 'FINISHED' AND outcome IN ({_COMPLETED_OUTCOMES}))",
            name="capture",
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
    # Phase 4: the image ID (digest) the sandbox actually ran, for reproducibility.
    sandbox_image_id: Mapped[str | None] = mapped_column(String(128))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Phase 5: the run's epoch this attempt was created under; why it failed.
    lease_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    failure_reason: Mapped[str | None] = mapped_column(String(40))
    retryable: Mapped[bool | None] = mapped_column(Boolean)
    workspace_capture: Mapped[str | None] = mapped_column(String(16))
