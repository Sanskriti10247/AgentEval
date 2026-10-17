"""Every Prometheus metric AgentEval exports, defined once (ADR-010 A).

Two rules shape this module:

* Closed label sets. A label value is always a member of a fixed enum or table
  (status, outcome, failure reason, transition code, tool, provider/model,
  stream). Never an id, never free text a user or a model can choose: a helper
  maps anything outside its set to "other"/"unknown", so a misbehaving model
  inventing tool names cannot grow the series count.
* Instrumentation never fails the work it observes. Every helper is wrapped by
  `_safe`, which swallows (and counts, via a log line) any exception, and every
  helper is built only from values the caller already holds: no extra query,
  no extra round trip.

Process split. The API process and each worker process expose the same default
registry; a process simply never moves the families it does not own (the API
moves `agenteval_http_*` and the scrape-time gauges, a worker the rest).

Names follow the Prometheus conventions: `_total` counters, base units
(seconds, USD), `_seconds` histograms.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from prometheus_client import Counter, Gauge, Histogram

from agenteval_core.agent.pricing import PRICES
from agenteval_core.logging import get_logger
from agenteval_core.providers.base import ModelErrorKind
from agenteval_core.reliability.taxonomy import (
    CancelReason,
    FailureReason,
    category_of,
)
from agenteval_core.reliability.transitions import EvaluationState, RunState
from agenteval_core.states import Outcome
from agenteval_core.tools.base import ToolStatus

log = get_logger(__name__)

NAMESPACE = "agenteval"
OTHER = "other"

# -- closed sets --------------------------------------------------------------------------

RUN_CODES = frozenset({"R1", "R2", "R3", "R4a", "R4b"} | {f"R{n}" for n in range(5, 15)})
EVALUATION_CODES = frozenset({"E1", "E2", "E3", "E4", "E5", "E6", "E7", "E8a", "E8b", "E9a", "E9b"})
ACTORS = frozenset({"worker", "reconciler"})
RUN_STATUSES = frozenset(s.value for s in RunState)
EVALUATION_STATUSES = frozenset(s.value for s in EvaluationState)
OUTCOMES = frozenset(o.value for o in Outcome)
NO_OUTCOME = "none"  # CANCELLED runs have no outcome
FAILURE_REASONS = frozenset(r.value for r in FailureReason)
CATEGORIES = frozenset(category_of(r).value for r in FailureReason)
DEAD_LETTER_REASONS = frozenset({"retries_exhausted", "non_retryable"})
CANCEL_REASONS = frozenset(r.value for r in CancelReason)
TOOL_STATUSES = frozenset(s.value for s in ToolStatus)
MODEL_ERROR_KINDS = frozenset(k.value for k in ModelErrorKind)
LEASE_KINDS = frozenset({"run", "evaluation"})
ARTIFACT_RESULTS = frozenset({"stored", "fenced", "failed"})
INFLIGHT_KINDS = frozenset({"run", "evaluation"})
SCRAPE_SOURCES = frozenset({"postgres", "redis"})
HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
AUTH_FAILURE_REASONS = frozenset({"missing", "invalid", "revoked", "expired", "forbidden"})
RATE_LIMIT_KINDS = frozenset({"read", "write"})
RECONCILER_STEPS = frozenset(
    {
        "recover_claimed",
        "recover_running",
        "recover_evaluations",
        "record_missing_evaluations",
        "retries_due",
        "redispatch",
        "dispatch_evaluations",
        "reclaim_pending",
        "workers",
        "orphan_sweep",
        "artifact_sweep",
    }
)
BACKLOG_KINDS = ("queued_run", "retrying_run", "pending_evaluation")
_MODEL_KEYS = frozenset(PRICES)  # (provider, model): the only models a run can reach


def _closed(value: object, allowed: frozenset[str], default: str = OTHER) -> str:
    text = getattr(value, "value", value)
    return text if isinstance(text, str) and text in allowed else default


def _safe[**P](fn: Callable[P, None]) -> Callable[P, None]:
    """A metric must never raise into the hot path (ADR-010 Consequences)."""

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> None:
        try:
            fn(*args, **kwargs)
        except Exception as exc:
            log.warning("metric_failed", metric=fn.__name__, error_type=type(exc).__name__)

    return wrapper


# -- API: HTTP -----------------------------------------------------------------------------

HTTP_REQUESTS = Counter(
    "agenteval_http_requests_total",
    "HTTP requests by method, route template and status code.",
    ["method", "route", "status"],
)
HTTP_DURATION = Histogram(
    "agenteval_http_request_duration_seconds",
    "HTTP request latency by method and route template.",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)

# -- API: authentication and rate limiting (ADR-012 A, B, C) -------------------------------

AUTH_FAILURES = Counter(
    "agenteval_auth_failures_total",
    "API requests refused by authentication (401: missing, invalid, revoked, expired) or "
    "authorization (403: forbidden).",
    ["reason"],
)
RATE_LIMITED = Counter(
    "agenteval_rate_limited_total",
    "API requests refused with 429, by budget (read or write).",
    ["kind"],
)
RATE_LIMIT_ERRORS = Counter(
    "agenteval_rate_limit_errors_total",
    "Rate-limit checks that could not reach Redis; the request was allowed (fail open).",
)

# -- API: scrape-time gauges (refreshed by observability.scrape on every scrape) ------------

RUNS_BY_STATUS = Gauge("agenteval_runs_by_status", "Runs by status.", ["status"])
EVALUATIONS_BY_STATUS = Gauge(
    "agenteval_evaluations_by_status", "Attempt evaluations by status.", ["status"]
)
BACKLOG = Gauge(
    "agenteval_backlog",
    "Work waiting for a worker: QUEUED runs, RETRYING runs, PENDING evaluations.",
    ["kind"],
)
BACKLOG_OLDEST_AGE = Gauge(
    "agenteval_backlog_oldest_age_seconds",
    "Age of the oldest waiting item per backlog kind (0 when empty). A queued run is aged "
    "from its creation, a retrying run or pending evaluation from when it became due.",
    ["kind"],
)
WORKERS_LIVE = Gauge(
    "agenteval_workers_live", "Registered workers that are not stopped and heartbeated recently."
)
STREAM_LENGTH = Gauge("agenteval_stream_length", "Entries in a Redis stream (XLEN).", ["stream"])
STREAM_PENDING = Gauge(
    "agenteval_stream_pending",
    "Entries delivered to a consumer but not yet acknowledged (XPENDING).",
    ["stream"],
)
SCRAPE_ERRORS = Counter(
    "agenteval_scrape_errors_total",
    "Failures while reading a source for the scrape-time gauges; the scrape still succeeds.",
    ["source"],
)

# -- worker: runs ---------------------------------------------------------------------------

RUNS_FINISHED = Counter(
    "agenteval_runs_finished_total",
    "Runs that reached a terminal status, by status and outcome.",
    ["status", "outcome"],
)
RUN_TRANSITIONS = Counter(
    "agenteval_run_transitions_total",
    "Run state-machine transitions applied (ADR-009 codes) by the lease owner or the "
    "reconciler. Retries are R8 (worker) and R9 (reconciler); recoveries are the "
    "reconciler's R4a, R4b, R5, R9, R10, R11 and R12.",
    ["code", "actor"],
)
ATTEMPT_FAILURES = Counter(
    "agenteval_attempt_failures_total",
    "Attempts that ended in a platform failure, by failure reason and its category.",
    ["failure_reason", "category"],
)
DEAD_LETTERS = Counter(
    "agenteval_run_dead_letters_total",
    "Runs dead-lettered (FAILED with a dead-letter reason).",
    ["reason"],
)
CANCELLATIONS = Counter(
    "agenteval_run_cancellations_total",
    "Runs cancelled while claimed or running, by the cancel request's reason.",
    ["reason"],
)
QUEUE_WAIT = Histogram(
    "agenteval_run_queue_wait_seconds",
    "Time from a run's creation to its first claim by a worker.",
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 300, 900),
)
RUN_DURATION = Histogram(
    "agenteval_run_duration_seconds",
    "Duration of one finished attempt (sandbox start to finish), by outcome.",
    ["outcome"],
    buckets=(0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300, 600),
)
EVALUATION_TRANSITIONS = Counter(
    "agenteval_evaluation_transitions_total",
    "Evaluation state-machine transitions applied (ADR-009 codes).",
    ["code"],
)
EVALUATION_DURATION = Histogram(
    "agenteval_evaluation_duration_seconds",
    "Duration of one grading attempt, by the status it ended in "
    "(PENDING means it was released for a retry).",
    ["status"],
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300),
)

# -- worker: model and tools ----------------------------------------------------------------

MODEL_CALL_DURATION = Histogram(
    "agenteval_model_call_duration_seconds",
    "Latency of a successful model call as reported by the provider.",
    ["provider", "model"],
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120),
)
MODEL_TOKENS = Counter(
    "agenteval_model_tokens_total",
    "Model tokens by direction (input includes cache reads and writes, as budgets count them).",
    ["provider", "model", "direction"],
)
MODEL_COST = Counter(
    "agenteval_model_cost_usd_total",
    "Estimated model spend in USD at list price (agent/pricing.py).",
    ["provider", "model"],
)
MODEL_ERRORS = Counter(
    "agenteval_model_call_errors_total",
    "Failed model calls (each retry counts), by provider error kind.",
    ["provider", "model", "kind"],
)
TOOL_CALLS = Counter(
    "agenteval_tool_calls_total", "Tool calls by tool and status.", ["tool", "status"]
)
TOOL_DURATION = Histogram(
    "agenteval_tool_call_duration_seconds",
    "Tool call latency by tool.",
    ["tool"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120),
)

# -- worker: reliability and plumbing -------------------------------------------------------

LEASE_LOSSES = Counter(
    "agenteval_lease_losses_total",
    "Leases the worker had to give up (fenced, or its self-stop deadline passed).",
    ["kind"],
)
ARTIFACT_SAVES = Counter(
    "agenteval_artifact_saves_total", "Workspace snapshot saves by result.", ["result"]
)
RECONCILER_STEP_ERRORS = Counter(
    "agenteval_reconciler_step_errors_total",
    "Reconciler steps that raised (the tick carries on with the remaining steps).",
    ["step"],
)
INFLIGHT = Gauge(
    "agenteval_worker_inflight",
    "Runs (including their inline grading) and queued gradings in flight on this worker.",
    ["kind"],
)
CONCURRENCY = Gauge("agenteval_worker_concurrency", "Configured concurrency slots of this worker.")

# -- helpers: one line per call site ---------------------------------------------------------


@_safe
def http_request(method: str, route: str, status: int, seconds: float) -> None:
    method = _closed(method.upper(), HTTP_METHODS)
    HTTP_REQUESTS.labels(method, route, str(status)).inc()
    HTTP_DURATION.labels(method, route).observe(seconds)


@_safe
def auth_failure(reason: str) -> None:
    AUTH_FAILURES.labels(_closed(reason, AUTH_FAILURE_REASONS)).inc()


@_safe
def rate_limited(kind: str) -> None:
    RATE_LIMITED.labels(_closed(kind, RATE_LIMIT_KINDS)).inc()


@_safe
def rate_limit_error() -> None:
    RATE_LIMIT_ERRORS.inc()


@_safe
def scrape_error(source: str) -> None:
    SCRAPE_ERRORS.labels(_closed(source, SCRAPE_SOURCES)).inc()


@_safe
def run_decided(
    code: str,
    actor: str,
    status: RunState | str,
    outcome: Outcome | str | None = None,
    *,
    dead_letter_reason: str | None = None,
    cancel_reason: str | None = None,
) -> None:
    """One committed run transition: the transition itself, plus the terminal
    consequences (finished, dead letter, cancellation) when it has them."""
    RUN_TRANSITIONS.labels(_closed(code, RUN_CODES), _closed(actor, ACTORS)).inc()
    status_value = _closed(status, RUN_STATUSES)
    if status_value in {"COMPLETED", "FAILED", "CANCELLED"}:
        outcome_value = NO_OUTCOME if outcome is None else _closed(outcome, OUTCOMES)
        RUNS_FINISHED.labels(status_value, outcome_value).inc()
    if dead_letter_reason is not None:
        DEAD_LETTERS.labels(_closed(dead_letter_reason, DEAD_LETTER_REASONS)).inc()
    if status_value == "CANCELLED":
        CANCELLATIONS.labels(_closed(cancel_reason, CANCEL_REASONS, "unknown")).inc()


# What each reconciler transition leaves behind: (status, outcome, dead-letter reason).
_RECOVERY_RESULT: dict[str, tuple[RunState, Outcome | None, str | None]] = {
    "R4a": (RunState.QUEUED, None, None),
    "R4b": (RunState.FAILED, Outcome.INTERNAL_ERROR, "retries_exhausted"),
    "R5": (RunState.CANCELLED, None, None),
    "R9": (RunState.RETRYING, None, None),
    "R10": (RunState.FAILED, Outcome.INTERNAL_ERROR, "retries_exhausted"),
    "R11": (RunState.CANCELLED, None, None),
    "R12": (RunState.QUEUED, None, None),
}
# Recovered while RUNNING: the attempt was abandoned (A3) with reason lease_expired.
_ABANDONS_ATTEMPT = frozenset({"R9", "R10", "R11"})


@_safe
def run_recovered(code: str, cancel_reason: str | None = None) -> None:
    """A transition applied by the reconciler (committed already)."""
    status, outcome, dead = _RECOVERY_RESULT[code]
    run_decided(
        code, "reconciler", status, outcome, dead_letter_reason=dead, cancel_reason=cancel_reason
    )
    if code in _ABANDONS_ATTEMPT:
        attempt_failed(FailureReason.LEASE_EXPIRED)


@_safe
def run_step(code: str, actor: str = "worker") -> None:
    """A transition with no terminal consequence (claim R1, start R3, ...)."""
    RUN_TRANSITIONS.labels(_closed(code, RUN_CODES), _closed(actor, ACTORS)).inc()


@_safe
def attempt_failed(reason: FailureReason | str | None) -> None:
    if reason is None:
        return
    try:
        category = category_of(FailureReason(reason)).value
    except ValueError:
        category = OTHER
    ATTEMPT_FAILURES.labels(_closed(reason, FAILURE_REASONS), _closed(category, CATEGORIES)).inc()


@_safe
def queue_wait(seconds: float) -> None:
    QUEUE_WAIT.observe(max(seconds, 0.0))


@_safe
def queue_wait_since(created_at: datetime) -> None:
    """Queue wait from a run's `created_at` (timezone-aware, set by the database)."""
    now = datetime.now(UTC) if created_at.tzinfo else datetime.now()
    QUEUE_WAIT.observe(max((now - created_at).total_seconds(), 0.0))


