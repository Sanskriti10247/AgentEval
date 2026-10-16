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

    Runtime guard (design §6 rule 4; ADR-008 D): `set_deadline` installs an
    optional deadline callback. Once `clock() >= fence_at`, `cancelled`
    reports True even if nothing has called `cancel()` yet - a backstop for
    the moments between steps, in case the lease manager's own watch loop
    (which actually calls `cancel("lease_lost")`, waking any in-flight
    `wait()`) is itself delayed. Not monkey-patched: this is a plain
    attribute the property checks.
    """

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self.reason: str | None = None
        self._fence_at: float | None = None
        self._clock: Callable[[], float] = time.monotonic

    def cancel(self, reason: str) -> None:
        if not self._event.is_set():
            self.reason = reason
            self._event.set()

    def set_deadline(self, fence_at: float, clock: Callable[[], float] = time.monotonic) -> None:
        """Install (or move) the self-fence deadline. Moving it is how the
        lease manager reports a later `fence_at` after a successful renewal;
        it never needs to move backwards in practice (fence_at only grows)."""
        self._fence_at = fence_at
        self._clock = clock

    @property
    def cancelled(self) -> bool:
        past_deadline = self._fence_at is not None and self._clock() >= self._fence_at
        if not self._event.is_set() and past_deadline:
            # The deadline fired before anything called cancel() for us (the
            # lease manager's own watch loop is the normal path - this is the
            # backstop). Cancelling for real here also wakes any in-flight
            # wait(), consistently with every other cancellation.
            self.cancel("lease_lost")
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
