"""AnthropicProvider against a stubbed client: real SDK types, no network.

The stub records a deep copy of every request and returns real
anthropic.types.Message objects (or raises real SDK exceptions).
"""

import copy
import random
import time
import uuid
from decimal import Decimal
from typing import Any

import anthropic
import httpx2
import pytest
from anthropic.types import Message

from agenteval_core.agent.context import AgentContext
from agenteval_core.agent.limits import AgentLimits
from agenteval_core.agent.pricing import price_for
from agenteval_core.agent.retry import RetryPolicy
from agenteval_core.agent.runtime import AgentRuntime
from agenteval_core.config import Settings
from agenteval_core.providers.base import (
    RETRYABLE_KINDS,
    ModelError,
    ModelErrorKind,
    ModelSession,
    ProtocolViolation,
    StopReason,
    ToolResultMsg,
    ToolSpec,
    Usage,
)
from agenteval_core.states import Outcome
from agenteval_core.tools.base import ToolContext
from agenteval_core.tools.registry import REGISTRY, ToolSet
from agenteval_core.tools.workspace import ExecOutput
from agenteval_worker.providers import build_providers
from agenteval_worker.providers.anthropic import (
    EMPTY_TOOL_RESULT,
    AnthropicProvider,
    AnthropicSession,
    make_client,
    strict_schema,
    translate_error,
)

REQ = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


class StubMessages:
    def __init__(self, outcomes: list[Message | BaseException]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **params: Any) -> Message:
        self.calls.append(copy.deepcopy(params))  # snapshot: the transcript keeps growing
        out = self.outcomes.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out


class StubClient:
    def __init__(self, *outcomes: Message | BaseException, max_retries: int = 0) -> None:
        self.messages = StubMessages(list(outcomes))
        self.max_retries = max_retries


def msg(
    *content: dict[str, Any],
    stop: str | None = "end_turn",
    usage: dict[str, Any] | None = None,
    model: str = "claude-opus-5",
    stop_details: dict[str, Any] | None = None,
) -> Message:
    return Message.model_validate(
        {
            "id": f"msg_{uuid.uuid4().hex[:8]}",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": list(content),
            "stop_reason": stop,
            "stop_sequence": None,
            "stop_details": stop_details,
            "usage": usage or {"input_tokens": 100, "output_tokens": 20},
        }
    )


def text(t: str) -> dict[str, Any]:
    return {"type": "text", "text": t}


def thinking(signature: str = "sig-abc") -> dict[str, Any]:
    return {"type": "thinking", "thinking": "", "signature": signature}


