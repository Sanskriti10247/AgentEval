import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from agenteval_core.evaluation.taxonomy import (
    MAX_SNAPSHOT_BYTES,
    ArtifactKind,
    EvaluationStatus,
    FailureCategory,
)
from agenteval_core.models.base import Base, CreatedAt, NullableJSONB, UUIDPk, sql_values
from agenteval_core.reliability.policy import MAX_ATTEMPTS_CAP, ErrorKind
from agenteval_core.reliability.taxonomy import EvaluationErrorReason


class Evaluation(UUIDPk, CreatedAt, Base):
    """A batch: one agent version x one frozen dataset x N repetitions. Its
    progress is derived from its runs and their evaluations, never stored."""

    __tablename__ = "evaluations"
    __table_args__ = (
        CheckConstraint("repetitions BETWEEN 1 AND 10", name="repetitions"),
        CheckConstraint("length(scoring_config_hash) = 64", name="scoring_config_hash"),
        # Phase 5: a batch is cancelled by a user; both budgets are pinned at creation.
        CheckConstraint("cancel_reason IN ('user')", name="cancel_reason"),
        CheckConstraint(
            "(cancel_reason IS NULL) = (cancel_requested_at IS NULL)",
            name="cancel_reason_iff_request",
        ),
        CheckConstraint(
            f"run_max_attempts BETWEEN 1 AND {MAX_ATTEMPTS_CAP}", name="run_max_attempts"
        ),
        CheckConstraint(
            f"eval_max_attempts BETWEEN 1 AND {MAX_ATTEMPTS_CAP}", name="eval_max_attempts"
        ),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    dataset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("datasets.id"), nullable=False)
    agent_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_versions.id"), nullable=False
    )
    repetitions: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    scoring_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    scoring_config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), unique=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(String(16))
    run_max_attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="1")
    eval_max_attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="1")


class AttemptArtifact(UUIDPk, CreatedAt, Base):
    """Opaque bytes captured from an attempt. Never extracted on the host:
    only streamed into a sandbox's tar. The bytes live either in `content`
    (Phase 4 rows) or in a file on the artifact volume named by `storage_key`
    (ADR-008); the row is authoritative either way."""

    __tablename__ = "attempt_artifacts"
    __table_args__ = (
        UniqueConstraint("attempt_id", "kind", name="uq_attempt_artifacts_attempt_id_kind"),
        CheckConstraint(f"kind IN ({sql_values(ArtifactKind)})", name="kind"),
        CheckConstraint(f"size_bytes BETWEEN 0 AND {MAX_SNAPSHOT_BYTES}", name="size_bytes"),
        CheckConstraint(
            "content IS NULL OR size_bytes = octet_length(content)", name="size_matches_content"
        ),
        CheckConstraint("length(sha256) = 64", name="sha256"),
        CheckConstraint("num_nonnulls(content, storage_key) = 1", name="one_location"),
    )

    attempt_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("run_attempts.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[bytes | None] = mapped_column(LargeBinary)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str | None] = mapped_column(String(255))
    # e.g. the snapshot's normalized_paths; `metadata` is reserved by SQLAlchemy.
    artifact_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )


