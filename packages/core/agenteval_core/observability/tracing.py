"""OpenTelemetry tracing (ADR-010 B): setup plus a few small helpers.

Three rules shape this module, mirroring metrics.py:

* Export is opt-in. A TracerProvider is always created, so span context still
  propagates (trace ids appear in logs, `traceparent` rides the Redis hop), but
  an exporter is attached ONLY when `OTEL_EXPORTER_OTLP_ENDPOINT` or
  `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` is set (and `OTEL_TRACES_EXPORTER` is not
  `none`). With neither, nothing leaves the process and no socket is opened.
  The standard `OTEL_*` variables (service name, sampler, batch tuning, OTLP
  headers and timeouts) are honoured by the SDK itself.
* Ids, enums and numbers only (D11, ADR-007). Every attribute goes through
  `ALLOWED_ATTRIBUTES`: a key outside the list is dropped, a value that is not a
  bool, int, float or short string is dropped. Prompts, model output, tool
  inputs and outputs, file contents, hidden checks and error messages from
  candidate code have no key to ride on. An exception is recorded as its TYPE
  name only, never `str(exc)` and never a traceback.
* Instrumentation never fails the work it observes. Every helper swallows its
  own errors; `span()` always yields a usable handle (a no-op one if the SDK
  misbehaves) and never changes what the wrapped block raises.
"""

from __future__ import annotations

import asyncio
import os
import threading
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from agenteval_core.logging import get_logger

log = get_logger(__name__)

TRACEPARENT_FIELD = "traceparent"  # the Redis stream field that carries the context
INSTRUMENTATION_NAME = "agenteval"
MAX_STRING = 128
_MAX_TRACEPARENT = 128  # a well-formed one is 55 characters

# The only attribute keys a span may carry. Adding a key is a reviewed change
# (tests/integration/test_tracing.py pins the set's intent).
ALLOWED_ATTRIBUTES = frozenset(
    {
        # ids
        "run_id", "evaluation_id", "attempt_id", "task_id", "worker_id", "message_id", "attempt_no",
        "step",
        # enums and closed sets
        "status", "outcome", "failure_reason", "tool", "tool_status", "provider", "model",
        "stop_reason", "error_kind", "transition", "result", "finish_outcome", "stream",
        "trace_origin", "http.request.method", "http.route", "http.response.status_code",
        "messaging.system", "messaging.operation.type", "messaging.destination.name",
        "error.type", "reconciler_step",
        # sizes, counts, durations, tokens, money
        "size_bytes", "output_bytes", "exit_code", "duration_ms", "tokens_in", "tokens_out",
        "cost_usd", "retry_no", "eval_attempts", "count", "idle",
    }
)  # fmt: skip

# -- setup ------------------------------------------------------------------------------------

_lock = threading.Lock()
_provider: TracerProvider | None = None


@dataclass(frozen=True)
class TracingHandle:
    """What `configure_tracing` returns. `owned` is False when a provider already
    existed (a second call in the same process): shutting down then only flushes."""

    provider: TracerProvider
    owned: bool
    exporting: bool

    def shutdown(self) -> None:
        try:
            if self.owned:
                self.provider.shutdown()
                _forget(self.provider)
            else:
                self.provider.force_flush()
        except Exception as exc:
            log.warning("tracing_shutdown_failed", error_type=type(exc).__name__)


def exporter_endpoint_configured() -> bool:
    if os.environ.get("OTEL_TRACES_EXPORTER", "").strip().lower() == "none":
        return False
    return any(
        os.environ.get(name, "").strip()
        for name in ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    )


class _DropIdle(SpanProcessor):
    """Wraps the exporting processor. A reconciler step that found nothing to do
    (`idle=True`) is not exported: there are several per second and they carry no
    information. Every other span passes straight through."""

    def __init__(self, inner: SpanProcessor) -> None:
        self._inner = inner

    def on_start(self, span: Any, parent_context: Context | None = None) -> None:
        self._inner.on_start(span, parent_context)

    def on_end(self, span: ReadableSpan) -> None:
        if (span.attributes or {}).get("idle") is True:
            return
        self._inner.on_end(span)

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._inner.force_flush(timeout_millis)


def configure_tracing(
    service_name: str, *, processors: Sequence[SpanProcessor] = ()
) -> TracingHandle:
    """Create the process's TracerProvider (once). `processors` is for tests,
    which attach an in-memory exporter; production relies on the environment."""
    global _provider
    with _lock:
        if _provider is not None:
            for processor in processors:
                _provider.add_span_processor(processor)
            return TracingHandle(_provider, owned=False, exporting=exporter_endpoint_configured())
        attributes = {} if os.environ.get("OTEL_SERVICE_NAME") else {SERVICE_NAME: service_name}
        provider = TracerProvider(resource=Resource.create(attributes))
        exporting = False
        if exporter_endpoint_configured():
            try:
                # Imported here: no exporter module is loaded unless export is on.
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                    OTLPSpanExporter,
                )

                provider.add_span_processor(_DropIdle(BatchSpanProcessor(OTLPSpanExporter())))
                exporting = True
            except Exception as exc:  # a bad OTEL_* value costs the traces, never the process
                log.warning("tracing_export_disabled", error_type=type(exc).__name__)
        for processor in processors:
            provider.add_span_processor(processor)
        _provider = provider
        # Deliberately not installed as OpenTelemetry's global provider: nothing here
        # uses auto-instrumentation, and a global would outlive `shutdown()`.
        log.info("tracing_configured", service=service_name, exporting=exporting)
        return TracingHandle(provider, owned=True, exporting=exporting)


