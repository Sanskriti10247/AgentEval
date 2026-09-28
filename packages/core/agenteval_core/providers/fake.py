"""Deterministic scripted model for tests, E2E runs and infra benchmarks.

A script is a list of steps consumed one per next() call:

  {"type": "turn", "text": "...", "tool_calls": [{"name": "read_file", "input": {...}}],
   "stop": "tool_use", "usage": {...}, "delay_s": 0.0,
   "expect_tool_results_contain": ["FAILED"]}
  {"type": "error", "kind": "overloaded", "retry_after_s": 1.0}

It is JSON-serializable so an agent version can carry it in model_settings
({"script": [...]}) and run end to end with no network and no cost.

Everything is deterministic: tool-call ids (toolu_fake_1, 2, ...), request
ids, usage (scripted or fixed defaults) and latency (the scripted delay).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agenteval_core.providers.base import (
    ModelError,
    ModelErrorKind,
    ModelTurn,
    PendingToolCalls,
    ProtocolViolation,
    StopReason,
    ToolCall,
    ToolResultMsg,
    ToolSpec,
    Usage,
)

MAX_SCRIPT_STEPS = 1000


class FakeScriptError(Exception):
    """The script itself can't continue (exhausted, or an expectation failed).

    Deliberately not a ModelError: it signals a broken test fixture, so the
    runtime treats it as an unexpected exception (INTERNAL_ERROR), never as a
    retryable provider fault.
    """


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FakeUsage(_Strict):
    input_tokens: int = Field(default=100, ge=0)
    output_tokens: int = Field(default=20, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)


class FakeToolCall(_Strict):
    name: str = Field(min_length=1, max_length=64)
    input: dict[str, Any] = Field(default_factory=dict)


class TurnStep(_Strict):
    type: Literal["turn"] = "turn"
    text: str = ""
    tool_calls: list[FakeToolCall] = Field(default_factory=list)
    stop: StopReason | None = None  # default: tool_use if tool_calls else end_turn
    stop_detail: str | None = None
    usage: FakeUsage = Field(default_factory=FakeUsage)
    delay_s: float = Field(default=0.0, ge=0, le=3600)
    # Assert the latest tool results (joined) contain each substring.
    expect_tool_results_contain: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent_stop(self) -> TurnStep:
        stop = self.effective_stop
        if stop is StopReason.TOOL_USE and not self.tool_calls:
            raise ValueError("stop=tool_use needs at least one tool call")
        if stop in (StopReason.END_TURN, StopReason.REFUSAL) and self.tool_calls:
            raise ValueError(f"stop={stop} cannot carry tool calls")
        return self

    @property
    def effective_stop(self) -> StopReason:
        if self.stop is not None:
            return self.stop
        return StopReason.TOOL_USE if self.tool_calls else StopReason.END_TURN


class ErrorStep(_Strict):
    type: Literal["error"] = "error"
    kind: ModelErrorKind
    message: str = "scripted error"
    status: int | None = None
    retry_after_s: float | None = Field(default=None, ge=0)
    delay_s: float = Field(default=0.0, ge=0, le=3600)


ScriptStep = Annotated[TurnStep | ErrorStep, Field(discriminator="type")]


class FakeScript(_Strict):
    """The shape of an agent version's model_settings for provider 'fake'."""

    script: list[ScriptStep] = Field(min_length=1, max_length=MAX_SCRIPT_STEPS)


@dataclass(frozen=True)
class FakeRequest:
    """What the runtime asked for on one next() call (for test assertions)."""

    max_tokens: int
    timeout_s: float
    transcript_len: int