class AttemptEvaluation(UUIDPk, CreatedAt, Base):
    """What was checked for one attempt (D1: a separate record, written after
    finish_attempt). How it was scored lives in `scores`."""

    __tablename__ = "attempt_evaluations"
    __table_args__ = (
        UniqueConstraint("attempt_id", name="uq_attempt_evaluations_attempt_id"),
        CheckConstraint(f"status IN ({sql_values(EvaluationStatus)})", name="status"),
        CheckConstraint(
            f"failure_category IN ({sql_values(FailureCategory)})", name="failure_category"
        ),
        # Not finished yet: nothing decided.
        CheckConstraint(
            "status NOT IN ('PENDING','RUNNING') OR "
            "(task_success IS NULL AND failure_category IS NULL AND finished_at IS NULL)",
            name="unfinished",
        ),
        # Graded: success decided, a category and a finish time recorded.
        CheckConstraint(
            "status <> 'SUCCEEDED' OR (task_success IS NOT NULL "
            "AND failure_category IS NOT NULL AND finished_at IS NOT NULL)",
            name="succeeded",
        ),
        # The evaluator failed: always evaluation_error, with a reason.
        CheckConstraint(
            "status <> 'ERROR' OR (failure_category = 'evaluation_error' "
            "AND error_message IS NOT NULL AND task_success IS NULL AND finished_at IS NOT NULL)",
            name="error",
        ),
        # Not graded: success is unknown.
        CheckConstraint(
            "status <> 'SKIPPED' OR (task_success IS NULL AND finished_at IS NOT NULL)",
            name="skipped",
        ),
        # 'passed' iff the task succeeded.
        CheckConstraint(
            "task_success IS NULL OR task_success = (failure_category = 'passed')",
            name="passed_iff_success",
        ),
        CheckConstraint("duration_ms >= 0", name="duration_ms"),
        # Phase 5: grading budget pinned per row; no PENDING row is ever exhausted (G9).
        CheckConstraint("eval_attempts >= 0", name="eval_attempts"),
        CheckConstraint(
            f"max_eval_attempts BETWEEN 1 AND {MAX_ATTEMPTS_CAP}", name="max_eval_attempts"
        ),
        CheckConstraint("eval_attempts <= max_eval_attempts", name="eval_budget"),
        CheckConstraint(
            "status <> 'PENDING' OR eval_attempts < max_eval_attempts", name="pending_budget"
        ),
        CheckConstraint(
            f"error_reason IN ({sql_values(EvaluationErrorReason)})", name="error_reason_values"
        ),
        CheckConstraint(f"error_kind IN ({sql_values(ErrorKind)})", name="error_kind_values"),
        CheckConstraint("error_kind IS NULL OR status = 'ERROR'", name="error_kind"),
        CheckConstraint(
            "error_reason IS NULL OR status IN ('ERROR','PENDING')", name="reason_states"
        ),
        Index(
            "ix_attempt_evaluations_lease",
            "lease_expires_at",
            postgresql_where=text("status = 'RUNNING'"),
        ),
        Index(
            "ix_attempt_evaluations_pending",
            "next_attempt_at",
            "dispatched_at",
            postgresql_where=text("status = 'PENDING'"),
        ),
    )

    attempt_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("run_attempts.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    evaluator_version: Mapped[str] = mapped_column(String(32), nullable=False)
    checks: Mapped[list[dict[str, Any]] | None] = mapped_column(NullableJSONB)
    metrics: Mapped[dict[str, Any] | None] = mapped_column(NullableJSONB)
    integrity: Mapped[dict[str, Any] | None] = mapped_column(NullableJSONB)
    task_success: Mapped[bool | None] = mapped_column(Boolean)
    failure_category: Mapped[str | None] = mapped_column(String(32))
    failure_tags: Mapped[list[str] | None] = mapped_column(NullableJSONB)
    failure_detail: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Phase 5: grading lease and retry state.
    worker_id: Mapped[str | None] = mapped_column(String(255))
    lease_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    eval_attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    max_eval_attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="1")
    error_reason: Mapped[str | None] = mapped_column(String(32))
    error_kind: Mapped[str | None] = mapped_column(String(20))


class Score(UUIDPk, CreatedAt, Base):
    """One scoring of one evaluation under one config. Append-only: rescoring
    with another config adds a row (D8); the same config is idempotent."""

    __tablename__ = "scores"
    __table_args__ = (
        UniqueConstraint(
            "attempt_evaluation_id",
            "scoring_config_hash",
            name="uq_scores_attempt_evaluation_id_scoring_config_hash",
        ),
        CheckConstraint("overall >= 0 AND overall <= 1", name="overall"),
        CheckConstraint("length(scoring_config_hash) = 64", name="scoring_config_hash"),
    )

    attempt_evaluation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("attempt_evaluations.id"), nullable=False
    )
    scoring_config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scoring_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    components: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    overall: Mapped[Decimal] = mapped_column(Numeric(6, 5), nullable=False)