@_safe
def run_duration_ms(outcome: Outcome | str, duration_ms: int | None) -> None:
    if duration_ms is not None:
        RUN_DURATION.labels(_closed(outcome, OUTCOMES)).observe(max(duration_ms / 1000, 0.0))


@_safe
def evaluation_transition(code: str) -> None:
    EVALUATION_TRANSITIONS.labels(_closed(code, EVALUATION_CODES)).inc()


@_safe
def evaluation_duration_ms(status: EvaluationState | str, duration_ms: int | None) -> None:
    if duration_ms is not None:
        EVALUATION_DURATION.labels(_closed(status, EVALUATION_STATUSES)).observe(
            max(duration_ms / 1000, 0.0)
        )


def _model_labels(provider: str, model: str) -> tuple[str, str]:
    return (provider, model) if (provider, model) in _MODEL_KEYS else (OTHER, OTHER)


@_safe
def model_call(
    provider: str,
    model: str,
    *,
    latency_s: float,
    tokens_in: int,
    tokens_out: int,
    cost_usd: float,
) -> None:
    labels = _model_labels(provider, model)
    MODEL_CALL_DURATION.labels(*labels).observe(max(latency_s, 0.0))
    MODEL_TOKENS.labels(*labels, "input").inc(max(tokens_in, 0))
    MODEL_TOKENS.labels(*labels, "output").inc(max(tokens_out, 0))
    MODEL_COST.labels(*labels).inc(max(cost_usd, 0.0))


