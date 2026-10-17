"""Tracing (ADR-010 B) through the real code paths: API request -> Redis -> worker ->
attempt -> model and tool calls -> artifact -> finish -> grading, against real Postgres,
Redis and Docker, with the SDK's in-memory exporter.

One run is traced end to end and its spans are checked for (a) a single trace id across
the Redis hop and the expected parent/child tree, and (b) no hidden evaluation data, tool
input, model output or file content in any span (D11, ADR-007).
"""

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agenteval_core.artifacts import LocalArtifactStore
from agenteval_core.observability import tracing
from agenteval_core.queue.streams import RunQueue
from tests.integration.conftest import SANDBOX_IMAGE
from tests.integration.eval_seed import MARKER as API_MARKER
from tests.integration.test_agent_executor import (
    BUGGY,
    FIXED,
    TESTS,
    call,
    handle_next,
    turn,
)
from tests.integration.test_evaluation_runner import HIDDEN_SPEC, build, make_run
from tests.integration.test_evaluation_runner import MARKER as EVAL_MARKER
from tests.integration.test_metrics_worker import build_reconciler
from tests.integration.test_reconciler import queued_run

SM = async_sessionmaker[AsyncSession]

TEXT_MARKER = "MODEL_TEXT_MARKER_c41"
INPUT_MARKER = "TOOL_INPUT_MARKER_c41"
FINAL_MARKER = "FINAL_ANSWER_MARKER_c41"
SECRETS = (API_MARKER, EVAL_MARKER, TEXT_MARKER, INPUT_MARKER, FINAL_MARKER, "sk-ant-api03-")
SCRIPT = [
    turn(f"{TEXT_MARKER}: running the tests.", tool_calls=[call("run_tests")]),
    turn(
        "fixing",
        tool_calls=[call("write_file", path="calc.py", content=f"{FIXED}# {INPUT_MARKER}\n")],
    ),
    turn(tool_calls=[call("run_tests")]),
    turn(f"Fixed. {FINAL_MARKER}"),
]


@contextmanager
def capture() -> Iterator[InMemorySpanExporter]:
    memory = InMemorySpanExporter()
    handle = tracing.configure_tracing("agenteval-test", processors=[SimpleSpanProcessor(memory)])
    try:
        yield memory
    finally:
        handle.shutdown()


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    with capture() as memory:
        yield memory


def finished(memory: InMemorySpanExporter) -> list[ReadableSpan]:
    return list(memory.get_finished_spans())


def named(spans: list[ReadableSpan], name: str) -> list[ReadableSpan]:
    return [s for s in spans if s.name == name]


def one(spans: list[ReadableSpan], name: str) -> ReadableSpan:
    [match] = named(spans, name)
    return match


def assert_no_secrets(spans: list[ReadableSpan]) -> None:
    """Every span, whole (attributes, events, status, links, name), is free of the seeds,
    and every attribute key is on the allow-list."""
    for span in spans:
        assert set(span.attributes or {}) <= tracing.ALLOWED_ATTRIBUTES, span.name
        dumped = span.to_json()
        for secret in SECRETS:
            assert secret not in dumped, (span.name, secret)


async def api_submit(client: httpx.AsyncClient) -> str:
    agent = (await client.post("/api/v1/agents", json={"name": f"a-{uuid.uuid4().hex[:6]}"})).json()
    version = await client.post(
        f"/api/v1/agents/{agent['id']}/versions",
        json={
            "model_provider": "fake",
            "model": "fake-model",
            "system_prompt": "You fix bugs in small Python projects.",
            "tools": ["read_file", "write_file", "list_dir", "search", "shell", "run_tests"],
            "shell_allowlist": ["python"],
            "limits": {"max_steps": 10},
            "model_settings": {"script": SCRIPT},
        },
    )
    assert version.status_code == 201, version.text
    task = await client.post(
        "/api/v1/tasks",
        json={
            "name": "fix-add",
            "task_type": "agent",
            "image": SANDBOX_IMAGE,
            "instructions": "The tests in test_calc.py fail. Fix calc.py so they pass.",
            "test_command": ["python", "-m", "unittest", "-q"],
            "files": {"calc.py": BUGGY, "test_calc.py": TESTS},
            "timeout_s": 120,
            "evaluation": HIDDEN_SPEC,
        },
    )
    assert task.status_code == 201, task.text
    run = await client.post(
        "/api/v1/runs",
        json={"task_id": task.json()["id"], "agent_version_id": version.json()["id"]},
    )
    assert run.status_code == 202, run.text
    return str(run.json()["id"])


