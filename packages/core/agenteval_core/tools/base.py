"""Tool contract: name, description, input_schema, execute, permissions.

A tool validates nothing itself: ToolSet validates the model's input against
`input_model` before calling `execute`. A tool reports agent-level problems
by raising ToolDenied / ToolFailed (they become is_error tool results the
agent can react to). Sandbox failures (WorkspaceUnavailable) and bugs
propagate: they are not the agent's fault.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from agenteval_core.tools.workspace import ExecOutput, Workspace

# Tool calls are refused, not attempted, when less than this remains.
MIN_TOOL_TIME_S = 0.5


class ToolStatus(StrEnum):
    """Mirrors trajectory_events.status (ck_trajectory_events_status)."""

    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"
    DENIED = "denied"
    INVALID_INPUT = "invalid_input"
    UNKNOWN_TOOL = "unknown_tool"


class Permission(StrEnum):
    """What a tool can do inside the sandbox. Declarative: which tools an
    agent version may use is the permission boundary (the allowlist)."""

    READ_FS = "read_fs"
    WRITE_FS = "write_fs"
    EXEC = "exec"


class ToolDenied(Exception):
    """Policy refused the call (e.g. argv[0] not on the shell allowlist)."""


class ToolFailed(Exception):
    """The tool could not do its job (e.g. file not found)."""


@dataclass(frozen=True)
class ToolContext:
    workspace: Workspace
    deadline: float  # time.monotonic() value at which the attempt ends
    tool_timeout_s: float
    shell_allowlist: frozenset[str] = frozenset()
    test_command: tuple[str, ...] | None = None
    clock: Callable[[], float] = field(default=time.monotonic)

    def time_budget_s(self) -> float:
        return min(self.tool_timeout_s, self.deadline - self.clock())


@dataclass(frozen=True)
class ToolOutput:
    content: str
    status: ToolStatus = ToolStatus.OK
    exit_code: int | None = None


class ToolInput(BaseModel):
    """Base for tool inputs: unknown fields are rejected, which also makes the
    JSON schema carry additionalProperties: false (needed for strict tool use)."""

    model_config = ConfigDict(extra="forbid")


class Tool(ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    input_model: ClassVar[type[ToolInput]]
    permission: ClassVar[Permission]

    @classmethod
    def input_schema(cls) -> dict[str, Any]:
        return _strip_titles(cls.input_model.model_json_schema())

    @abstractmethod
    async def execute(self, ctx: ToolContext, args: Any) -> ToolOutput: ...


async def run_exec(ctx: ToolContext, argv: list[str]) -> ExecOutput | ToolOutput:
    """Exec within the call's time budget. Returns a TIMEOUT ToolOutput
    instead of executing when there is no meaningful time left."""
    budget = ctx.time_budget_s()
    if budget < MIN_TOOL_TIME_S:
        return ToolOutput("Not run: the attempt has no time left.", ToolStatus.TIMEOUT)
    return await ctx.workspace.exec(argv, timeout_s=budget)


def format_exec(out: ExecOutput, *, timeout_s: float | None = None) -> str:
    """Render an exec for the model: stdout, then labelled stderr, then notes."""
    parts: list[str] = []
    if out.stdout:
        parts.append(out.stdout.rstrip("\n"))
    if out.stderr:
        parts.append("[stderr]\n" + out.stderr.rstrip("\n"))
    notes: list[str] = []
    if out.stdout_truncated or out.stderr_truncated:
        notes.append("[output truncated at the sandbox limit]")
    if out.timed_out:
        limit = f" after {timeout_s:g}s" if timeout_s else ""
        notes.append(f"[timed out{limit}; the command and its processes were killed]")
    elif out.exit_code not in (0, None):
        notes.append(f"[exit code {out.exit_code}]")
    if not parts and not notes:
        return "(no output)"
    return "\n".join(parts + notes)


def _strip_titles(node: Any, *, is_properties: bool = False) -> Any:
    """Pydantic adds a 'title' keyword everywhere; it's noise in the model's
    context. Keys of a 'properties' map are field names, never keywords, so a
    field that is itself called 'title' survives."""
    if isinstance(node, dict):
        return {
            k: _strip_titles(v, is_properties=(k == "properties" and not is_properties))
            for k, v in node.items()
            if is_properties or k != "title"
        }
    if isinstance(node, list):
        return [_strip_titles(v) for v in node]
    return node