def tool_use(id: str, name: str, **input: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": id, "name": name, "input": input}


SPECS = ToolSet.build(sorted(REGISTRY), has_test_command=True).specs()


def start(
    client: StubClient,
    *,
    model: str = "claude-opus-5",
    settings: dict[str, Any] | None = None,
    tools: list[ToolSpec] | None = None,
) -> AnthropicSession:
    provider = AnthropicProvider(client)  # type: ignore[arg-type]
    return provider.start(
        model=model,
        system="You fix bugs.\n\nRuntime preamble.",
        tools=SPECS if tools is None else tools,
        task_prompt="Make the tests pass.",
        settings={"effort": "high"} if settings is None else settings,
    )


def status_error(cls: type[anthropic.APIStatusError], status: int, **headers: str) -> Exception:
    resp = httpx2.Response(status, headers={"request-id": "req_123", **headers}, request=REQ)
    return cls(f"HTTP {status}", response=resp, body=None)


# -- request shape --------------------------------------------------------------------------------


async def test_request_shape() -> None:
    client = StubClient(msg(text("hi")))
    session = start(client)
    await session.next(max_tokens=16_000, timeout_s=42.5)
    [params] = client.messages.calls
    assert params["model"] == "claude-opus-5"
    assert (params["max_tokens"], params["timeout"]) == (16_000, 42.5)
    assert params["system"] == [
        {
            "type": "text",
            "text": "You fix bugs.\n\nRuntime preamble.",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    assert params["cache_control"] == {"type": "ephemeral"}  # automatic caching for the tail
    assert params["thinking"] == {"type": "adaptive"}
    assert params["output_config"] == {"effort": "high"}
    assert params["messages"] == [{"role": "user", "content": "Make the tests pass."}]
    # Deliberately absent: forced tool choice, fallbacks, betas, streaming, stop sequences.
    for absent in ("tool_choice", "fallbacks", "betas", "stream", "stop_sequences", "temperature"):
        assert absent not in params


async def test_tools_are_strict_sorted_and_strict_compatible() -> None:
    client = StubClient(msg(text("hi")))
    await start(client).next(max_tokens=100, timeout_s=10)
    tools = client.messages.calls[0]["tools"]
    assert [t["name"] for t in tools] == sorted(t["name"] for t in tools)
    assert all(t["strict"] is True for t in tools)
    list_dir = next(t for t in tools if t["name"] == "list_dir")["input_schema"]
    assert list_dir["additionalProperties"] is False
    depth = list_dir["properties"]["depth"]
    assert "minimum" not in depth and "maximum" not in depth
    assert depth["description"].endswith("(value >= 1, value <= 3)")


@pytest.mark.parametrize(
    ("model", "thinking_sent", "effort_sent"),
    [
        ("claude-opus-5", True, True),
        ("claude-opus-5-5", True, True),
        ("claude-sonnet-5", True, True),
        ("claude-fable-5-1", True, True),
        ("claude-haiku-4-5", False, False),  # no adaptive thinking; effort is a 400 there
    ],
)
async def test_per_model_parameters(model: str, thinking_sent: bool, effort_sent: bool) -> None:
    client = StubClient(msg(text("hi"), model=model))
    settings = {"effort": "medium"} if effort_sent else {}
    await start(client, model=model, settings=settings).next(max_tokens=100, timeout_s=10)
    params = client.messages.calls[0]
    assert ("thinking" in params) is thinking_sent
    assert ("output_config" in params) is effort_sent


def test_unknown_model_is_rejected_at_start() -> None:
    with pytest.raises(ValueError, match="unsupported Anthropic model"):
        start(StubClient(), model="claude-opus-5-20260401")


def test_sdk_retries_must_be_disabled() -> None:
    with pytest.raises(ValueError, match="max_retries=0"):
        AnthropicProvider(StubClient(max_retries=2))  # type: ignore[arg-type]
    assert make_client("sk-ant-test-not-a-real-key").max_retries == 0  # no network call


# -- response translation -------------------------------------------------------------------------


async def test_tool_turn_translation() -> None:
    response = msg(
        thinking(),
        text("Reading the file."),
        tool_use("toolu_1", "read_file", path="calc.py"),
        tool_use("toolu_2", "run_tests"),
        stop="tool_use",
        usage={
            "input_tokens": 1200,
            "output_tokens": 80,
            "cache_read_input_tokens": 900,
            "cache_creation_input_tokens": 300,
        },
        model="claude-opus-5",
    )
    turn = await start(StubClient(response)).next(max_tokens=100, timeout_s=10)
    assert turn.stop is StopReason.TOOL_USE
    assert turn.text == "Reading the file."
    assert [(c.id, c.name, c.input) for c in turn.tool_calls] == [
        ("toolu_1", "read_file", {"path": "calc.py"}),
        ("toolu_2", "run_tests", {}),
    ]
    assert turn.usage == Usage(1200, 80, cache_read_tokens=900, cache_write_tokens=300)
    assert turn.model == "claude-opus-5" and turn.latency_ms >= 0


async def test_multiple_text_blocks_are_joined_and_missing_cache_counts_are_zero() -> None:
    response = msg(text("First."), text("Second."), usage={"input_tokens": 5, "output_tokens": 2})
    turn = await start(StubClient(response)).next(max_tokens=100, timeout_s=10)
    assert turn.text == "First.\n\nSecond."
    assert turn.usage == Usage(5, 2, 0, 0)


@pytest.mark.parametrize(
    ("stop_reason", "stop", "detail"),
    [
        ("end_turn", StopReason.END_TURN, None),
        ("max_tokens", StopReason.MAX_TOKENS, None),
        ("pause_turn", StopReason.OTHER, "pause_turn"),  # server tools only; we use none
        ("stop_sequence", StopReason.OTHER, "stop_sequence"),
        ("model_context_window_exceeded", StopReason.OTHER, "model_context_window_exceeded"),
        (None, StopReason.OTHER, None),
    ],
)
async def test_stop_reason_mapping(
    stop_reason: str | None, stop: StopReason, detail: str | None
) -> None:
    turn = await start(StubClient(msg(text("x"), stop=stop_reason))).next(
        max_tokens=100, timeout_s=10
    )
    assert (turn.stop, turn.stop_detail) == (stop, detail)


async def test_refusal_carries_its_category() -> None:
    response = msg(
        stop="refusal", stop_details={"type": "refusal", "category": "cyber", "explanation": "..."}
    )
    turn = await start(StubClient(response)).next(max_tokens=100, timeout_s=10)
    assert (turn.stop, turn.stop_detail, turn.tool_calls) == (StopReason.REFUSAL, "cyber", ())


# -- transcript: append-only, verbatim ------------------------------------------------------------


async def test_transcript_replays_thinking_verbatim_and_formats_tool_results() -> None:
    first = msg(
        thinking("sig-XYZ"),
        text("Running tests."),
        tool_use("toolu_1", "run_tests"),
        stop="tool_use",
    )
    client = StubClient(first, msg(text("Done.")))
    session = start(client)
    await session.next(max_tokens=100, timeout_s=10)
    session.add_tool_results([ToolResultMsg("toolu_1", "Tests failed (exit 1).", is_error=False)])
    await session.next(max_tokens=100, timeout_s=10)

    second = client.messages.calls[1]["messages"]
    assert [m["role"] for m in second] == ["user", "assistant", "user"]
    assert second[1]["content"] == first.content  # exactly what was returned, thinking included
    assert second[1]["content"][0].signature == "sig-XYZ"
    assert second[2]["content"] == [
        {
            "type": "tool_result",
            "tool_use_id": "toolu_1",
            "content": "Tests failed (exit 1).",
            "is_error": False,
        }
    ]
    # Earlier requests are never rewritten: the first call's snapshot is unchanged.
    assert client.messages.calls[0]["messages"] == [
        {"role": "user", "content": "Make the tests pass."}
    ]


async def test_empty_tool_results_are_replaced() -> None:
    client = StubClient(msg(tool_use("toolu_1", "list_dir"), stop="tool_use"), msg(text("ok")))
    session = start(client)
    await session.next(max_tokens=100, timeout_s=10)
    session.add_tool_results([ToolResultMsg("toolu_1", "", is_error=True)])
    await session.next(max_tokens=100, timeout_s=10)
    result = client.messages.calls[1]["messages"][2]["content"][0]
    assert (result["content"], result["is_error"]) == (EMPTY_TOOL_RESULT, True)


async def test_truncation_nudge_is_a_text_block() -> None:
    client = StubClient(msg(text("half"), stop="max_tokens"), msg(text("whole")))
    session = start(client)
    await session.next(max_tokens=100, timeout_s=10)
    session.add_user_text("Continue.")
    await session.next(max_tokens=100, timeout_s=10)
    assert client.messages.calls[1]["messages"][-1] == {
        "role": "user",
        "content": [{"type": "text", "text": "Continue."}],
    }


async def test_transcript_rules_are_enforced_before_any_request() -> None:
    client = StubClient(msg(tool_use("toolu_1", "list_dir"), stop="tool_use"))
    session = start(client)
    await session.next(max_tokens=100, timeout_s=10)
    with pytest.raises(ProtocolViolation):
        await session.next(max_tokens=100, timeout_s=10)
    with pytest.raises(ProtocolViolation):
        session.add_tool_results([ToolResultMsg("toolu_other", "x")])
    assert len(client.messages.calls) == 1  # the bad calls never reached the API


def test_session_satisfies_the_protocol() -> None:
    s: ModelSession = start(StubClient())  # checked by mypy
    assert s is not None


# -- errors ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "kind", "status"),
    [
        (status_error(anthropic.RateLimitError, 429), ModelErrorKind.RATE_LIMITED, 429),
        (status_error(anthropic.OverloadedError, 529), ModelErrorKind.OVERLOADED, 529),
        (status_error(anthropic.InternalServerError, 500), ModelErrorKind.SERVER_ERROR, 500),
        (status_error(anthropic.ServiceUnavailableError, 503), ModelErrorKind.SERVER_ERROR, 503),
        (status_error(anthropic.DeadlineExceededError, 504), ModelErrorKind.TIMEOUT, 504),
        (status_error(anthropic.APIStatusError, 408), ModelErrorKind.TIMEOUT, 408),
        (status_error(anthropic.ConflictError, 409), ModelErrorKind.CONFLICT, 409),
        (status_error(anthropic.BadRequestError, 400), ModelErrorKind.BAD_REQUEST, 400),
        (status_error(anthropic.UnprocessableEntityError, 422), ModelErrorKind.BAD_REQUEST, 422),
        (status_error(anthropic.AuthenticationError, 401), ModelErrorKind.AUTHENTICATION, 401),
        (status_error(anthropic.APIStatusError, 402), ModelErrorKind.PERMISSION, 402),
        (status_error(anthropic.PermissionDeniedError, 403), ModelErrorKind.PERMISSION, 403),
        (status_error(anthropic.NotFoundError, 404), ModelErrorKind.NOT_FOUND, 404),
        (status_error(anthropic.RequestTooLargeError, 413), ModelErrorKind.REQUEST_TOO_LARGE, 413),
        (anthropic.APITimeoutError(request=REQ), ModelErrorKind.TIMEOUT, None),
        (anthropic.APIConnectionError(request=REQ), ModelErrorKind.CONNECTION, None),
    ],
)
async def test_error_translation(exc: Exception, kind: ModelErrorKind, status: int | None) -> None:
    with pytest.raises(ModelError) as info:
        await start(StubClient(exc)).next(max_tokens=100, timeout_s=10)
    err = info.value
    assert (err.kind, err.status) == (kind, status)
    if status is not None:
        assert err.request_id == "req_123"


def test_retryability_follows_the_kind() -> None:
    retryable = {
        translate_error(status_error(anthropic.APIStatusError, s)).kind in RETRYABLE_KINDS
        for s in (429, 529, 500, 503, 504, 408, 409)
    }
    fatal = {
        translate_error(status_error(anthropic.APIStatusError, s)).kind in RETRYABLE_KINDS
        for s in (400, 401, 402, 403, 404, 413, 422)
    }
    assert (retryable, fatal) == ({True}, {False})


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"retry-after": "7"}, 7.0),
        ({"retry-after-ms": "1500"}, 1.5),
        ({"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}, None),
        ({}, None),
    ],
)
def test_retry_after_parsing(headers: dict[str, str], expected: float | None) -> None:
    err = translate_error(status_error(anthropic.RateLimitError, 429, **headers))
    assert err.retry_after_s == expected


