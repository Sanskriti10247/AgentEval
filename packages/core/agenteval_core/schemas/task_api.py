"""Task request/response schemas.

Separate from schemas/task.py because TaskCreate carries an EvaluationSpec,
and evaluation/spec.py itself imports the path and size helpers from
schemas/task.py.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agenteval_core.evaluation.spec import EvaluationSpec
from agenteval_core.models import Task
from agenteval_core.schemas.task import (
    MAX_AGENT_TIMEOUT_S,
    MAX_COMMAND_TIMEOUT_S,
    MAX_FILES,
    MAX_FILES_BYTES,
    validate_argv,
    validate_relative_path,
)


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
    # Phase 4 (agent tasks only). `evaluation` is WRITE-ONLY (D11): it is
    # never returned by any endpoint; TaskRead only says whether it exists.
    dataset_id: uuid.UUID | None = None
    difficulty: Literal["easy", "medium", "hard"] | None = None
    evaluation: EvaluationSpec | None = None

    @field_validator("command", "test_command")
    @classmethod
    def _argv(cls, v: list[str] | None) -> list[str] | None:
        return validate_argv(v)

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
    def _by_type(self) -> TaskCreate:
        if self.task_type == "command":
            if self.command is None:
                raise ValueError("command tasks need a command")
            if self.instructions is not None or self.test_command is not None:
                raise ValueError("instructions and test_command are for agent tasks")
            if self.timeout_s > MAX_COMMAND_TIMEOUT_S:
                raise ValueError(f"command tasks allow timeout_s <= {MAX_COMMAND_TIMEOUT_S}")
            if self.evaluation is not None or self.dataset_id is not None:
                # D4: command tasks stay as in S1. Fixed text: never spec content.
                raise ValueError("evaluation and dataset_id are for agent tasks")
        else:
            if self.command is not None:
                raise ValueError("agent tasks have no command (the agent decides what to run)")
            if self.instructions is None:
                raise ValueError("agent tasks need instructions")
        return self


class TaskRead(BaseModel):
    """The public task. Built only via from_task(): there is deliberately no
    `evaluation` field, so the stored spec can't be serialised (D11)."""

    model_config = ConfigDict(extra="forbid")

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
    dataset_id: uuid.UUID | None
    difficulty: str | None
    has_evaluation: bool

    @classmethod
    def from_task(cls, task: Task) -> TaskRead:
        return cls(
            id=task.id,
            name=task.name,
            description=task.description,
            task_type=task.task_type,
            image=task.image,
            command=task.command,
            instructions=task.instructions,
            test_command=task.test_command,
            files=task.files,
            timeout_s=task.timeout_s,
            memory_mb=task.memory_mb,
            cpu_limit=task.cpu_limit,
            created_at=task.created_at,
            dataset_id=task.dataset_id,
            difficulty=task.difficulty,
            has_evaluation=task.evaluation is not None,
        )
