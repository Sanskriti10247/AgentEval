"""The agent loop, driven by FakeProvider scripts against a scripted
workspace. No Docker, no network; backoff sleeps are recorded, not slept."""

import asyncio
import random
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pytest

from agenteval_core.agent.context import AgentContext, CancelToken
from agenteval_core.agent.events import EventType, SinkFailed, TrajectoryEventData
from agenteval_core.agent.limits import AgentLimits
from agenteval_core.agent.pricing import price_for
from agenteval_core.agent.prompts import (
    RUNTIME_PREAMBLE,
    RUNTIME_VERSION,
    TRUNCATED_TOOL_INPUT,
    TRUNCATION_NUDGE,
    build_system_prompt,
)
from agenteval_core.agent.result import AgentResult
from agenteval_core.agent.retry import RetryPolicy
from agenteval_core.agent.runtime import AgentRuntime
from agenteval_core.providers.base import Usage
from agenteval_core.providers.fake import FakeProvider, FakeSession
from agenteval_core.reliability.taxonomy import FailureReason
from agenteval_core.states import Outcome, status_for
from agenteval_core.tools.base import ToolContext
from agenteval_core.tools.registry import REGISTRY, ToolSet
from agenteval_core.tools.workspace import ExecOutput, WorkspaceUnavailable

PRICE = price_for("fake", "fake-model")
assert PRICE is not None
DEFAULT_CALL_COST = Decimal("0.000200")  # 100 in x $1/MTok + 20 out x $5/MTok


@dataclass
class ScriptedWorkspace:
    """exec() returns `result` (or raises it); hang=True blocks until abort()."""

    result: ExecOutput | Exception = field(default_factory=lambda: ExecOutput(0, "file body\n", ""))
    hang: bool = False
    execs: list[list[str]] = field(default_factory=list)
    reaps: int = 0
    aborts: int = 0
    _aborted: asyncio.Event = field(default_factory=asyncio.Event)

    async def exec(self, argv: list[str], *, timeout_s: float) -> ExecOutput:
        self.execs.append(argv)
        if self.hang:
            await self._aborted.wait()
            raise WorkspaceUnavailable("sandbox killed")
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def write_files(self, files: dict[str, str]) -> ExecOutput:
        return ExecOutput(0, "", "")

    async def reap(self) -> None:
        self.reaps += 1

    async def abort(self) -> None:
        self.aborts += 1
        self._aborted.set()


@dataclass
class ListSink:
    batches: list[list[TrajectoryEventData]] = field(default_factory=list)
    fail: bool = False

    async def append(self, events: Sequence[TrajectoryEventData]) -> None:
        if self.fail:
            raise ConnectionError("postgres down")
        self.batches.append(list(events))

    @property
    def events(self) -> list[TrajectoryEventData]:
        return [e for b in self.batches for e in b]

    def types(self) -> list[str]:
        return [e.event_type.value for e in self.events]


@dataclass
class Harness:
    runtime: AgentRuntime
    ctx: AgentContext
    session: FakeSession
    sink: ListSink
    ws: ScriptedWorkspace
    sleeps: list[float]

    async def run(self) -> AgentResult:
        return await self.runtime.run(self.ctx)


def harness(
    *steps: dict[str, Any],
    limits: AgentLimits | None = None,
    time_left: float = 600,
    ws: ScriptedWorkspace | None = None,
    tools: list[str] | None = None,
    allow: frozenset[str] = frozenset({"python"}),
) -> Harness:
    ws = ws or ScriptedWorkspace()
    toolset = ToolSet.build(tools or sorted(REGISTRY), has_test_command=True)
    session = FakeProvider().start(
        model="fake-model",
        system=build_system_prompt("You fix bugs."),
        tools=toolset.specs(),
        task_prompt="Make the tests pass.",
        settings={"script": list(steps)},
    )
    sink = ListSink()
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    deadline = time.monotonic() + time_left
    limits = limits or AgentLimits()
    ctx = AgentContext(
        run_id=uuid.uuid4(),
        attempt_id=uuid.uuid4(),
        attempt_no=1,
        limits=limits,
        deadline=deadline,
        tools=ToolContext(
            workspace=ws,
            deadline=deadline,
            tool_timeout_s=limits.tool_timeout_s,
            shell_allowlist=allow,
            test_command=("python", "-m", "unittest"),
        ),
        config={"agent": "test-agent", "version": 1},
    )
    runtime = AgentRuntime(
        session=session,
        toolset=toolset,
        price=PRICE,
        sink=sink,
        retry_policy=RetryPolicy(max_retries=3, base_s=2, cap_s=30, jitter_s=0),
        rng=random.Random(0),
        sleep=fake_sleep,
    )
    return Harness(runtime, ctx, session, sink, ws, sleeps)