async def test_missing_credentials_is_an_authentication_error() -> None:
    with pytest.raises(ModelError) as info:
        await start(StubClient(anthropic.CredentialsError("no key"))).next(
            max_tokens=100, timeout_s=10
        )
    assert info.value.kind is ModelErrorKind.AUTHENTICATION and not info.value.retryable


# -- strict schemas -------------------------------------------------------------------------------


def test_strict_schema_strips_and_describes_unsupported_keywords() -> None:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "maximum": {"type": "integer", "minimum": 0},  # a *property* named like a keyword
            "argv": {
                "type": "array",
                "minItems": 1,
                "maxItems": 64,
                "items": {"type": "string", "maxLength": 4096},
            },
            "depth": {"type": "integer", "description": "Levels.", "minimum": 1, "maximum": 3},
        },
        "required": ["argv"],
    }
    out = strict_schema(schema)
    props = out["properties"]
    assert set(props) == {"maximum", "argv", "depth"}
    assert props["maximum"] == {"type": "integer", "description": "(value >= 0)"}
    assert props["argv"]["description"] == "(at least 1 items, at most 64 items)"
    assert props["argv"]["items"] == {"type": "string", "description": "(at most 4096 characters)"}
    assert props["depth"]["description"] == "Levels. (value >= 1, value <= 3)"
    assert out["additionalProperties"] is False and out["required"] == ["argv"]
    assert schema["properties"]["depth"]["minimum"] == 1  # the original is not mutated


