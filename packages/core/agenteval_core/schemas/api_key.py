import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agenteval_core.auth import Scope


class ApiKeyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100, description="A label for the key's holder.")
    scopes: list[Scope] = Field(min_length=1, description="read, write and/or admin.")
    expires_days: int | None = Field(default=None, ge=1, le=3650)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v


class ApiKeyRead(BaseModel):
    """Everything about a key except its secret and its hash (never returned)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    prefix: str
    scopes: list[Scope]
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None


class ApiKeyCreated(ApiKeyRead):
    key: str = Field(description="The full key. Shown once: it cannot be retrieved later.")