def turn(text: str = "", **kw: Any) -> dict[str, Any]:
    return {"type": "turn", "text": text, **kw}


def tool(name: str, **input: Any) -> dict[str, Any]:
    return {"name": name, "input": input}


def err(kind: str, **kw: Any) -> dict[str, Any]:
    return {"type": "error", "kind": kind, **kw}


def finished_output(h: Harness) -> dict[str, Any]:
    last = h.sink.events[-1]
    assert last.event_type is EventType.RUN_FINISHED
    assert last.output is not None
    return last.output


# -- happy paths ----------------------------------------------------------------------------------


async def test_immediate_final_answer() -> None:
    h = harness(turn("The answer is 42."))
    r = await h.run()
    assert (r.outcome, r.reason, r.final_answer, r.steps) == (
        Outcome.AGENT_FINISHED, None, "The answer is 42.", 1,
    )  # fmt: skip
    assert r.usage == Usage(100, 20) and r.cost_usd == DEFAULT_CALL_COST
    assert h.sink.types() == ["run_started", "model_call", "run_finished"]
    assert status_for(r.outcome).value == "COMPLETED"


async def test_tool_loop_feeds_results_back_and_reaps() -> None:
    h = harness(turn("reading", tool_calls=[tool("read_file", path="calc.py")]),
                turn("fixed it", expect_tool_results_contain=["file body"]))  # fmt: skip
    r = await h.run()
    assert r.outcome is Outcome.AGENT_FINISHED and r.steps == 2
    assert h.ws.execs == [["cat", "--", "calc.py"]] and h.ws.reaps == 1
    assert h.sink.types() == [
        "run_started", "model_call", "tool_call", "model_call", "run_finished",
    ]  # fmt: skip
    call = h.sink.events[2]
    assert (call.tool, call.status, call.step, call.input) == ("read_file", "ok", 1,
                                                              {"path": "calc.py"})  # fmt: skip
    assert call.output is not None and call.output["content"] == "file body\n"


async def test_multiple_tool_calls_run_in_order_and_are_answered_together() -> None:
    h = harness(
        turn(tool_calls=[tool("read_file", path="a.py"), tool("read_file", path="b.py"),
                         tool("list_dir")]),
        turn("done"),
    )  # fmt: skip
    await h.run()
    assert [argv[-1] for argv in h.ws.execs[:2]] == ["a.py", "b.py"]
    assert h.ws.execs[2][:2] == ["sh", "-c"]
    tool_results = [e for e in h.session.transcript if "tool_results" in e]
    assert len(tool_results) == 1
    assert [r["id"] for r in tool_results[0]["tool_results"]] == [
        "toolu_fake_1", "toolu_fake_2", "toolu_fake_3",
    ]  # fmt: skip


async def test_events_are_numbered_and_flushed_at_the_agreed_points() -> None:
    h = harness(turn(tool_calls=[tool("read_file", path="a"), tool("read_file", path="b")]),
                turn("done"))  # fmt: skip
    await h.run()
    events = h.sink.events
    assert [e.seq for e in events] == list(range(len(events)))
    assert [e.step for e in events] == [0, 1, 1, 1, 2, 2]
    assert [[e.event_type.value for e in b] for b in h.sink.batches] == [
        ["run_started"], ["model_call"], ["tool_call", "tool_call"], ["model_call"],
        ["run_finished"],
    ]  # fmt: skip