def test_every_builtin_tool_schema_becomes_strict_compatible() -> None:
    banned = {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "uniqueItems",
    }

    def keywords(node: Any, in_props: bool = False) -> set[str]:
        found: set[str] = set()
        if isinstance(node, dict):
            for k, v in node.items():
                if not in_props:
                    found.add(k)
                found |= keywords(v, in_props=(k == "properties" and not in_props))
        elif isinstance(node, list):
            for v in node:
                found |= keywords(v)
        return found

    for spec in SPECS:
        assert not keywords(strict_schema(spec.input_schema)) & banned, spec.name


# -- the runtime with the real provider (stubbed transport) ---------------------------------------


class OkWorkspace:
    async def exec(self, argv: list[str], *, timeout_s: float) -> ExecOutput:
        return ExecOutput(0, "", "OK\n")

    async def write_files(self, files: dict[str, str]) -> ExecOutput:
        return ExecOutput(0, "", "")

    async def reap(self) -> None:
        pass

    async def abort(self) -> None:
        pass


class ListSink:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append(self, events: Any) -> None:
        self.events += list(events)


async def test_agent_runtime_end_to_end_through_the_provider() -> None:
    client = StubClient(
        status_error(anthropic.OverloadedError, 529),  # retried by the runtime, not the SDK
        msg(
            thinking(),
            tool_use("toolu_1", "run_tests"),
            stop="tool_use",
            usage={"input_tokens": 2000, "output_tokens": 100},
        ),
        # input_tokens is the *uncached* part; the 2000 cached tokens are separate.
        msg(
            text("All tests pass."),
            usage={"input_tokens": 300, "output_tokens": 20, "cache_read_input_tokens": 2000},
        ),
    )
    session = start(client)
    toolset = ToolSet.build(sorted(REGISTRY), has_test_command=True)
    deadline = time.monotonic() + 60
    ctx = AgentContext(
        run_id=uuid.uuid4(),
        attempt_id=uuid.uuid4(),
        attempt_no=1,
        limits=AgentLimits(),
        deadline=deadline,
        tools=ToolContext(
            workspace=OkWorkspace(),
            deadline=deadline,
            tool_timeout_s=30,
            test_command=("python", "-m", "unittest"),
        ),
    )
    price = price_for("anthropic", "claude-opus-5")
    assert price is not None
    sink = ListSink()
    slept: list[float] = []

    async def no_sleep(s: float) -> None:
        slept.append(s)

    runtime = AgentRuntime(
        session=session,
        toolset=toolset,
        price=price,
        sink=sink,
        retry_policy=RetryPolicy(jitter_s=0),
        rng=random.Random(0),
        sleep=no_sleep,
    )
    result = await runtime.run(ctx)
    assert (result.outcome, result.final_answer, result.steps) == (
        Outcome.AGENT_FINISHED,
        "All tests pass.",
        2,
    )
    # 2000*5 + 100*25 + 300*5 + 2000*0.5 + 20*25 = 10000+2500+1500+1000+500 = 15500 / 1e6
    assert result.cost_usd == Decimal("0.015500")
    assert slept == [2.0] and len(client.messages.calls) == 3
    assert [e.error_class for e in sink.events if e.error_class] == ["overloaded"]


# -- worker wiring --------------------------------------------------------------------------------


def test_anthropic_is_off_by_default() -> None:
    providers = build_providers(Settings())
    assert set(providers) == {"fake"}


def test_enabling_anthropic_requires_a_key() -> None:
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY is not"):
        build_providers(Settings(enable_anthropic_provider=True, anthropic_api_key=None))


def test_enabled_anthropic_builds_a_provider_without_network() -> None:
    settings = Settings(
        enable_anthropic_provider=True, anthropic_api_key="sk-ant-test-not-a-real-key"
    )  # type: ignore[arg-type]
    providers = build_providers(settings)
    assert isinstance(providers["anthropic"], AnthropicProvider)
    assert "sk-ant-test" not in repr(settings)  # SecretStr hides the key


def test_an_empty_key_counts_as_no_key() -> None:
    settings = Settings(enable_anthropic_provider=True, anthropic_api_key="")  # type: ignore[arg-type]
    assert settings.anthropic_api_key is None
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY is not"):
        build_providers(settings)
