"""Tracing helpers (ADR-010 B): setup, propagation, the attribute allow-list, exception
recording and log correlation. Spans go to the SDK's in-memory exporter."""

import io
import json
import socket
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import structlog
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from agenteval_core.logging import REDACTED, add_trace_context, redact_secrets
from agenteval_core.observability import tracing
from agenteval_core.queue.streams import _parse_entry

OTEL_ENV = (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_TRACES_EXPORTER",
    "OTEL_SERVICE_NAME",
)


@pytest.fixture
def exporter(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemorySpanExporter]:
    for name in OTEL_ENV:
        monkeypatch.delenv(name, raising=False)
    memory = InMemorySpanExporter()
    handle = tracing.configure_tracing("agenteval-test", processors=[SimpleSpanProcessor(memory)])
    yield memory
    handle.shutdown()


def by_name(exporter: InMemorySpanExporter) -> dict[str, ReadableSpan]:
    return {s.name: s for s in exporter.get_finished_spans()}


def test_no_endpoint_means_no_exporter_and_no_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in OTEL_ENV:
        monkeypatch.delenv(name, raising=False)

    def no_network(*_a: Any, **_k: Any) -> None:
        raise AssertionError("tracing opened a socket with no exporter configured")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    handle = tracing.configure_tracing("agenteval-api")
    try:
        assert handle.exporting is False
        with tracing.span("a") as outer, tracing.span("b"):
            outer.set(run_id=uuid.uuid4())
            # Context still propagates, so trace ids exist (logs, traceparent).
            assert tracing.current_traceparent() is not None
    finally:
        handle.shutdown()  # flushes: must not touch the network either


def test_an_endpoint_turns_the_exporter_on_and_the_service_name_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:9")  # nothing listens
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    handle = tracing.configure_tracing("agenteval-worker")
    try:
        assert handle.exporting is True
        assert handle.provider.resource.attributes["service.name"] == "agenteval-worker"
        with tracing.span("x"):
            pass
    finally:
        handle.shutdown()  # an unreachable collector costs a warning, never an exception


def test_tracing_off_when_the_exporter_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:9")
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "none")
    assert tracing.exporter_endpoint_configured() is False


def test_unconfigured_process_has_no_spans_and_no_errors() -> None:
    # No provider: spans are no-ops and nothing raises (the library default).
    with tracing.span("unconfigured", run_id=uuid.uuid4()) as sp:
        sp.set(status="ok")
        assert tracing.current_traceparent() is None


def test_children_share_the_trace_and_a_root_does_not(exporter: InMemorySpanExporter) -> None:
    with tracing.span("parent"):
        with tracing.span("child"):
            pass
        with tracing.span("fresh", root=True):
            pass
    spans = by_name(exporter)
    assert spans["child"].context.trace_id == spans["parent"].context.trace_id
    assert spans["child"].parent.span_id == spans["parent"].context.span_id
    assert spans["fresh"].context.trace_id != spans["parent"].context.trace_id
    assert spans["fresh"].parent is None


def test_traceparent_round_trip_makes_a_remote_child(exporter: InMemorySpanExporter) -> None:
    with tracing.span("publish", kind=SpanKind.PRODUCER):
        carried = tracing.current_traceparent()
    assert carried is not None
    with tracing.span("consume", kind=SpanKind.CONSUMER, remote=True, traceparent=carried):
        pass
    spans = by_name(exporter)
    assert spans["consume"].context.trace_id == spans["publish"].context.trace_id
    assert spans["consume"].parent.span_id == spans["publish"].context.span_id
    assert spans["consume"].attributes["trace_origin"] == "propagated"


@pytest.mark.parametrize(
    "bad", [None, "", "garbage", "00-" + "0" * 32 + "-" + "0" * 16 + "-01", "x" * 500]
)
def test_missing_or_malformed_traceparent_starts_a_new_root(
    exporter: InMemorySpanExporter, bad: str | None
) -> None:
    run_id = uuid.uuid4()
    with tracing.span("outer"):  # even with a span current, a consumer span is a new root
        with tracing.span("consume", remote=True, traceparent=bad, run_id=run_id):
            pass
    consume = by_name(exporter)["consume"]
    assert consume.parent is None
    assert consume.context.trace_id != by_name(exporter)["outer"].context.trace_id
    assert consume.attributes["trace_origin"] == "new_root"
    assert consume.attributes["run_id"] == str(run_id)