async def test_run_started_records_config_limits_tools_and_runtime_version() -> None:
    h = harness(turn("ok"), tools=["read_file", "shell"])
    await h.run()
    started = h.sink.events[0]
    assert started.input is not None
    assert started.input["runtime_version"] == RUNTIME_VERSION
    assert started.input["tools"] == ["read_file", "shell"]
    assert started.input["config"] == {"agent": "test-agent", "version": 1}
    assert started.input["limits"]["max_steps"] == 30


async def test_totals_match_the_sum_of_model_call_events() -> None:
    h = harness(turn(tool_calls=[tool("read_file", path="a")],
                     usage={"input_tokens": 1000, "output_tokens": 50, "cache_read_tokens": 400,
                            "cache_write_tokens": 600}),
                turn("done", usage={"input_tokens": 300, "output_tokens": 10}))  # fmt: skip
    r = await h.run()
    calls = [e for e in h.sink.events if e.event_type is EventType.MODEL_CALL]
    assert sum((c.cost_usd or Decimal(0) for c in calls), Decimal(0)) == r.cost_usd
    assert r.usage == Usage(1300, 60, 400, 600)
    assert finished_output(h)["usage"]["cache_write_tokens"] == 600


# -- limits ---------------------------------------------------------------------------------------


async def test_max_steps() -> None:
    forever = [turn(tool_calls=[tool("read_file", path="a")]) for _ in range(5)]
    h = harness(*forever, limits=AgentLimits(max_steps=2))
    r = await h.run()
    assert (r.outcome, r.reason, r.steps) == (Outcome.MAX_STEPS, "max_steps", 2)
    assert h.session.steps_consumed == 2
    limit = [e for e in h.sink.events if e.event_type is EventType.LIMIT_REACHED]
    assert limit[0].output == {"limit": "max_steps", "steps": 2, "tokens": 240,
                               "cost_usd": "0.000400"}  # fmt: skip


async def test_token_budget_shrinks_max_tokens_then_stops() -> None:
    big = {"input_tokens": 900, "output_tokens": 100}
    h = harness(turn(tool_calls=[tool("read_file", path="a")], usage=big),
                turn(tool_calls=[tool("read_file", path="a")], usage=big),
                limits=AgentLimits(max_total_tokens=1500, max_tokens_per_call=1000))  # fmt: skip
    r = await h.run()
    assert (r.outcome, r.reason) == (Outcome.BUDGET_EXCEEDED, "max_total_tokens")
    assert [q.max_tokens for q in h.session.requests] == [1000, 500]  # 1500 - 1000 used


async def test_cost_budget() -> None:
    pricey = {"input_tokens": 0, "output_tokens": 200_000}  # $1.00 of output at $5/MTok
    limits = AgentLimits(max_cost_usd=Decimal("0.50"))
    h = harness(turn(tool_calls=[tool("read_file", path="a")], usage=pricey),
                turn("never reached"), limits=limits)  # fmt: skip
    r = await h.run()
    assert (r.outcome, r.reason) == (Outcome.BUDGET_EXCEEDED, "max_cost_usd")
    assert r.cost_usd == Decimal("1.000000")  # overshoot by at most one call, as documented
    assert h.session.steps_consumed == 1


async def test_a_final_answer_wins_even_when_it_crosses_the_budget() -> None:
    h = harness(turn("done", usage={"output_tokens": 200_000}),
                limits=AgentLimits(max_cost_usd=Decimal("0.10")))  # fmt: skip
    assert (await h.run()).outcome is Outcome.AGENT_FINISHED


# -- deadline and cancellation --------------------------------------------------------------------


async def test_slow_healthy_model_hits_the_deadline_as_timeout() -> None:
    h = harness(turn("too slow", delay_s=60), time_left=0.3)
    t0 = time.monotonic()
    r = await h.run()
    assert (r.outcome, r.reason) == (Outcome.TIMEOUT, "deadline")
    assert time.monotonic() - t0 < 2
    assert "limit_reached" in h.sink.types()
    assert status_for(r.outcome).value == "COMPLETED"


