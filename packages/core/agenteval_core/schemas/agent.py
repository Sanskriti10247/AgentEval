import re
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_PROGRAM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")


class AgentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Unique; letters, digits, '.', '_' and '-'.")

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not _NAME.match(v):
            raise ValueError("must start with a letter or digit and use only A-Z a-z 0-9 . _ -")
        return v


class AgentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    created_at: datetime


class AgentVersionCreate(BaseModel):
    """Shape only. Config-dependent rules (enabled providers, pricing, caps,
    fake-script validity) are checked by AgentService."""

    model_config = ConfigDict(extra="forbid")

    model_provider: Literal["anthropic", "fake"]
    model: str = Field(min_length=1, max_length=100)
    system_prompt: str = Field(default="", max_length=50_000)
    tools: list[str] = Field(min_length=1, max_length=20)
    shell_allowlist: list[str] = Field(default_factory=list, max_length=50)
    limits: dict[str, Any] = Field(default_factory=dict)
    model_settings: dict[str, Any] = Field(default_factory=dict)

    @field_validator("shell_allowlist")
    @classmethod
    def _programs(cls, v: list[str]) -> list[str]:
        for p in v:
            if not _PROGRAM.match(p):
                raise ValueError(f"{p!r} is not a bare program name (no paths or spaces)")
        return v


class AgentVersionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    agent_id: uuid.UUID
    version: int
    model_provider: str
    model: str
    system_prompt: str
    tools: list[str]
    shell_allowlist: list[str]
    limits: dict[str, Any]
    model_settings: dict[str, Any]
    config_hash: str
    created_at: datetime


class TrajectoryEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    seq: int
    step: int
    event_type: str
    tool: str | None
    status: str | None
    error_class: str | None
    input: dict[str, Any] | None
    output: dict[str, Any] | None
    tokens_in: int | None
    tokens_out: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    cost_usd: Decimal | None
    duration_ms: int | None
    ts: datetime


class TrajectoryPage(BaseModel):
    events: list[TrajectoryEventRead]
    next_after_seq: int | None = Field(
        description="Pass as after_seq to fetch the next page; null when this page is the last."
    )
