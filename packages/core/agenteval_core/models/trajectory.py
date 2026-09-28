import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from agenteval_core.models.base import Base, NullableJSONB, UUIDPk

EVENT_TYPES = (
    "run_started",
    "model_call",
    "model_error",
    "tool_call",
    "limit_reached",
    "run_finished",
)
TOOL_STATUSES = ("ok", "error", "timeout", "denied", "invalid_input", "unknown_tool")


def _in(values: tuple[str, ...]) -> str:
    return ",".join(f"'{v}'" for v in values)


class TrajectoryEvent(UUIDPk, Base):
    """One operational event of an agent attempt. Append-only.

    Stores tool calls/results, model usage, errors and limits; never raw
    chain-of-thought (thinking blocks are not persisted).
    """

    __tablename__ = "trajectory_events"
    __table_args__ = (
        # Ordering + replay safety: seq N of an attempt can be written once.
        UniqueConstraint("attempt_id", "seq", name="uq_trajectory_events_attempt_id_seq"),
        CheckConstraint(f"event_type IN ({_in(EVENT_TYPES)})", name="event_type"),
        CheckConstraint(f"status IN ({_in(TOOL_STATUSES)})", name="status"),
        CheckConstraint(
            "(event_type = 'tool_call') = (tool IS NOT NULL)", name="tool_iff_tool_call"
        ),
        CheckConstraint(
            "(event_type = 'tool_call') = (status IS NOT NULL)", name="status_iff_tool_call"
        ),
        CheckConstraint("seq >= 0 AND step >= 0", name="seq_step"),
        CheckConstraint(
            "tokens_in >= 0 AND tokens_out >= 0 AND cache_read_tokens >= 0 "
            "AND cache_write_tokens >= 0",
            name="tokens",
        ),
        CheckConstraint("cost_usd >= 0", name="cost_usd"),
        CheckConstraint("duration_ms >= 0", name="duration_ms"),
    )

    attempt_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("run_attempts.id"), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    step: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    tool: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str | None] = mapped_column(String(32))
    error_class: Mapped[str | None] = mapped_column(String(64))
    input: Mapped[dict[str, Any] | None] = mapped_column(NullableJSONB)
    output: Mapped[dict[str, Any] | None] = mapped_column(NullableJSONB)
    # Model usage as reported by the provider (model_call events only).
    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)
    cache_read_tokens: Mapped[int | None] = mapped_column(Integer)
    cache_write_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