async def test_provider_timeout_at_the_deadline_is_timeout_not_model_error() -> None:
    """A real SDK's client timeout is set to the time left, so it can fire just
    before our own deadline timer. That is still the agent running out of time."""
    h = harness(err("timeout", delay_s=0.4), turn("never"), time_left=0.5)
    r = await h.run()
    assert (r.outcome, r.reason) == (Outcome.TIMEOUT, "deadline")
    assert [q.timeout_s <= 0.5 for q in h.session.requests] == [True]
    assert not [e for e in h.sink.events if e.event_type is EventType.MODEL_ERROR]


async def test_no_time_left_before_the_first_call() -> None:
    h = harness(turn("never"), time_left=-1)
    r = await h.run()
    assert r.outcome is Outcome.TIMEOUT and h.session.steps_consumed == 0


async def test_cancel_during_a_model_call() -> None:
    h = harness(turn("slow", delay_s=60))
    task = asyncio.create_task(h.run())
    await asyncio.sleep(0.05)
    h.ctx.cancel.cancel("worker_shutdown")
    r = await asyncio.wait_for(task, 2)
    assert (r.outcome, r.reason) == (Outcome.INTERNAL_ERROR, "cancelled: worker_shutdown")
    assert r.failure_reason is FailureReason.WORKER_SHUTDOWN
    assert status_for(r.outcome).value == "FAILED"
    assert finished_output(h)["reason"] == "cancelled: worker_shutdown"


async def test_cancel_during_a_tool_call_aborts_the_sandbox() -> None:
    ws = ScriptedWorkspace(hang=True)
    h = harness(turn(tool_calls=[tool("read_file", path="a")]), turn("never"), ws=ws)
    task = asyncio.create_task(h.run())
    await asyncio.sleep(0.05)
    h.ctx.cancel.cancel("worker_shutdown")
    r = await asyncio.wait_for(task, 2)
    assert r.outcome is Outcome.INTERNAL_ERROR and ws.aborts == 1
    interrupted = [e for e in h.sink.events if e.event_type is EventType.TOOL_CALL]
    assert interrupted[0].error_class == "Interrupted"
    assert h.session.steps_consumed == 1


async def test_cancel_during_retry_backoff() -> None:
    h = harness(err("overloaded"), turn("never"))

    async def real_sleep(seconds: float) -> None:
        await asyncio.sleep(seconds)

    h.runtime._sleep = real_sleep
    task = asyncio.create_task(h.run())
    await asyncio.sleep(0.05)
    h.ctx.cancel.cancel("worker_shutdown")
    r = await asyncio.wait_for(task, 2)
    assert r.reason == "cancelled: worker_shutdown"


def test_cancel_token_keeps_the_first_reason() -> None:
    token = CancelToken()
    token.cancel("worker_shutdown")
    token.cancel("something else")
    assert token.cancelled and token.reason == "worker_shutdown"


def test_cancel_token_deadline_guard_flips_exactly_at_the_fence() -> None:
    """Design §6 rule 4: the runtime guard also trips from a deadline alone,
    with a fake clock standing in for monotonic()."""
    token = CancelToken()
    now = [100.0]
    token.set_deadline(120.0, clock=lambda: now[0])
    now[0] = 119.999
    assert not token.cancelled
    assert token.reason is None
    now[0] = 120.0
    assert token.cancelled
    assert token.reason == "lease_lost"


async def test_cancel_token_deadline_guard_wakes_an_in_flight_wait() -> None:
    """Checking `cancelled` past the deadline performs a real cancel(), so
    anything already racing against wait() unblocks too."""
    token = CancelToken()
    now = [0.0]
    token.set_deadline(1.0, clock=lambda: now[0])
    waiter = asyncio.ensure_future(token.wait())
    now[0] = 1.0
    assert token.cancelled  # the property check performs the real cancel()
    await asyncio.wait_for(waiter, 1)


