"""structlog configuration. Context (run_id, task_id, attempt_no, worker_id) is
bound through contextvars so every log line in a job carries it."""

import logging
import sys

import structlog


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
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
