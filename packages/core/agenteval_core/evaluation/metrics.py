"""Trajectory metrics: how the agent worked, computed from stored events.

They feed scoring (reliability) and classification tags; they never decide
task success. Works on runtime TrajectoryEventData and on stored
TrajectoryEvent rows alike (same field names).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from agenteval_core.hashing import canonical_json
from agenteval_core.tools.base import ToolStatus
from agenteval_core.tools.builtin import TESTS_PASSED, RunTests

# Tool results that were the agent's mistake (not the platform's).
AGENT_TOOL_ERRORS = frozenset(
    {ToolStatus.INVALID_INPUT, ToolStatus.UNKNOWN_TOOL, ToolStatus.DENIED, ToolStatus.ERROR}
)
LOOP_STREAK = 3  # identical consecutive tool calls that count as a loop


class EventLike(Protocol):
    event_type: Any  # str or EventType (a StrEnum)
    tool: str | None
    status: str | None
    input: dict[str, Any] | None
    output: dict[str, Any] | None


class TrajectoryMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model_calls: int = 0
    model_errors: int = 0  # failed tries, i.e. retries the runtime recorded
    tool_calls: int = 0
    tool_status_counts: dict[str, int] = {}
    agent_tool_errors: int = 0
    tool_error_rate: float | None = None  # None when there were no tool calls
    max_identical_streak: int = 0
    loop_detected: bool = False
    ran_tests: bool = False
    last_tests_passed: bool | None = None
    limits_reached: list[str] = []


def compute(events: Iterable[EventLike]) -> TrajectoryMetrics:
    statuses: Counter[str] = Counter()
    model_calls = model_errors = 0
    streak = max_streak = 0
    previous: tuple[str, str] | None = None
    ran_tests = False
    last_tests: bool | None = None
    limits: list[str] = []
    for e in events:
        kind = str(e.event_type)
        if kind == "model_call":
            model_calls += 1
        elif kind == "model_error":
            model_errors += 1
        elif kind == "limit_reached":
            limits.append(str((e.output or {}).get("limit", "")))
        elif kind == "tool_call":
            statuses[str(e.status)] += 1
            key = (str(e.tool), canonical_json(e.input or {}))
            streak = streak + 1 if key == previous else 1
            previous = key
            max_streak = max(max_streak, streak)
            if e.tool == RunTests.name and e.status == ToolStatus.OK:
                ran_tests = True
                content = str((e.output or {}).get("content", ""))
                last_tests = content.startswith(TESTS_PASSED)
    tool_calls = sum(statuses.values())
    errors = sum(n for s, n in statuses.items() if s in AGENT_TOOL_ERRORS)
    return TrajectoryMetrics(
        model_calls=model_calls,
        model_errors=model_errors,
        tool_calls=tool_calls,
        tool_status_counts=dict(sorted(statuses.items())),
        agent_tool_errors=errors,
        tool_error_rate=round(errors / tool_calls, 5) if tool_calls else None,
        max_identical_streak=max_streak,
        loop_detected=max_streak >= LOOP_STREAK,
        ran_tests=ran_tests,
        last_tests_passed=last_tests,
        limits_reached=limits,
    )