def test_cancel_token_explicit_cancel_beats_a_later_deadline() -> None:
    token = CancelToken()
    token.cancel("worker_shutdown")
    token.set_deadline(0.0, clock=lambda: 0.0)  # already past
    assert token.cancelled and token.reason == "worker_shutdown"


# -- model behaviour ------------------------------------------------------------------------------


async def test_refusal() -> None:
    h = harness(turn(stop="refusal", stop_detail="cyber"))
    r = await h.run()
    assert (r.outcome, r.reason) == (Outcome.MODEL_REFUSAL, "refusal: cyber")
    assert status_for(r.outcome).value == "COMPLETED"


async def test_unexpected_stop_reason_is_a_model_error() -> None:
    h = harness(turn(stop="other", stop_detail="pause_turn"))
    r = await h.run()
    assert (r.outcome, r.reason) == (Outcome.MODEL_ERROR, "unexpected stop reason: pause_turn")
    assert r.failure_reason is FailureReason.MODEL_PROTOCOL


async def test_truncated_text_gets_a_continue_nudge() -> None:
    h = harness(turn("half an ans", stop="max_tokens"), turn("half an answer, finished."))
    r = await h.run()
    assert r.final_answer == "half an answer, finished." and r.steps == 2
    assert h.session.transcript[2] == {"role": "user", "text": TRUNCATION_NUDGE}


async def test_truncated_tool_calls_are_answered_without_running() -> None:
    h = harness(turn(tool_calls=[tool("write_file", path="a.py")], stop="max_tokens"),
                turn("retrying later", expect_tool_results_contain=["cut off"]))  # fmt: skip
    r = await h.run()
    assert r.outcome is Outcome.AGENT_FINISHED
    assert h.ws.execs == [] and h.ws.reaps == 0
    result = h.session.transcript[2]["tool_results"][0]
    assert result["is_error"] and result["content"] == TRUNCATED_TOOL_INPUT


async def test_agent_recovers_from_bad_tool_calls() -> None:
    h = harness(
        turn(tool_calls=[tool("browse", url="x"), tool("read_file"),
                         tool("shell", argv=["rm", "-rf", "/"])]),
        turn("ok, done", expect_tool_results_contain=["Unknown tool", "Field required",
                                                      "not an allowed command"]),
    )  # fmt: skip
    r = await h.run()
    assert r.outcome is Outcome.AGENT_FINISHED
    statuses = [e.status for e in h.sink.events if e.event_type is EventType.TOOL_CALL]
    assert statuses == ["unknown_tool", "invalid_input", "denied"]


# -- provider errors and retries ------------------------------------------------------------------


async def test_retryable_errors_then_success() -> None:
    h = harness(err("overloaded", status=529), err("rate_limited", retry_after_s=7),
                turn("finally"))  # fmt: skip
    r = await h.run()
    assert (r.outcome, r.steps) == (Outcome.AGENT_FINISHED, 1)  # retries are not steps
    assert h.sleeps == [2, 7]  # backoff 2s, then the server's retry-after beats 4s
    errors = [e for e in h.sink.events if e.event_type is EventType.MODEL_ERROR]
    assert [(e.error_class, e.output["decision"], e.output["retry_no"]) for e in errors] == [
        ("overloaded", "retry", 0), ("rate_limited", "retry", 1),
    ]  # fmt: skip
    assert errors[1].output["retry_after_s"] == 7


async def test_retries_exhausted_is_a_model_error() -> None:
    h = harness(*[err("server_error", status=500) for _ in range(4)])
    r = await h.run()
    assert r.outcome is Outcome.MODEL_ERROR and "retries_exhausted" in (r.reason or "")
    assert r.failure_reason is FailureReason.MODEL_SERVER_ERROR
    assert h.sleeps == [2, 4, 8]
    decisions = [e.output["decision"] for e in h.sink.events if e.error_class]
    assert decisions == ["retry", "retry", "retry", "give_up"]
    assert status_for(r.outcome).value == "FAILED"


