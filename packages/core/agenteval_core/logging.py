"""structlog configuration. Context (run_id, task_id, attempt_no, worker_id) is
bound through contextvars so every log line in a job carries it."""

import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog
from opentelemetry import trace

# Anthropic API keys look like sk-ant-...; redact them wherever they appear.
_SECRET_VALUE = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")
# AgentEval API keys (ae_<prefix>_<secret>, ADR-012) and bearer credentials in any text.
# The 8-hex prefix is not secret, but a log line never needs the key at all.
_AE_KEY = re.compile(r"ae_[0-9a-f]{8}_[A-Za-z0-9_\-]{8,}")
_BEARER = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=\-]{8,}")
# Log keys whose values are always secret (exact names, case-insensitive).
_SECRET_KEYS = frozenset(
    {"api_key", "anthropic_api_key", "x-api-key", "authorization", "password", "secret"}
)
REDACTED = "[REDACTED]"


def _is_secret_name(name: Any) -> bool:
    if isinstance(name, bytes):
        name = name.decode("latin-1")
    return isinstance(name, str) and name.lower() in _SECRET_KEYS


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        value = _SECRET_VALUE.sub("sk-ant-" + REDACTED, value)
        value = _AE_KEY.sub("ae_" + REDACTED, value)
        return _BEARER.sub(lambda m: m.group(1) + REDACTED, value)
    if isinstance(value, dict):
        return {k: REDACTED if str(k).lower() in _SECRET_KEYS else _redact(v)
                for k, v in value.items()}  # fmt: skip
    if isinstance(value, list | tuple):
        # A header as a (name, value) pair, as ASGI and httpx carry them.
        if len(value) == 2 and _is_secret_name(value[0]):
            pair = (value[0], REDACTED)
            return pair if isinstance(value, tuple) else list(pair)
        return type(value)(_redact(v) for v in value)
    return value


def redact_secrets(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor: never let a credential reach the logs."""
    for key in list(event_dict):
        if key.lower() in _SECRET_KEYS:
            event_dict[key] = REDACTED
        else:
            event_dict[key] = _redact(event_dict[key])
    return event_dict


def add_trace_context(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor: inside a span, tag the line with its trace and span id
    (lowercase hex, as trace UIs show them) so a log line finds its trace."""
    try:
        ctx = trace.get_current_span().get_span_context()
        if ctx.is_valid:
            event_dict["trace_id"] = format(ctx.trace_id, "032x")
            event_dict["span_id"] = format(ctx.span_id, "016x")
    except Exception:  # logging must never fail because tracing did
        pass
    return event_dict


def configure_logging(level: str = "INFO", json: bool = True) -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level.upper())
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            add_trace_context,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            redact_secrets,  # after format_exc_info, so tracebacks are covered too
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
