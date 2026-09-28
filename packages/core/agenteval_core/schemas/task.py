import posixpath
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_FILES_BYTES = 1024 * 1024
MAX_FILES = 100
MAX_COMMAND_ARGS = 64
MAX_ARG_LEN = 4096


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10_000)
    task_type: Literal["command"] = "command"
    image: str = Field(min_length=1, max_length=255)
    command: list[str] = Field(min_length=1, max_length=MAX_COMMAND_ARGS)
    files: dict[str, str] = Field(default_factory=dict, max_length=MAX_FILES)
    timeout_s: int = Field(default=30, ge=1, le=300)
    memory_mb: int = Field(default=256, ge=64, le=1024)
    cpu_limit: float = Field(default=1.0, ge=0.1, le=2.0)

    @field_validator("command")
    @classmethod
    def _command_args(cls, v: list[str]) -> list[str]:
        if not v[0]:
            raise ValueError("command[0] must not be empty")
        for arg in v:
            if len(arg) > MAX_ARG_LEN or "\x00" in arg:
                raise ValueError(f"command args must be <= {MAX_ARG_LEN} chars with no NUL")
        return v

    @field_validator("files")
    @classmethod
    def _files(cls, v: dict[str, str]) -> dict[str, str]:
        total = 0
        for path, content in v.items():
            validate_relative_path(path)
            total += len(path.encode()) + len(content.encode())
        if total > MAX_FILES_BYTES:
            raise ValueError(f"files exceed {MAX_FILES_BYTES} bytes in total")
        return v


def validate_relative_path(path: str) -> None:
    """Files are extracted under /workspace; paths must stay inside it."""
    if not path or len(path) > 255 or "\x00" in path or "\\" in path:
        raise ValueError(f"invalid file path: {path!r}")
    if path.startswith("/"):
        raise ValueError(f"file path must be relative: {path!r}")
    parts = path.split("/")
    if any(p in ("", ".", "..") for p in parts) or posixpath.normpath(path) != path:
        raise ValueError(f"file path must be normalized with no '..': {path!r}")


class TaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    task_type: str
    image: str
    command: list[str]
    files: dict[str, str]
    timeout_s: int
    memory_mb: int
    cpu_limit: float
    created_at: datetime
