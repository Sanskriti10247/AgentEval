"""structlog configuration. Context (run_id, task_id, attempt_no, worker_id) is
bound through contextvars so every log line in a job carries it."""

import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

# Anthropic API keys look like sk-ant-...; redact them wherever they appear.
_SECRET_VALUE = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")
# Log keys whose values are always secret (exact names, case-insensitive).
_SECRET_KEYS = frozenset(
    {"api_key", "anthropic_api_key", "x-api-key", "authorization", "password", "secret"}
)
REDACTED = "[REDACTED]"


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        return _SECRET_VALUE.sub("sk-ant-" + REDACTED, value)
    if isinstance(value, dict):
        return {k: REDACTED if str(k).lower() in _SECRET_KEYS else _redact(v)
                for k, v in value.items()}  # fmt: skip
    if isinstance(value, list | tuple):
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


def configure_logging(level: str = "INFO", json: bool = True) -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level.upper())
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
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
