import posixpath
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_FILES_BYTES = 1024 * 1024
MAX_FILES = 100
MAX_COMMAND_ARGS = 64
MAX_ARG_LEN = 4096
MAX_COMMAND_TIMEOUT_S = 300
MAX_AGENT_TIMEOUT_S = 1800


def _validate_argv(v: list[str] | None) -> list[str] | None:
    if v is None:
        return v
    if not v or not v[0]:
        raise ValueError("argv must be non-empty and argv[0] must not be empty")
    if len(v) > MAX_COMMAND_ARGS:
        raise ValueError(f"at most {MAX_COMMAND_ARGS} args")
    for arg in v:
        if len(arg) > MAX_ARG_LEN or "\x00" in arg:
            raise ValueError(f"args must be <= {MAX_ARG_LEN} chars with no NUL")
    return v


class TaskCreate(BaseModel):
    """A command task runs `command` once (S1). An agent task gives
    `instructions` to an agent, which works in the sandbox with tools;
    `test_command` (optional) backs its run_tests tool."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10_000)
    task_type: Literal["command", "agent"] = "command"
    image: str = Field(min_length=1, max_length=255)
    command: list[str] | None = None
    instructions: str | None = Field(default=None, min_length=1, max_length=50_000)
    test_command: list[str] | None = None
    files: dict[str, str] = Field(default_factory=dict, max_length=MAX_FILES)
    timeout_s: int = Field(default=30, ge=1, le=MAX_AGENT_TIMEOUT_S)
    memory_mb: int = Field(default=256, ge=64, le=1024)
    cpu_limit: float = Field(default=1.0, ge=0.1, le=2.0)

    @field_validator("command", "test_command")
    @classmethod
    def _argv(cls, v: list[str] | None) -> list[str] | None:
        return _validate_argv(v)

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

    @model_validator(mode="after")
    def _by_type(self) -> "TaskCreate":
        if self.task_type == "command":
            if self.command is None:
                raise ValueError("command tasks need a command")
            if self.instructions is not None or self.test_command is not None:
                raise ValueError("instructions and test_command are for agent tasks")
            if self.timeout_s > MAX_COMMAND_TIMEOUT_S:
                raise ValueError(f"command tasks allow timeout_s <= {MAX_COMMAND_TIMEOUT_S}")
        else:
            if self.command is not None:
                raise ValueError("agent tasks have no command (the agent decides what to run)")
            if self.instructions is None:
                raise ValueError("agent tasks need instructions")
        return self


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
    command: list[str] | None
    instructions: str | None
    test_command: list[str] | None
    files: dict[str, str]
    timeout_s: int
    memory_mb: int
    cpu_limit: float
    created_at: datetime
