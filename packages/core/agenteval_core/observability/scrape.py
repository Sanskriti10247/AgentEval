"""Scrape-time gauges: the state of the system as Postgres and Redis hold it now.

prometheus_client collectors are synchronous, but the API's engine and Redis
client are asyncio-only. Running them inside a sync `Collector.collect()` would
mean blocking the event loop or opening a second, thread-bound connection
stack. Instead the `/metrics` handler awaits `refresh()` first, which reads
both sources with the application's own async clients and writes the results
into ordinary gauges (metrics.py); `generate_latest()` then renders those. The
values are therefore read on every scrape and never cached between scrapes.

A failing source never fails the scrape. It increments
`agenteval_scrape_errors_total{source}`, its gauges are cleared (absent, not
stale: an alert on `absent()` or on the error counter sees it), and the other
source is unaffected. Each source has a hard timeout, so a hung dependency
costs a scrape at most `timeout_s`, not Prometheus's whole scrape window.

Cost: six small aggregate queries and two Redis commands per stream, each
served by an existing (partial) index. At a 15 s scrape interval that is
negligible next to the run workload (ADR-010 Consequences).
"""

import asyncio
from collections.abc import Coroutine, Sequence
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import ResponseError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agenteval_core.evaluation.taxonomy import EvaluationStatus
from agenteval_core.logging import get_logger
from agenteval_core.observability import metrics
from agenteval_core.reliability.transitions import RunState

log = get_logger(__name__)

DEFAULT_TIMEOUT_S = 3.0

_RUNS_BY_STATUS = text("SELECT status, count(*) AS n FROM runs GROUP BY status")
_QUEUED_AGE = text(
    "SELECT extract(epoch FROM now() - min(created_at)) AS age FROM runs WHERE status = 'QUEUED'"
)
_RETRYING = text(
    "SELECT count(*) AS n, extract(epoch FROM now() - min(next_attempt_at)) AS age"
    " FROM runs WHERE status = 'RETRYING'"
)
_EVALUATIONS_BY_STATUS = text(
    "SELECT status, count(*) AS n FROM attempt_evaluations GROUP BY status"
)
_PENDING_AGE = text(
    "SELECT extract(epoch FROM now() - min(COALESCE(next_attempt_at, created_at))) AS age"
    " FROM attempt_evaluations WHERE status = 'PENDING'"
)
_LIVE_WORKERS = text(
    "SELECT count(*) AS n FROM workers"
    " WHERE stopped_at IS NULL AND last_heartbeat_at > now() - make_interval(secs => :window)"
)


async def refresh(
    sessionmaker: async_sessionmaker[AsyncSession],
    redis: Redis,
    *,
    streams: Sequence[str],
    group: str,
    live_worker_window_s: float,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> None:
    """Re-read every source and update the gauges. Never raises."""
    await asyncio.gather(
        _guarded("postgres", _refresh_postgres(sessionmaker, live_worker_window_s), timeout_s),
        _guarded("redis", _refresh_redis(redis, streams, group), timeout_s),
    )


async def _guarded(source: str, work: Coroutine[Any, Any, None], timeout_s: float) -> None:
    try:
        async with asyncio.timeout(timeout_s):
            await work
    except Exception as exc:  # includes TimeoutError; CancelledError still propagates
        metrics.scrape_error(source)
        _clear(source)
        log.warning("scrape_source_failed", source=source, error_type=type(exc).__name__)


def _clear(source: str) -> None:
    gauges = (
        (
            metrics.RUNS_BY_STATUS,
            metrics.EVALUATIONS_BY_STATUS,
            metrics.BACKLOG,
            metrics.BACKLOG_OLDEST_AGE,
            metrics.WORKERS_LIVE,
        )
        if source == "postgres"
        else (metrics.STREAM_LENGTH, metrics.STREAM_PENDING)
    )
    for gauge in gauges:
        gauge.clear()


async def _refresh_postgres(
    sessionmaker: async_sessionmaker[AsyncSession], live_window_s: float
) -> None:
    # Everything is read first and applied last, so a failure half way leaves
    # no mixed old/new set of gauges.
    async with sessionmaker() as s, s.begin():
        runs = {r.status: int(r.n) for r in (await s.execute(_RUNS_BY_STATUS)).all()}
        queued_age: float | None = (await s.execute(_QUEUED_AGE)).scalar_one()
        retrying = (await s.execute(_RETRYING)).one()
        evaluations = {r.status: int(r.n) for r in (await s.execute(_EVALUATIONS_BY_STATUS)).all()}
        pending_age: float | None = (await s.execute(_PENDING_AGE)).scalar_one()
        live: int = (await s.execute(_LIVE_WORKERS, {"window": live_window_s})).scalar_one()

    _clear("postgres")
    for state in RunState:
        metrics.RUNS_BY_STATUS.labels(state.value).set(runs.get(state.value, 0))
    for status in EvaluationStatus:
        metrics.EVALUATIONS_BY_STATUS.labels(status.value).set(evaluations.get(status.value, 0))
    backlog = {
        "queued_run": (runs.get("QUEUED", 0), queued_age),
        "retrying_run": (int(retrying.n), retrying.age),
        "pending_evaluation": (evaluations.get("PENDING", 0), pending_age),
    }
    for kind, (count, age) in backlog.items():
        metrics.BACKLOG.labels(kind).set(count)
        metrics.BACKLOG_OLDEST_AGE.labels(kind).set(max(float(age or 0), 0.0))
    metrics.WORKERS_LIVE.set(int(live))


async def _refresh_redis(redis: Redis, streams: Sequence[str], group: str) -> None:
    lengths: dict[str, int] = {}
    pending: dict[str, int] = {}
    for stream in streams:
        lengths[stream] = int(await redis.xlen(stream))
        try:
            summary = await redis.xpending(stream, group)
            pending[stream] = int(summary["pending"])
        except ResponseError as exc:
            # No stream or group yet (nothing ever published or consumed): empty, not broken.
            if "NOGROUP" not in str(exc) and "no such key" not in str(exc).lower():
                raise
            pending[stream] = 0

    _clear("redis")
    for stream in streams:
        metrics.STREAM_LENGTH.labels(stream).set(lengths[stream])
        metrics.STREAM_PENDING.labels(stream).set(pending[stream])