async def test_non_retryable_error_fails_immediately() -> None:
    h = harness(err("authentication", status=401, message="invalid x-api-key"))
    r = await h.run()
    assert r.outcome is Outcome.MODEL_ERROR and "not_retryable" in (r.reason or "")
    assert r.failure_reason is FailureReason.MODEL_AUTHENTICATION
    assert h.sleeps == [] and h.session.steps_consumed == 1


async def test_provider_errors_running_into_the_deadline_are_a_model_error_not_timeout() -> None:
    h = harness(err("overloaded", retry_after_s=120), turn("never"), time_left=60)
    r = await h.run()
    assert r.outcome is Outcome.MODEL_ERROR and "deadline" in (r.reason or "")
    assert r.failure_reason is FailureReason.MODEL_OVERLOADED


async def test_a_short_per_call_timeout_is_retried() -> None:
    """A call timeout shorter than the time left is provider slowness: retry.
    (model_construct skips the >=5s validation so the test stays fast.)"""
    limits = AgentLimits.model_construct(**{**AgentLimits().model_dump(),
                                            "model_call_timeout_s": 0.2})  # fmt: skip
    h = harness(turn("slow", delay_s=10), turn("fast"), limits=limits, time_left=600)
    r = await asyncio.wait_for(h.run(), 5)
    assert r.final_answer == "fast"
    assert [q.timeout_s for q in h.session.requests] == [0.2, 0.2]
    assert [e.error_class for e in h.sink.events if e.error_class] == ["timeout"]


# -- infrastructure failures ----------------------------------------------------------------------


async def test_sandbox_loss_during_a_tool_is_a_sandbox_error() -> None:
    ws = ScriptedWorkspace(result=WorkspaceUnavailable("container gone"))
    h = harness(turn(tool_calls=[tool("read_file", path="a")]), turn("never"), ws=ws)
    r = await h.run()
    assert (r.outcome, r.reason) == (Outcome.SANDBOX_ERROR, "container gone")
    # WorkspaceUnavailable had no reason of its own: falls back to sandbox_exec_failed.
    assert r.failure_reason is FailureReason.SANDBOX_EXEC_FAILED
    failed = [e for e in h.sink.events if e.event_type is EventType.TOOL_CALL][0]
    assert (failed.status, failed.error_class) == ("error", "WorkspaceUnavailable")
    assert status_for(r.outcome).value == "FAILED"


async def test_a_broken_script_is_an_internal_error_and_still_recorded() -> None:
    h = harness(turn(tool_calls=[tool("read_file", path="a")]))  # exhausted on the 2nd call
    r = await h.run()
    assert r.outcome is Outcome.INTERNAL_ERROR and "exhausted" in (r.reason or "")
    assert r.failure_reason is FailureReason.INTERNAL_UNEXPECTED
    assert h.sink.types()[-1] == "run_finished"


async def test_sink_failure_stops_the_run_and_propagates() -> None:
    h = harness(turn("never"))
    h.sink.fail = True
    with pytest.raises(SinkFailed, match="postgres down"):
        await h.run()
    assert h.session.steps_consumed == 0  # no money spent on an unrecordable run


# -- trajectory hygiene ---------------------------------------------------------------------------


async def test_large_tool_inputs_are_capped_in_the_trajectory() -> None:
    huge = "x" * 200_000
    h = harness(turn(tool_calls=[tool("write_file", path="big.txt", content=huge)]), turn("ok"))
    await h.run()
    call = [e for e in h.sink.events if e.event_type is EventType.TOOL_CALL][0]
    assert call.input is not None and call.input["_truncated"] is True
    assert len(call.input["preview"].encode()) < 70_000


def test_system_prompt_is_static() -> None:
    assert build_system_prompt("You fix bugs.") == f"You fix bugs.\n\n{RUNTIME_PREAMBLE}"
    assert build_system_prompt("  ") == RUNTIME_PREAMBLE
    assert build_system_prompt("x") == build_system_prompt("x")  # no timestamps/ids
