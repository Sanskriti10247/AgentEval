import uuid
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from agenteval_core.models.base import Base, CreatedAt, UUIDPk


class Agent(UUIDPk, CreatedAt, Base):
    __tablename__ = "agents"

    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)


class AgentVersion(UUIDPk, CreatedAt, Base):
    """An immutable agent configuration. Runs reference a version, so a
    benchmark result is always attributable to one exact config."""

    __tablename__ = "agent_versions"
    __table_args__ = (
        UniqueConstraint("agent_id", "version", name="uq_agent_versions_agent_id_version"),
        # Re-submitting an identical config returns the existing version.
        UniqueConstraint("agent_id", "config_hash", name="uq_agent_versions_agent_id_config_hash"),
        CheckConstraint("version >= 1", name="version"),
        CheckConstraint("model_provider IN ('anthropic','fake')", name="model_provider"),
        CheckConstraint("length(config_hash) = 64", name="config_hash"),
    )

    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agents.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    model_provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    tools: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    shell_allowlist: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    limits: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    model_settings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # sha256 hex of the canonical JSON of every field above except agent_id/version.
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
