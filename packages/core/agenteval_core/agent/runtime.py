"""The agent loop.

    run_started
    loop:
        stop if cancelled / past the deadline / over a limit
        model call (in-step retries on retryable provider errors)  -> model_call
        refusal -> MODEL_REFUSAL      no tool calls -> AGENT_FINISHED
        run each tool call, sequentially                            -> tool_call
        answer all tool calls in one message
    run_finished

Every exit goes through run_finished, so the trajectory always says how and
why the attempt ended. Events are flushed to the sink after run_started,
after each model response (before tools run), after each step's tool calls,
and at the end.

Deadline vs. provider fault: a healthy model that is still thinking when the
deadline passes is the agent running out of time (TIMEOUT, COMPLETED).
Provider errors that exhaust the retries or the remaining time are platform
faults (MODEL_ERROR, FAILED): the agent was never fairly tested.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any, TypeVar

from agenteval_core.agent.context import AgentContext
from agenteval_core.agent.events import EventSink, EventType, SinkFailed, TrajectoryEventData
from agenteval_core.agent.limits import BudgetTracker, Limit
from agenteval_core.agent.pricing import ModelPrice, cost_usd
from agenteval_core.agent.prompts import RUNTIME_VERSION, TRUNCATED_TOOL_INPUT, TRUNCATION_NUDGE
from agenteval_core.agent.result import AgentResult
from agenteval_core.agent.retry import GiveUp, Retry, RetryPolicy, next_model_retry
from agenteval_core.logging import get_logger
from agenteval_core.providers.base import (
    ModelError,
    ModelErrorKind,
    ModelSession,
    ModelTurn,
    StopReason,
    ToolCall,
    ToolResultMsg,
    Usage,
)
from agenteval_core.states import Outcome
from agenteval_core.tools.base import ToolStatus
from agenteval_core.tools.registry import (
    STORED_OUTPUT_LIMIT,
    ToolInvocation,
    ToolSet,
    cap_for_model,
)
from agenteval_core.tools.workspace import WorkspaceUnavailable

log = get_logger(__name__)
T = TypeVar("T")

# A tool call bounds itself by the deadline (in-container `timeout`); this
# grace covers kill-after, Docker's stream drain and the reaper.
TOOL_DEADLINE_GRACE_S = 15.0
# How long to wait for an aborted tool call to unwind after killing the sandbox.
ABORT_WAIT_S = 10.0

_LIMIT_OUTCOME = {
    Limit.MAX_STEPS: Outcome.MAX_STEPS,
    Limit.MAX_TOTAL_TOKENS: Outcome.BUDGET_EXCEEDED,
    Limit.MAX_COST_USD: Outcome.BUDGET_EXCEEDED,
}


class _Stop(Exception):
    """Internal: the attempt ends with this outcome."""

    def __init__(self, outcome: Outcome, reason: str | None) -> None:
        super().__init__(reason)
        self.outcome = outcome
        self.reason = reason


class AgentRuntime:
    def __init__(
        self,
        *,
        session: ModelSession,
        toolset: ToolSet,
        price: ModelPrice,
        sink: EventSink,
        retry_policy: RetryPolicy | None = None,
        rng: random.Random | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._session = session
        self._toolset = toolset
        self._price = price
        self._sink = sink
        self._retry = retry_policy or RetryPolicy()
        self._rng = rng or random.Random()
        self._sleep = sleep
        self._seq = 0
        self._step = 0
        self._pending: list[TrajectoryEventData] = []

    # -- entry point -------------------------------------------------------------------

    async def run(self, ctx: AgentContext) -> AgentResult:
        started = ctx.clock()
        budget = BudgetTracker(ctx.limits)
        final_answer: str | None = None
        try:
            self._emit(EventType.RUN_STARTED, input=self._run_started_payload(ctx))
            await self._flush()
            final_answer = await self._loop(ctx, budget)
            outcome, reason = Outcome.AGENT_FINISHED, None
        except _Stop as stop:
            outcome, reason = stop.outcome, stop.reason
        except SinkFailed:
            raise
        except Exception as exc:  # a bug, a broken fixture, an unexpected provider failure
            log.exception("agent_runtime_error", step=self._step)
            outcome, reason = Outcome.INTERNAL_ERROR, f"{type(exc).__name__}: {exc}"

        result = AgentResult(
            outcome=outcome,
            reason=reason,
            final_answer=final_answer,
            steps=budget.steps,
            usage=budget.usage,
            cost_usd=budget.cost_usd,
            duration_ms=int((ctx.clock() - started) * 1000),
        )
        self._emit(
            EventType.RUN_FINISHED,
            output={
                "outcome": outcome.value,
                "reason": reason,
                "final_answer": _cap(final_answer),
                "steps": result.steps,
                "usage": _usage_dict(result.usage),
                "cost_usd": str(result.cost_usd),
            },
            duration_ms=result.duration_ms,
        )
        await self._flush()
        log.info(
            "agent_finished",
            outcome=outcome,
            reason=reason,
            steps=result.steps,
            tokens=result.usage.total_tokens,
            cost_usd=str(result.cost_usd),
        )
        return result

    # -- the loop ----------------------------------------------------------------------

    async def _loop(self, ctx: AgentContext, budget: BudgetTracker) -> str:
        while True:
            await self._stop_if_interrupted(ctx)
            limit = budget.exceeded()
            if limit is not None:
                await self._limit_reached(limit.value, budget)
                raise _Stop(_LIMIT_OUTCOME[limit], limit.value)

            self._step += 1
            turn = await self._call_model(ctx, budget)
            cost = cost_usd(self._price, turn.usage)
            budget.record_call(turn.usage, cost)
            self._emit_model_call(turn, cost)
            await self._flush()  # before tools run: a hang still shows what was requested

            if turn.stop is StopReason.REFUSAL:
                raise _Stop(Outcome.MODEL_REFUSAL, f"refusal: {turn.stop_detail or 'unspecified'}")
            if turn.stop is StopReason.OTHER:
                raise _Stop(Outcome.MODEL_ERROR, f"unexpected stop reason: {turn.stop_detail}")
            if turn.tool_calls:
                results = await self._run_tools(ctx, turn)
                self._session.add_tool_results(results)
                continue
            if turn.stop is StopReason.MAX_TOKENS:
                self._session.add_user_text(TRUNCATION_NUDGE)
                continue
            return turn.text

    # -- model calls -------------------------------------------------------------------

    async def _call_model(self, ctx: AgentContext, budget: BudgetTracker) -> ModelTurn:
        retries = 0
        while True:
            await self._stop_if_interrupted(ctx)
            time_left = ctx.time_left_s()
            timeout_s = min(ctx.limits.model_call_timeout_s, time_left)
            deadline_bound = timeout_s >= time_left
            max_tokens = budget.max_tokens_for_next_call()
            try:
                return await self._race(
                    ctx,
                    self._session.next(max_tokens=max_tokens, timeout_s=timeout_s),
                    timeout_s=time_left,
                )
            except ModelError as err:
                if err.kind is ModelErrorKind.TIMEOUT and deadline_bound:
                    # The call was only cut short because the attempt's time ran out.
                    await self._limit_reached("deadline", budget)
                    raise _Stop(Outcome.TIMEOUT, "deadline") from err
                decision = next_model_retry(err, retries, self._retry, ctx.time_left_s(), self._rng)
                self._emit_model_error(err, retries, decision)
                await self._flush()
                if isinstance(decision, GiveUp):
                    raise _Stop(
                        Outcome.MODEL_ERROR, f"{err.kind} ({decision.reason}): {err.message}"
                    ) from err
                await self._race(ctx, self._sleep(decision.delay_s), timeout_s=None)
                retries += 1

    # -- tools -------------------------------------------------------------------------

    async def _run_tools(self, ctx: AgentContext, turn: ModelTurn) -> list[ToolResultMsg]:
        results: list[ToolResultMsg] = []
        for call in turn.tool_calls:
            if turn.stop is StopReason.MAX_TOKENS:
                inv = _not_run(call, ToolStatus.INVALID_INPUT, TRUNCATED_TOOL_INPUT)
            else:
                inv = await self._invoke(ctx, call)
            self._emit_tool_call(inv)
            results.append(ToolResultMsg(call.id, inv.model_content, inv.is_error))
        await self._flush()
        return results

    async def _invoke(self, ctx: AgentContext, call: ToolCall) -> ToolInvocation:
        await self._stop_if_interrupted(ctx)
        task = asyncio.ensure_future(self._toolset.invoke(ctx.tools, call))
        waiter = asyncio.ensure_future(ctx.cancel.wait())
        backstop = max(ctx.time_left_s(), 0) + TOOL_DEADLINE_GRACE_S
        try:
            done, _ = await asyncio.wait(
                {task, waiter}, timeout=backstop, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            waiter.cancel()
        if task in done:
            try:
                return task.result()
            except WorkspaceUnavailable as exc:
                self._emit_tool_call(_not_run(call, ToolStatus.ERROR, str(exc)),
                                     error_class="WorkspaceUnavailable")  # fmt: skip
                raise _Stop(Outcome.SANDBOX_ERROR, str(exc)) from exc

        # Cancelled, or the call overran the deadline: kill the sandbox so the
        # exec ends, give it a moment to unwind, and record what happened.
        await ctx.tools.workspace.abort()
        await _settle(task, ABORT_WAIT_S)
        why = f"cancelled: {ctx.cancel.reason}" if ctx.cancel.cancelled else "deadline"
        self._emit_tool_call(_not_run(call, ToolStatus.ERROR, f"interrupted ({why})"),
                             error_class="Interrupted")  # fmt: skip
        if ctx.cancel.cancelled:
            raise _Stop(Outcome.INTERNAL_ERROR, why)
        raise _Stop(Outcome.TIMEOUT, "deadline")

    # -- interruption ------------------------------------------------------------------

    async def _stop_if_interrupted(self, ctx: AgentContext) -> None:
        if ctx.cancel.cancelled:
            # Phase 3's only trigger is worker shutdown: a platform
            # interruption, not the agent's doing.
            raise _Stop(Outcome.INTERNAL_ERROR, f"cancelled: {ctx.cancel.reason}")
        if ctx.time_left_s() <= 0:
            await self._limit_reached("deadline", None)
            raise _Stop(Outcome.TIMEOUT, "deadline")

    async def _race(self, ctx: AgentContext, aw: Awaitable[T], *, timeout_s: float | None) -> T:
        """Await `aw`, but give up if the run is cancelled or timeout_s passes."""
        task = asyncio.ensure_future(aw)
        waiter = asyncio.ensure_future(ctx.cancel.wait())
        try:
            done, _ = await asyncio.wait(
                {task, waiter},
                timeout=None if timeout_s is None else max(timeout_s, 0),
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            waiter.cancel()
        if task in done:
            return task.result()
        task.cancel()
        await _settle(task, ABORT_WAIT_S)
        await self._stop_if_interrupted(ctx)
        # The timer fired a hair before the clock reached the deadline.
        await self._limit_reached("deadline", None)
        raise _Stop(Outcome.TIMEOUT, "deadline")

    async def _limit_reached(self, limit: str, budget: BudgetTracker | None) -> None:
        used: dict[str, Any] = {}
        if budget is not None:
            used = {
                "steps": budget.steps,
                "tokens": budget.tokens_used,
                "cost_usd": str(budget.cost_usd),
            }
        self._emit(EventType.LIMIT_REACHED, output={"limit": limit, **used})
        await self._flush()

    # -- events ------------------------------------------------------------------------

    def _emit(self, event_type: EventType, **fields: Any) -> None:
        self._pending.append(
            TrajectoryEventData(seq=self._seq, step=self._step, event_type=event_type, **fields)
        )
        self._seq += 1

    async def _flush(self) -> None:
        if not self._pending:
            return
        batch, self._pending = self._pending, []
        try:
            await self._sink.append(batch)
        except Exception as exc:
            raise SinkFailed(f"could not record trajectory events: {exc}") from exc

    def _run_started_payload(self, ctx: AgentContext) -> dict[str, Any]:
        return {
            "runtime_version": RUNTIME_VERSION,
            "run_id": str(ctx.run_id),
            "attempt_no": ctx.attempt_no,
            "tools": self._toolset.names,
            "limits": ctx.limits.model_dump(mode="json"),
            "config": ctx.config,
        }

    def _emit_model_call(self, turn: ModelTurn, cost: Decimal) -> None:
        self._emit(
            EventType.MODEL_CALL,
            output={
                "model": turn.model,
                "stop": turn.stop.value,
                "stop_detail": turn.stop_detail,
                "text": _cap(turn.text),
                "tool_calls": [
                    {"id": c.id, "name": c.name, "input": _cap_json(c.input)}
                    for c in turn.tool_calls
                ],
                "request_id": turn.request_id,
            },
            usage=turn.usage,
            cost_usd=cost,
            duration_ms=turn.latency_ms,
        )

    def _emit_model_error(self, err: ModelError, retries: int, decision: Retry | GiveUp) -> None:
        self._emit(
            EventType.MODEL_ERROR,
            error_class=err.kind.value,
            output={
                "message": err.message,
                "status": err.status,
                "retryable": err.retryable,
                "retry_no": retries,
                "decision": "retry" if isinstance(decision, Retry) else "give_up",
                "give_up_reason": decision.reason if isinstance(decision, GiveUp) else None,
                "wait_s": round(decision.delay_s, 3) if isinstance(decision, Retry) else None,
                "retry_after_s": err.retry_after_s,
                "request_id": err.request_id,
            },
        )

    def _emit_tool_call(self, inv: ToolInvocation, error_class: str | None = None) -> None:
        self._emit(
            EventType.TOOL_CALL,
            tool=inv.tool,
            status=inv.status.value,
            error_class=error_class,
            input=_cap_json(inv.input),
            output={
                "call_id": inv.call_id,
                "content": inv.stored_content,
                "exit_code": inv.exit_code,
                "model_truncated": inv.model_content != inv.stored_content,
            },
            duration_ms=inv.duration_ms,
        )


# -- helpers -------------------------------------------------------------------------------


def _not_run(call: ToolCall, status: ToolStatus, content: str) -> ToolInvocation:
    return ToolInvocation(
        call_id=call.id,
        tool=call.name,
        status=status,
        model_content=content,
        stored_content=content,
        input=call.input,
        exit_code=None,
        duration_ms=0,
    )


async def _settle(task: asyncio.Future[Any], timeout_s: float) -> None:
    """Wait (bounded) for a cancelled/aborted task to finish, and retrieve its
    exception so asyncio doesn't warn. Never raises the task's error, and
    never swallows cancellation of the caller."""
    if not task.done():
        await asyncio.wait({task}, timeout=timeout_s)
    if not task.done():
        task.cancel()
    elif not task.cancelled():
        task.exception()


def _cap(text: str | None) -> str | None:
    return None if text is None else cap_for_model(text, STORED_OUTPUT_LIMIT)


def _cap_json(value: dict[str, Any]) -> dict[str, Any]:
    """Tool inputs can be large (write_file content). Keep them readable but
    bounded in the trajectory."""
    raw = json.dumps(value, ensure_ascii=False, default=str)
    if len(raw.encode()) <= STORED_OUTPUT_LIMIT:
        return value
    return {"_truncated": True, "preview": cap_for_model(raw, STORED_OUTPUT_LIMIT)}


def _usage_dict(u: Usage) -> dict[str, int]:
    return {
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "cache_read_tokens": u.cache_read_tokens,
        "cache_write_tokens": u.cache_write_tokens,
    }
