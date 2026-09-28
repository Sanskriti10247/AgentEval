"""Per-attempt runtime context."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agenteval_core.agent.limits import AgentLimits
from agenteval_core.tools.base import ToolContext


class CancelToken:
    """Cooperative cancellation with a reason (e.g. 'worker_shutdown').

    The runtime checks it between steps and races model calls, backoff sleeps
    and tool calls against it.
    """

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self.reason: str | None = None

    def cancel(self, reason: str) -> None:
        if not self._event.is_set():
            self.reason = reason
            self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    async def wait(self) -> None:
        await self._event.wait()


@dataclass(frozen=True)
class AgentContext:
    run_id: uuid.UUID
    attempt_id: uuid.UUID
    attempt_no: int
    limits: AgentLimits
    deadline: float  # clock() value at which the attempt ends
    tools: ToolContext
    cancel: CancelToken = field(default_factory=CancelToken)
    # Frozen agent-version config etc., recorded verbatim in run_started.
    config: dict[str, Any] = field(default_factory=dict)
    clock: Callable[[], float] = field(default=time.monotonic)

    def time_left_s(self) -> float:
        return self.deadline - self.clock()