@_safe
def model_error(provider: str, model: str, kind: ModelErrorKind | str) -> None:
    MODEL_ERRORS.labels(*_model_labels(provider, model), _closed(kind, MODEL_ERROR_KINDS)).inc()


@_safe
def tool_call(
    tool: str, known_tools: frozenset[str], status: ToolStatus | str, seconds: float
) -> None:
    """`tool` is whatever the model asked for; only a registered name is a label."""
    name = tool if tool in known_tools else "unknown"
    TOOL_CALLS.labels(name, _closed(status, TOOL_STATUSES)).inc()
    TOOL_DURATION.labels(name).observe(max(seconds, 0.0))


@_safe
def lease_lost(kind: str) -> None:
    LEASE_LOSSES.labels(_closed(kind, LEASE_KINDS)).inc()


@_safe
def artifact_save(result: str) -> None:
    ARTIFACT_SAVES.labels(_closed(result, ARTIFACT_RESULTS)).inc()


@_safe
def reconciler_step_error(step: str) -> None:
    RECONCILER_STEP_ERRORS.labels(_closed(step, RECONCILER_STEPS)).inc()


@_safe
def set_concurrency(slots: int) -> None:
    CONCURRENCY.set(slots)


@contextmanager
def inflight(kind: str) -> Iterator[None]:
    """Gauge of work in flight; the decrement is in a finally, so a failing
    run cannot leave it high."""
    gauge = _safe_gauge(kind)
    _bump(gauge, 1)
    try:
        yield
    finally:
        _bump(gauge, -1)


def _safe_gauge(kind: str) -> Any:
    try:
        return INFLIGHT.labels(_closed(kind, INFLIGHT_KINDS))
    except Exception:
        return None


def _bump(gauge: Any, delta: int) -> None:
    try:
        if gauge is not None:
            gauge.inc(delta)
    except Exception as exc:
        log.warning("metric_failed", metric="inflight", error_type=type(exc).__name__)
