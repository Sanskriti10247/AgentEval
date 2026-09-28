import asyncio
import time
from typing import Any

import pytest
from pydantic import ValidationError

from agenteval_core.providers.base import (
    ModelError,
    ModelErrorKind,
    ModelProvider,
    ModelSession,
    ModelTurn,
    ProtocolViolation,
    StopReason,
    ToolResultMsg,
    ToolSpec,
    Usage,
)
from agenteval_core.providers.fake import FakeProvider, FakeScript, FakeScriptError, FakeSession

TOOLS = [ToolSpec("read_file", "Read a file", {"type": "object"})]


def session(*steps: dict[str, Any]) -> FakeSession:
    provider = FakeProvider()
    return provider.start(
        model="fake-model",
        system="sys",
        tools=TOOLS,
        task_prompt="do the task",
        settings={"script": list(steps)},
    )


async def call(s: FakeSession, max_tokens: int = 1000, timeout_s: float = 30) -> ModelTurn:
    return await s.next(max_tokens=max_tokens, timeout_s=timeout_s)


def test_fake_provider_satisfies_the_protocols() -> None:
    # Structural conformance is checked by mypy on these assignments.
    provider: ModelProvider = FakeProvider()
    s: ModelSession = session({"type": "turn"})
    assert provider.name == "fake" and s is not None


async def test_tool_turn_then_final_answer() -> None:
    s = session(
        {
            "type": "turn",
            "text": "reading",
            "tool_calls": [{"name": "read_file", "input": {"path": "a.py"}}],
        },
        {"type": "turn", "text": "done: 42", "usage": {"input_tokens": 300, "output_tokens": 7}},
    )
    t1 = await call(s)
    assert t1.stop is StopReason.TOOL_USE
    assert [(c.id, c.name, c.input) for c in t1.tool_calls] == [
        ("toolu_fake_1", "read_file", {"path": "a.py"})
    ]
    assert (t1.model, t1.request_id, t1.usage) == ("fake-model", "req_fake_1", Usage(100, 20))
    s.add_tool_results([ToolResultMsg("toolu_fake_1", "print(42)")])
    t2 = await call(s)
    assert (t2.stop, t2.text, t2.tool_calls, t2.usage) == (
        StopReason.END_TURN,
        "done: 42",
        (),
        Usage(300, 7),
    )
    assert [e["role"] for e in s.transcript] == ["user", "assistant", "user", "assistant"]
    assert s.transcript[2]["tool_results"] == [
        {"id": "toolu_fake_1", "content": "print(42)", "is_error": False}
    ]


async def test_tool_call_ids_are_sequential_across_turns() -> None:
    s = session(
        {"type": "turn", "tool_calls": [{"name": "a"}, {"name": "b"}]},
        {"type": "turn", "tool_calls": [{"name": "c"}]},
    )
    t1 = await call(s)
    assert [c.id for c in t1.tool_calls] == ["toolu_fake_1", "toolu_fake_2"]
    s.add_tool_results([ToolResultMsg("toolu_fake_2", "b"), ToolResultMsg("toolu_fake_1", "a")])
    t2 = await call(s)
    assert [c.id for c in t2.tool_calls] == ["toolu_fake_3"]


async def test_records_requests_for_assertions() -> None:
    s = session({"type": "turn", "text": "x"})
    await call(s, max_tokens=512, timeout_s=9.5)
    assert [(r.max_tokens, r.timeout_s, r.transcript_len) for r in s.requests] == [(512, 9.5, 1)]
    assert s.steps_consumed == 1


# -- transcript rules (catch runtime bugs) -------------------------------------------


async def test_next_while_tool_results_owed_is_a_protocol_violation() -> None:
    s = session({"type": "turn", "tool_calls": [{"name": "a"}]}, {"type": "turn"})
    await call(s)
    with pytest.raises(ProtocolViolation, match="owed"):
        await call(s)


@pytest.mark.parametrize(
    "results",
    [
        [],  # missing
        [ToolResultMsg("toolu_fake_1", "a")],  # partial
        [
            ToolResultMsg("toolu_fake_1", "a"),
            ToolResultMsg("toolu_fake_1", "a"),
            ToolResultMsg("toolu_fake_2", "b"),
        ],  # duplicate
        [ToolResultMsg("toolu_fake_1", "a"), ToolResultMsg("toolu_other", "b")],  # wrong id
    ],
)
async def test_tool_results_must_answer_exactly_the_pending_calls(
    results: list[ToolResultMsg],
) -> None:
    s = session({"type": "turn", "tool_calls": [{"name": "a"}, {"name": "b"}]})
    await call(s)
    with pytest.raises(ProtocolViolation):
        s.add_tool_results(results)


async def test_tool_results_without_pending_calls_rejected() -> None:
    s = session({"type": "turn", "text": "hi"})
    await call(s)
    with pytest.raises(ProtocolViolation, match="no tool calls"):
        s.add_tool_results([ToolResultMsg("toolu_fake_1", "x")])