class FakeSession:
    def __init__(
        self,
        script: FakeScript,
        *,
        model: str,
        system: str,
        tools: list[ToolSpec],
        task_prompt: str,
    ) -> None:
        self.model = model
        self.system = system
        self.tools = list(tools)
        # Neutral, append-only record of the conversation (for assertions).
        self.transcript: list[dict[str, Any]] = [{"role": "user", "text": task_prompt}]
        self.requests: list[FakeRequest] = []
        self._steps = script.script
        self._cursor = 0
        self._tool_call_seq = 0
        self._pending = PendingToolCalls()

    @property
    def steps_consumed(self) -> int:
        return self._cursor

    async def next(self, *, max_tokens: int, timeout_s: float) -> ModelTurn:
        self._pending.check_can_call_model()
        if max_tokens < 1 or timeout_s <= 0:
            raise ProtocolViolation(f"invalid call: max_tokens={max_tokens} timeout_s={timeout_s}")
        self.requests.append(FakeRequest(max_tokens, timeout_s, len(self.transcript)))
        if self._cursor >= len(self._steps):
            raise FakeScriptError(f"script exhausted after {len(self._steps)} steps")
        step = self._steps[self._cursor]
        self._cursor += 1
        n = self._cursor

        if step.delay_s:
            if step.delay_s > timeout_s:
                # Mirrors a client-side request timeout: we waited timeout_s, then gave up.
                await asyncio.sleep(timeout_s)
                raise ModelError(
                    ModelErrorKind.TIMEOUT,
                    f"request timed out after {timeout_s}s",
                    request_id=f"req_fake_{n}",
                )
            await asyncio.sleep(step.delay_s)

        if isinstance(step, ErrorStep):
            raise ModelError(
                step.kind,
                step.message,
                status=step.status,
                retry_after_s=step.retry_after_s,
                request_id=f"req_fake_{n}",
            )

        self._check_expectations(step)
        calls = tuple(self._make_call(c) for c in step.tool_calls)
        self.transcript.append(
            {"role": "assistant", "text": step.text, "tool_calls": [c.id for c in calls]}
        )
        if calls:  # every tool call must be answered, whatever the stop reason
            self._pending.expect(calls)
        u = step.usage
        return ModelTurn(
            text=step.text,
            tool_calls=calls,
            stop=step.effective_stop,
            stop_detail=step.stop_detail,
            usage=Usage(u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens),
            model=self.model,
            latency_ms=int(step.delay_s * 1000),
            request_id=f"req_fake_{n}",
        )

    def add_tool_results(self, results: list[ToolResultMsg]) -> None:
        self._pending.resolve(results)
        self.transcript.append(
            {
                "role": "user",
                "tool_results": [
                    {"id": r.tool_call_id, "content": r.content, "is_error": r.is_error}
                    for r in results
                ],
            }
        )

    def add_user_text(self, text: str) -> None:
        self._pending.check_can_add_user_text()
        self.transcript.append({"role": "user", "text": text})

    def _make_call(self, c: FakeToolCall) -> ToolCall:
        self._tool_call_seq += 1
        return ToolCall(id=f"toolu_fake_{self._tool_call_seq}", name=c.name, input=dict(c.input))

    def _check_expectations(self, step: TurnStep) -> None:
        if not step.expect_tool_results_contain:
            return
        last = self.transcript[-1]
        if "tool_results" not in last:
            raise FakeScriptError(f"step {self._cursor}: expected tool results, got {last}")
        joined = "\n".join(r["content"] for r in last["tool_results"])
        missing = [s for s in step.expect_tool_results_contain if s not in joined]
        if missing:
            raise FakeScriptError(
                f"step {self._cursor}: tool results missing {missing}; got {joined[:500]!r}"
            )


class FakeProvider:
    """Serves scripted sessions. Pass a script directly (unit tests) or let
    each start() parse it from the agent version's model_settings."""

    name = "fake"

    def __init__(self, script: FakeScript | None = None) -> None:
        self._script = script
        self.sessions: list[FakeSession] = []

    def start(
        self,
        *,
        model: str,
        system: str,
        tools: list[ToolSpec],
        task_prompt: str,
        settings: dict[str, Any],
    ) -> FakeSession:
        script = self._script or FakeScript.model_validate(settings)
        session = FakeSession(
            script, model=model, system=system, tools=tools, task_prompt=task_prompt
        )
        self.sessions.append(session)
        return session