def _forget(provider: TracerProvider) -> None:
    global _provider
    with _lock:
        if _provider is provider:
            _provider = None


def _tracer() -> trace.Tracer:
    provider = _provider
    if provider is not None:
        return provider.get_tracer(INSTRUMENTATION_NAME)
    return trace.get_tracer(INSTRUMENTATION_NAME)  # a no-op tracer until configured


# -- propagation ------------------------------------------------------------------------------

_PROPAGATOR = TraceContextTextMapPropagator()


def current_traceparent() -> str | None:
    """The W3C `traceparent` of the active span, or None when there is none (no
    provider configured, or no span open). Never raises."""
    try:
        carrier: dict[str, str] = {}
        _PROPAGATOR.inject(carrier)
        return carrier.get(TRACEPARENT_FIELD)
    except Exception:
        return None


def _context_from(traceparent: str | None) -> Context | None:
    """The remote parent a `traceparent` describes, or None if it is absent or
    malformed (an unparseable value is treated exactly like a missing one)."""
    if not traceparent or len(traceparent) > _MAX_TRACEPARENT:
        return None
    try:
        ctx = _PROPAGATOR.extract({TRACEPARENT_FIELD: traceparent}, context=Context())
        return ctx if trace.get_current_span(ctx).get_span_context().is_valid else None
    except Exception:
        return None


# -- spans ------------------------------------------------------------------------------------


def _clean(attributes: dict[str, Any]) -> dict[str, str | bool | int | float]:
    out: dict[str, str | bool | int | float] = {}
    for key, value in attributes.items():
        if key not in ALLOWED_ATTRIBUTES or value is None:
            continue
        if isinstance(value, Enum):
            value = value.value
        if isinstance(value, bool | int | float):
            out[key] = value
        elif isinstance(value, uuid.UUID):
            out[key] = str(value)
        elif isinstance(value, str):
            out[key] = value[:MAX_STRING]
    return out


class SpanHandle:
    """What `span()` yields. Every method is safe to call whatever the SDK does."""

    __slots__ = ("_span",)

    def __init__(self, span: trace.Span | None) -> None:
        self._span = span

    def set(self, **attributes: Any) -> None:
        if self._span is None:
            return
        try:
            for key, value in _clean(attributes).items():
                self._span.set_attribute(key, value)
        except Exception:
            pass

    def fail(self, exc: BaseException, *, benign: bool = False) -> None:
        """Record an exception by TYPE only. A `benign` one (control flow such as
        a stop the agent loop raises on purpose) is noted but is not an error."""
        if self._span is None:
            return
        try:
            name = type(exc).__name__
            self._span.add_event("exception", {"exception.type": name})
            if not benign:
                self._span.set_attribute("error.type", name)
                self._span.set_status(Status(StatusCode.ERROR, name))
        except Exception:
            pass

    def mark_error(self, error_type: str) -> None:
        """Error status with a fixed, caller-chosen label (never a message)."""
        if self._span is None:
            return
        try:
            self._span.set_attribute("error.type", error_type)
            self._span.set_status(Status(StatusCode.ERROR, error_type))
        except Exception:
            pass


_NOOP = SpanHandle(None)


@contextmanager
def span(
    name: str,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
    traceparent: str | None = None,
    remote: bool = False,
    root: bool = False,
    benign: tuple[type[BaseException], ...] = (),
    attributes: dict[str, Any] | None = None,
    **named: Any,
) -> Iterator[SpanHandle]:
    """Open a span, make it current for the block, end it on exit.

    * default: a child of the current span (or a root if none is open);
    * `remote=True`: the span's parent is the context in `traceparent`; a missing
      or malformed one starts a NEW root (the run_id attribute links it to the
      rest of the run's story) and `trace_origin` says which of the two happened;
    * `root=True`: always a new root, whatever is current.

    Attributes are keyword arguments, or `attributes={...}` for dotted names.

    The block's own exceptions pass through untouched; they are only recorded.
    """
    handle = _NOOP
    token = None
    sp: trace.Span | None = None
    try:
        parent: Context | None = None
        origin = None
        if remote:
            parent = _context_from(traceparent)
            origin = "propagated" if parent is not None else "new_root"
            if parent is None:
                parent = Context()
        elif root:
            parent = Context()
        sp = _tracer().start_span(name, context=parent, kind=kind)
        token = otel_context.attach(trace.set_span_in_context(sp))
        handle = SpanHandle(sp)
        handle.set(**{**(attributes or {}), **named, "trace_origin": origin})
    except Exception:
        handle = _NOOP
    try:
        yield handle
    except BaseException as exc:
        handle.fail(exc, benign=isinstance(exc, (*benign, asyncio.CancelledError)))
        raise
    finally:
        try:
            if token is not None:
                otel_context.detach(token)
            if sp is not None:
                sp.end()
        except Exception:
            pass


def set_attributes(**attributes: Any) -> None:
    """Attributes on the active span, for a place that has no handle in scope."""
    try:
        SpanHandle(trace.get_current_span()).set(**attributes)
    except Exception:
        pass