async def test_user_text_only_when_no_results_owed() -> None:
    s = session(
        {"type": "turn", "text": "partial answ", "stop": "max_tokens"},
        {"type": "turn", "tool_calls": [{"name": "a"}], "stop": "max_tokens"},
    )
    t1 = await call(s)
    assert t1.stop is StopReason.MAX_TOKENS and not t1.tool_calls
    s.add_user_text("You were cut off; continue.")
    t2 = await call(s)
    # Truncated turn with tool calls: they still have to be answered (is_error).
    assert t2.stop is StopReason.MAX_TOKENS and t2.tool_calls
    with pytest.raises(ProtocolViolation):
        s.add_user_text("continue")
    s.add_tool_results([ToolResultMsg("toolu_fake_1", "input truncated", is_error=True)])


@pytest.mark.parametrize(("max_tokens", "timeout_s"), [(0, 10), (100, 0), (100, -1)])
async def test_invalid_call_parameters(max_tokens: int, timeout_s: float) -> None:
    s = session({"type": "turn"})
    with pytest.raises(ProtocolViolation, match="invalid call"):
        await s.next(max_tokens=max_tokens, timeout_s=timeout_s)


# -- scripted failures ----------------------------------------------------------------


async def test_scripted_error_then_success() -> None:
    s = session(
        {"type": "error", "kind": "overloaded", "status": 529, "retry_after_s": 1.5},
        {"type": "turn", "text": "ok"},
    )
    with pytest.raises(ModelError) as exc:
        await call(s)
    assert (exc.value.kind, exc.value.status, exc.value.retry_after_s, exc.value.retryable) == (
        ModelErrorKind.OVERLOADED,
        529,
        1.5,
        True,
    )
    assert exc.value.request_id == "req_fake_1"
    assert (await call(s)).text == "ok"


async def test_delay_is_reported_as_latency() -> None:
    s = session({"type": "turn", "text": "slow", "delay_s": 0.05})
    t0 = time.monotonic()
    turn = await call(s)
    assert time.monotonic() - t0 >= 0.05
    assert turn.latency_ms == 50


async def test_delay_longer_than_timeout_raises_timeout_after_timeout() -> None:
    s = session({"type": "turn", "delay_s": 60})
    t0 = time.monotonic()
    with pytest.raises(ModelError) as exc:
        await call(s, timeout_s=0.1)
    assert exc.value.kind is ModelErrorKind.TIMEOUT and exc.value.retryable
    assert 0.1 <= time.monotonic() - t0 < 1.0


async def test_delay_is_cancellable() -> None:
    s = session({"type": "turn", "delay_s": 60})
    task = asyncio.create_task(call(s, timeout_s=120))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_script_exhausted() -> None:
    s = session({"type": "turn", "text": "only one"})
    await call(s)
    with pytest.raises(FakeScriptError, match="exhausted after 1"):
        await call(s)


async def test_expectations_on_tool_results() -> None:
    script = (
        {"type": "turn", "tool_calls": [{"name": "run_tests"}]},
        {"type": "turn", "text": "fixed", "expect_tool_results_contain": ["2 passed"]},
    )
    ok = session(*script)
    await call(ok)
    ok.add_tool_results([ToolResultMsg("toolu_fake_1", "==== 2 passed in 0.1s ====")])
    assert (await call(ok)).text == "fixed"

    bad = session(*script)
    await call(bad)
    bad.add_tool_results([ToolResultMsg("toolu_fake_1", "1 failed")])
    with pytest.raises(FakeScriptError, match="missing \\['2 passed'\\]"):
        await call(bad)


async def test_refusal_carries_its_category() -> None:
    s = session({"type": "turn", "stop": "refusal", "stop_detail": "cyber"})
    turn = await call(s)
    assert (turn.stop, turn.stop_detail, turn.tool_calls) == (StopReason.REFUSAL, "cyber", ())


# -- script validation (also used to validate agent versions via the API) ----------


@pytest.mark.parametrize(
    "script",
    [
        [],  # empty
        [{"type": "turn", "stop": "tool_use"}],  # tool_use without calls
        [{"type": "turn", "stop": "end_turn", "tool_calls": [{"name": "a"}]}],
        [{"type": "turn", "stop": "refusal", "tool_calls": [{"name": "a"}]}],
        [{"type": "turn", "usage": {"input_tokens": -1}}],
        [{"type": "turn", "surprise": True}],  # unknown field
        [{"type": "error", "kind": "not_a_kind"}],
        [{"type": "sleep"}],  # unknown step type
        [{"type": "turn", "tool_calls": [{"name": ""}]}],
    ],
)
def test_invalid_scripts_rejected(script: list[dict[str, Any]]) -> None:
    with pytest.raises(ValidationError):
        FakeScript.model_validate({"script": script})


def test_script_round_trips_through_json() -> None:
    raw = {
        "script": [
            {"type": "turn", "tool_calls": [{"name": "a", "input": {"x": 1}}]},
            {"type": "error", "kind": "rate_limited", "retry_after_s": 2},
        ]
    }
    script = FakeScript.model_validate(raw)
    assert FakeScript.model_validate_json(script.model_dump_json()) == script


async def test_a_provider_built_with_a_script_ignores_settings() -> None:
    script = FakeScript.model_validate({"script": [{"type": "turn", "text": "direct"}]})
    s = FakeProvider(script).start(
        model="fake-model", system="", tools=[], task_prompt="t", settings={"script": "ignored"}
    )
    assert (await call(s)).text == "direct"
