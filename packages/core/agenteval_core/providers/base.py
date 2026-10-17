"""Provider-neutral model interface used by the agent runtime.

The provider owns the conversation transcript (a ModelSession). The runtime
only hands it neutral values (tool results, text) and gets neutral values
back (ModelTurn). This keeps provider-specific message types - and any
opaque blocks that must be replayed verbatim, such as thinking blocks -
inside the provider, and makes the history append-only by construction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol


@dataclass(frozen=True)
class ToolSpec:
    """A tool as the model sees it."""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class ToolResultMsg:
    tool_call_id: str
    content: str
    is_error: bool = False


class StopReason(StrEnum):
    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"
    REFUSAL = "refusal"
    OTHER = "other"  # anything else the provider reported; see ModelTurn.stop_detail


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0  # uncached input
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total_input_tokens(self) -> int:
        """Everything the model read this call (what the token budget counts)."""
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    @property
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.output_tokens

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
        )


@dataclass(frozen=True)
class ModelTurn:
    text: str
    tool_calls: tuple[ToolCall, ...]
    stop: StopReason
    usage: Usage
    model: str  # the model that actually served the call
    latency_ms: int
    stop_detail: str | None = None  # refusal category, or the raw stop reason for OTHER
    request_id: str | None = None


class ModelErrorKind(StrEnum):
    RATE_LIMITED = "rate_limited"  # 429
    OVERLOADED = "overloaded"  # 529
    SERVER_ERROR = "server_error"  # other 5xx
    TIMEOUT = "timeout"  # request timed out (408 or client-side)
    CONNECTION = "connection"  # network failure
    CONFLICT = "conflict"  # 409
    BAD_REQUEST = "bad_request"  # 400/422: our request is wrong
    AUTHENTICATION = "authentication"  # 401
    PERMISSION = "permission"  # 403
    NOT_FOUND = "not_found"  # 404, e.g. unknown model
    REQUEST_TOO_LARGE = "request_too_large"  # 413


# The single source of truth for which provider errors are worth retrying.
RETRYABLE_KINDS: frozenset[ModelErrorKind] = frozenset(
    {
        ModelErrorKind.RATE_LIMITED,
        ModelErrorKind.OVERLOADED,
        ModelErrorKind.SERVER_ERROR,
        ModelErrorKind.TIMEOUT,
        ModelErrorKind.CONNECTION,
        ModelErrorKind.CONFLICT,
    }
)


class ModelError(Exception):
    """A provider call failed. Providers translate their SDK errors into this."""

    def __init__(
        self,
        kind: ModelErrorKind,
        message: str,
        *,
        status: int | None = None,
        retry_after_s: float | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.message = message
        self.status = status
        self.retry_after_s = retry_after_s
        self.request_id = request_id

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE_KINDS


class ProtocolViolation(Exception):
    """The runtime used a ModelSession incorrectly (a bug, never a model error).

    Examples: calling next() while tool results are still owed, or answering
    tool calls that were never made. A real API would reject these with a 400;
    sessions raise this instead so the bug is caught in tests.
    """


@dataclass
class PendingToolCalls:
    """Tracks the tool calls the model is waiting on. Shared by providers so
    every session enforces the same transcript rules."""

    ids: tuple[str, ...] = field(default_factory=tuple)

    def expect(self, calls: tuple[ToolCall, ...]) -> None:
        self.ids = tuple(c.id for c in calls)

    def check_can_call_model(self) -> None:
        if self.ids:
            raise ProtocolViolation(f"tool results owed for {list(self.ids)} before next()")

    def check_can_add_user_text(self) -> None:
        if self.ids:
            raise ProtocolViolation("cannot add user text while tool results are owed")

    def resolve(self, results: list[ToolResultMsg]) -> None:
        got = [r.tool_call_id for r in results]
        if not self.ids:
            raise ProtocolViolation(f"no tool calls are pending, got results for {got}")
        if len(got) != len(set(got)) or set(got) != set(self.ids):
            raise ProtocolViolation(
                f"tool results must answer exactly {list(self.ids)} once each, got {got}"
            )
        self.ids = ()


class ModelSession(Protocol):
    """One conversation. Append-only: nothing already sent can be edited."""

    async def next(self, *, max_tokens: int, timeout_s: float) -> ModelTurn:
        """Call the model with the transcript so far. Raises ModelError."""
        ...

    def add_tool_results(self, results: list[ToolResultMsg]) -> None:
        """Answer every tool call of the previous turn, in one message."""
        ...

    def add_user_text(self, text: str) -> None:
        """Only for runtime nudges, e.g. 'you were cut off; continue'."""
        ...


class ModelProvider(Protocol):
    name: str

    def start(
        self,
        *,
        model: str,
        system: str,
        tools: list[ToolSpec],
        task_prompt: str,
        settings: dict[str, Any],
        attempt_no: int = 1,
    ) -> ModelSession:
        """attempt_no is the run attempt (1-based). Real providers ignore it;
        the scripted fake uses it to fail only some attempts."""
        ...
