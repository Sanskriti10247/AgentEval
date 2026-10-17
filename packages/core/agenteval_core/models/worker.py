from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, SmallInteger, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from agenteval_core.models.base import Base


class Worker(Base):
    """Registry of worker processes, for visibility only (F12). Fencing never
    depends on it: leases and epochs on the rows being worked on do that."""

    __tablename__ = "workers"
    __table_args__ = (
        CheckConstraint("stop_reason IN ('shutdown','lost')", name="stop_reason"),
        # A worker has a stop reason exactly when it has stopped (migration 0007).
        CheckConstraint(
            "(stop_reason IS NULL) = (stopped_at IS NULL)", name="stop_reason_iff_stopped"
        ),
        Index("ix_workers_live", "last_heartbeat_at", postgresql_where=text("stopped_at IS NULL")),
    )

    id: Mapped[str] = mapped_column(String(255), primary_key=True)  # "{hostname}-{pid}-{8 hex}"
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    pid: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    concurrency: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 'shutdown' (graceful) or 'lost' (the reconciler found no heartbeat); G3.
    stop_reason: Mapped[str | None] = mapped_column(String(16))