@pytest.mark.docker
async def test_one_trace_spans_the_api_the_redis_hop_the_worker_and_the_grading(
    client: httpx.AsyncClient,
    sessionmaker: SM,
    queue: RunQueue,
    artifact_store: LocalArtifactStore,
    spans: InMemorySpanExporter,
) -> None:
    run_id = await api_submit(client)
    ex, _ = build(sessionmaker, queue, artifact_store)
    await handle_next(ex, queue)

    got = finished(spans)
    request = one(got, "POST /api/v1/runs")
    publish = one(got, "queue.publish")
    execute = one(got, "run.execute")
    assert request.kind is SpanKind.SERVER
    assert request.attributes["http.route"] == "/api/v1/runs"  # the template, not a raw path
    assert request.attributes["http.response.status_code"] == 202
    assert (publish.kind, execute.kind) == (SpanKind.PRODUCER, SpanKind.CONSUMER)

    # One trace id for everything the request caused, on both sides of Redis.
    tree = [
        request, publish, execute, one(got, "run.claim"), one(got, "attempt"),
        one(got, "run.finish"), one(got, "artifact.save"), one(got, "evaluation.grade"),
        one(got, "evaluation.check"), *named(got, "agent.step"), *named(got, "model.call"),
        *named(got, "tool.call"),
    ]  # fmt: skip
    assert {s.context.trace_id for s in tree} == {request.context.trace_id}

    def parent_of(span: ReadableSpan) -> int | None:
        return span.parent.span_id if span.parent else None

    assert parent_of(publish) == request.context.span_id
    assert parent_of(execute) == publish.context.span_id  # the traceparent crossed Redis
    assert execute.attributes["trace_origin"] == "propagated"
    assert execute.attributes["run_id"] == run_id
    attempt = one(got, "attempt")
    for child in (one(got, "run.claim"), attempt, one(got, "evaluation.grade")):
        assert parent_of(child) == execute.context.span_id
    assert parent_of(one(got, "run.finish")) == attempt.context.span_id
    assert parent_of(one(got, "artifact.save")) == attempt.context.span_id
    assert parent_of(one(got, "evaluation.check")) == one(got, "evaluation.grade").context.span_id
    step_ids = {s.context.span_id for s in named(got, "agent.step")}
    assert len(step_ids) == 4
    for child in (*named(got, "model.call"), *named(got, "tool.call")):
        assert parent_of(child) in step_ids

    assert [s.attributes["tool"] for s in named(got, "tool.call")] == [
        "run_tests", "write_file", "run_tests",
    ]  # fmt: skip
    model = named(got, "model.call")[0]
    assert (model.attributes["provider"], model.attributes["model"]) == ("fake", "fake-model")
    assert (model.attributes["tokens_in"], model.attributes["tokens_out"]) == (100, 20)
    assert one(got, "run.finish").attributes["finish_outcome"] == "finished"
    assert one(got, "run.finish").attributes["outcome"] == "AGENT_FINISHED"
    assert one(got, "artifact.save").attributes["result"] == "stored"
    assert one(got, "evaluation.grade").attributes["status"] in {"SUCCEEDED", "FAILED"}
    assert_no_secrets(got)


@pytest.mark.docker
async def test_a_message_without_traceparent_starts_a_new_root_linked_by_run_id(
    sessionmaker: SM, queue: RunQueue, artifact_store: LocalArtifactStore
) -> None:
    # Published before tracing is configured, like an older publisher: no traceparent field.
    run_id = await make_run(sessionmaker, queue, SCRIPT)
    with capture() as spans:
        ex, _ = build(sessionmaker, queue, artifact_store)
        await handle_next(ex, queue)

        got = finished(spans)
        execute = one(got, "run.execute")
        assert execute.parent is None
        assert execute.attributes["trace_origin"] == "new_root"
        assert execute.attributes["run_id"] == str(run_id)
        below = {s.context.trace_id for s in got if s.name != "run.execute"}
        assert below == {execute.context.trace_id}  # the whole worker side hangs off the new root
        assert_no_secrets(got)


async def test_an_unconfigured_publisher_adds_no_traceparent_field(
    queue: RunQueue, redis: Any
) -> None:
    await queue.publish(uuid.uuid4())
    [(_, fields)] = await redis.xrange(queue.stream)
    assert set(fields) == {b"run_id"}


async def test_a_publish_inside_a_span_carries_the_traceparent(
    queue: RunQueue, redis: Any, spans: InMemorySpanExporter
) -> None:
    with tracing.span("request"):
        await queue.publish(uuid.uuid4())
    [(_, fields)] = await redis.xrange(queue.stream)
    publish = one(finished(spans), "queue.publish")
    trace_id = format(publish.context.trace_id, "032x")
    span_id = format(publish.context.span_id, "016x")
    assert fields[b"traceparent"].decode().split("-")[1:3] == [trace_id, span_id]
    [msg] = await queue.read("t", count=1, block_ms=500)
    assert msg.traceparent == fields[b"traceparent"].decode()
    assert json.dumps(publish.attributes["messaging.destination.name"]) == f'"{queue.stream}"'


async def test_reconciler_steps_are_root_spans_and_redispatch_publishes_a_fresh_context(
    engine: AsyncEngine, sessionmaker: SM, queue: RunQueue, spans: InMemorySpanExporter
) -> None:
    await queued_run(engine, dispatched=None)  # never published: redispatch picks it up
    await build_reconciler(sessionmaker, queue).tick()

    got = finished(spans)
    steps = [s for s in got if s.name.startswith("reconciler.")]
    assert {s.name for s in steps} >= {"reconciler.redispatch", "reconciler.retries_due"}
    assert all(s.parent is None for s in steps)  # each step is its own trace
    assert len({s.context.trace_id for s in steps}) == len(steps)
    redispatch = one(got, "reconciler.redispatch")
    assert (redispatch.attributes["count"], redispatch.attributes["idle"]) == (1, False)
    assert one(got, "reconciler.retries_due").attributes["idle"] is True
    publish = one(got, "queue.publish")
    assert publish.parent.span_id == redispatch.context.span_id
    [msg] = await queue.read("t", count=1, block_ms=500)
    assert msg.traceparent is not None
    assert msg.traceparent.split("-")[1] == format(redispatch.context.trace_id, "032x")
    assert_no_secrets(got)