def test_stream_entries_carry_and_parse_the_traceparent() -> None:
    run_id = uuid.uuid4()
    tp = "00-" + "ab" * 16 + "-" + "cd" * 8 + "-01"
    msg = _parse_entry(b"1-0", {b"run_id": str(run_id).encode(), b"traceparent": tp.encode()})
    assert (msg.run_id, msg.traceparent) == (run_id, tp)
    old = _parse_entry(b"2-0", {b"run_id": str(run_id).encode()})  # an older publisher
    assert (old.run_id, old.traceparent) == (run_id, None)


def test_only_allow_listed_scalar_attributes_are_recorded(
    exporter: InMemorySpanExporter,
) -> None:
    with tracing.span(
        "s",
        run_id=uuid.UUID(int=1),
        prompt="SECRET_PROMPT",  # not an allowed key
        content={"x": "SECRET_FILE"},  # not an allowed key
        tool="a" * 1000,  # allowed key, bounded value
        outcome=None,  # None is skipped
        status=["not", "scalar"],  # allowed key, wrong type
        tokens_in=12,
    ) as sp:
        sp.set(error_message="SECRET_ERROR", cost_usd=0.25)
    attrs = dict(by_name(exporter)["s"].attributes)
    assert attrs == {
        "run_id": str(uuid.UUID(int=1)),
        "tool": "a" * tracing.MAX_STRING,
        "tokens_in": 12,
        "cost_usd": 0.25,
    }


def test_exceptions_are_recorded_by_type_only_and_still_propagate(
    exporter: InMemorySpanExporter,
) -> None:
    with pytest.raises(ValueError, match="SECRET_MESSAGE"):
        with tracing.span("boom"):
            raise ValueError("SECRET_MESSAGE /workspace/hidden_test.py")
    span = by_name(exporter)["boom"]
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == "ValueError"
    assert span.attributes["error.type"] == "ValueError"
    [event] = span.events
    assert event.name == "exception"
    assert dict(event.attributes or {}) == {"exception.type": "ValueError"}
    assert "SECRET" not in json.dumps(span.to_json())


def test_a_benign_exception_is_noted_but_is_not_an_error(exporter: InMemorySpanExporter) -> None:
    class Stop(Exception):
        pass

    with pytest.raises(Stop):
        with tracing.span("loop", benign=(Stop,)):
            raise Stop("max steps")
    span = by_name(exporter)["loop"]
    assert span.status.status_code is StatusCode.UNSET
    assert [e.name for e in span.events] == ["exception"]


def test_idle_spans_are_dropped_only_on_the_exporting_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[str] = []

    class Inner(SimpleSpanProcessor):
        def __init__(self) -> None:
            super().__init__(InMemorySpanExporter())

        def on_end(self, span: ReadableSpan) -> None:
            sent.append(span.name)

    drop = tracing._DropIdle(Inner())
    handle = tracing.configure_tracing("agenteval-test", processors=[drop])
    try:
        with tracing.span("reconciler.idle", root=True, idle=True):
            pass
        with tracing.span("reconciler.busy", root=True, idle=False):
            pass
    finally:
        handle.shutdown()
    assert sent == ["reconciler.busy"]


# -- log correlation -------------------------------------------------------------------------


def render(event: dict[str, Any]) -> dict[str, Any]:
    out = io.StringIO()
    logger = structlog.wrap_logger(
        structlog.PrintLogger(file=out),
        processors=[
            structlog.contextvars.merge_contextvars,
            add_trace_context,
            redact_secrets,
            structlog.processors.JSONRenderer(),
        ],
    )
    logger.info(**event)
    return json.loads(out.getvalue())


def test_log_lines_inside_a_span_carry_its_ids_and_keep_redaction(
    exporter: InMemorySpanExporter,
) -> None:
    with tracing.span("work"):
        line = render({"event": "hello", "api_key": "sk-ant-api03-abc"})
    span = by_name(exporter)["work"]
    assert line["trace_id"] == format(span.context.trace_id, "032x")
    assert line["span_id"] == format(span.context.span_id, "016x")
    assert line["api_key"] == REDACTED
    assert "trace_id" not in render({"event": "outside any span"})
