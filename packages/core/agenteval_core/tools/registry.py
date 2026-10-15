"""ToolRegistry and ToolSet.

The registry is every built-in tool. A ToolSet is the subset an agent version
allowlists: the only tools the model is shown and the only ones the runtime
will invoke. ToolSet.invoke turns every agent-level problem into an is_error
result (unknown tool, invalid input, denied, failed, timeout) and runs the
reaper after every executed tool call, whatever happened.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from agenteval_core.providers.base import ToolCall, ToolSpec
from agenteval_core.tools.base import Tool, ToolContext, ToolDenied, ToolFailed, ToolStatus
from agenteval_core.tools.builtin import BUILTIN_TOOLS, RunTests

REGISTRY: dict[str, type[Tool]] = {t.name: t for t in BUILTIN_TOOLS}

# What the model sees per tool result. The trajectory keeps more (up to the
# 64 KiB sandbox cap) for debugging.
MODEL_OUTPUT_LIMIT = 16 * 1024
STORED_OUTPUT_LIMIT = 64 * 1024


def unknown_tool_names(names: list[str]) -> list[str]:
    return sorted(set(names) - set(REGISTRY))


@dataclass(frozen=True)
class ToolInvocation:
    call_id: str
    tool: str
    status: ToolStatus
    model_content: str  # capped at MODEL_OUTPUT_LIMIT, marker appended if cut
    stored_content: str  # what the trajectory keeps, capped at STORED_OUTPUT_LIMIT
    input: dict[str, Any]
    exit_code: int | None
    duration_ms: int

    @property
    def is_error(self) -> bool:
        return self.status is not ToolStatus.OK


class ToolSet:
    def __init__(self, tools: list[Tool]) -> None:
        self._tools = {t.name: t for t in sorted(tools, key=lambda t: t.name)}

    @classmethod
    def build(cls, names: list[str], *, has_test_command: bool) -> ToolSet:
        """The version's allowlist, minus run_tests when the task has no tests
        (offering a tool that can only fail would waste the agent's steps)."""
        unknown = unknown_tool_names(names)
        if unknown:
            raise ValueError(f"unknown tools: {unknown}")
        selected = [
            REGISTRY[n]() for n in sorted(set(names)) if has_test_command or n != RunTests.name
        ]
        return cls(selected)

    @property
    def names(self) -> list[str]:
        return list(self._tools)

    def specs(self) -> list[ToolSpec]:
        """Sorted by name: a stable tool list keeps the prompt cache prefix stable."""
        return [ToolSpec(t.name, t.description, t.input_schema()) for t in self._tools.values()]

    async def invoke(self, ctx: ToolContext, call: ToolCall) -> ToolInvocation:
        started = time.monotonic()
        tool = self._tools.get(call.name)
        if tool is None:
            available = ", ".join(self._tools) or "none"
            return self._result(
                call, ToolStatus.UNKNOWN_TOOL,
                f"Unknown tool '{call.name}'. Available tools: {available}.", None, started,
            )  # fmt: skip
        try:
            args = tool.input_model.model_validate(call.input)
        except ValidationError as exc:
            return self._result(
                call, ToolStatus.INVALID_INPUT, _describe(exc), None, started
            )  # fmt: skip

        try:
            try:
                out = await tool.execute(ctx, args)
            except ToolDenied as exc:
                return self._result(call, ToolStatus.DENIED, str(exc), None, started)
            except ToolFailed as exc:
                return self._result(call, ToolStatus.ERROR, str(exc), None, started)
            return self._result(call, out.status, out.content, out.exit_code, started)
        finally:
            # Always, including timeouts, failures and exceptions: no process
            # outlives its tool call. Raises WorkspaceUnavailable on failure.
            await ctx.workspace.reap()

    @staticmethod
    def _result(
        call: ToolCall, status: ToolStatus, content: str, exit_code: int | None, started: float
    ) -> ToolInvocation:
        return ToolInvocation(
            call_id=call.id,
            tool=call.name,
            status=status,
            model_content=cap_for_model(content),
            stored_content=cap_for_model(content, STORED_OUTPUT_LIMIT),
            input=call.input,
            exit_code=exit_code,
            duration_ms=int((time.monotonic() - started) * 1000),
        )


def cap_for_model(content: str, limit: int = MODEL_OUTPUT_LIMIT) -> str:
    """Cut to `limit` UTF-8 bytes with an explicit marker (used for both caps)."""
    data = content.encode()
    if len(data) <= limit:
        return content
    head = data[:limit].decode("utf-8", errors="ignore")  # never split a character
    return f"{head}\n[output truncated: showing the first {limit} of {len(data)} bytes]"


def _describe(exc: ValidationError) -> str:
    problems = []
    for err in exc.errors(include_url=False)[:10]:
        where = ".".join(str(p) for p in err["loc"]) or "input"
        problems.append(f"{where}: {err['msg']}")
    return "Invalid input: " + "; ".join(problems)
