"""Trajectory events as the runtime produces them (one row of
trajectory_events each). Operational data only: no chain-of-thought."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

from agenteval_core.providers.base import Usage


class EventType(StrEnum):
    """Mirrors ck_trajectory_events_event_type."""

    RUN_STARTED = "run_started"
    MODEL_CALL = "model_call"
    MODEL_ERROR = "model_error"
    TOOL_CALL = "tool_call"
    LIMIT_REACHED = "limit_reached"
    RUN_FINISHED = "run_finished"


@dataclass(frozen=True)
class TrajectoryEventData:
    seq: int
    step: int
    event_type: EventType
    tool: str | None = None
    status: str | None = None
    error_class: str | None = None
    input: dict[str, Any] | None = None
    output: dict[str, Any] | None = None
    usage: Usage | None = None
    cost_usd: Decimal | None = None
    duration_ms: int | None = None


class EventSink(Protocol):
    async def append(self, events: Sequence[TrajectoryEventData]) -> None:
        """Durably record events, in order. Raises if they could not be stored."""
        ...


class SinkFailed(Exception):
    """The trajectory could not be recorded. The run stops: continuing would
    spend money on work nobody can inspect."""
