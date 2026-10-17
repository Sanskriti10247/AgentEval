"""Timing queries (Postgres is the source of truth) and the percentile maths.

Everything below `fetch_rows` is pure: it takes the per-run rows and returns
numbers, so the arithmetic is unit-tested without a database.

Definitions (ADR-011), all from server-side timestamps in seconds:

  queue     claimed_at - created_at          (first claim of the run)
  start     started_at - claimed_at          (claim -> attempt running)
  exec      attempt finished_at - started_at (the last attempt)
  e2e       finished_at - created_at         (run terminal)
  grade     evaluation finished_at - attempt finished_at   (agent runs only)
  e2e_graded  evaluation finished_at - created_at          (agent runs only)

Percentiles use linear interpolation between closest ranks (the "type 7"
estimator that numpy and Excel default to). Throughput is finished runs divided
by (last finished_at - first created_at).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import asyncpg

LATENCY_KINDS = ("queue", "start", "exec", "e2e", "grade", "e2e_graded")
GRADED_KINDS = ("grade", "e2e_graded")
TERMINAL = ("COMPLETED", "FAILED", "CANCELLED")

# One row per run of the scenario's task; the latest attempt and its evaluation joined on.
ROWS_SQL = """
SELECT r.id::text AS id, r.status, r.outcome, r.attempt_count,
       r.failure_reason, r.dead_letter_reason,
       EXTRACT(EPOCH FROM r.created_at)::float8 AS created,
       EXTRACT(EPOCH FROM r.claimed_at)::float8 AS claimed,
       EXTRACT(EPOCH FROM r.started_at)::float8 AS started,
       EXTRACT(EPOCH FROM r.finished_at)::float8 AS finished,
       EXTRACT(EPOCH FROM a.started_at)::float8 AS a_started,
       EXTRACT(EPOCH FROM a.finished_at)::float8 AS a_finished,
       a.failure_reason AS a_reason,
       e.status AS e_status, e.task_success, e.failure_category,
       EXTRACT(EPOCH FROM e.finished_at)::float8 AS e_finished
FROM runs r
LEFT JOIN LATERAL (
    SELECT * FROM run_attempts WHERE run_id = r.id ORDER BY attempt_no DESC LIMIT 1
) a ON true
LEFT JOIN attempt_evaluations e ON e.attempt_id = a.id
WHERE r.task_id = $1
ORDER BY r.created_at, r.id
"""

PROGRESS_SQL = """
SELECT count(*) FILTER (WHERE r.status IN ('COMPLETED','FAILED','CANCELLED')) AS terminal,
       count(*) AS total,
       count(*) FILTER (WHERE a.id IS NOT NULL AND a.status = 'FINISHED') AS finished_attempts,
       count(e.id) AS evaluations,
       count(*) FILTER (WHERE e.status IN ('PENDING','RUNNING')) AS grading_open
FROM runs r
LEFT JOIN LATERAL (
    SELECT * FROM run_attempts WHERE run_id = r.id ORDER BY attempt_no DESC LIMIT 1
) a ON true
LEFT JOIN attempt_evaluations e ON e.attempt_id = a.id
WHERE r.task_id = $1
"""


async def fetch_rows(conn: asyncpg.Connection, task_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in await conn.fetch(ROWS_SQL, task_id)]


async def fetch_progress(conn: asyncpg.Connection, task_id: str) -> dict[str, int]:
    return dict(await conn.fetchrow(PROGRESS_SQL, task_id))  # type: ignore[arg-type]


# -- pure maths ------------------------------------------------------------------------------


def percentile(values: Sequence[float], q: float) -> float:
    """q in [0, 100]; linear interpolation between closest ranks."""
    if not values:
        raise ValueError("percentile of an empty sequence")
    if not 0 <= q <= 100:
        raise ValueError("q must be within [0, 100]")
    s = sorted(values)
    pos = (len(s) - 1) * q / 100
    lo = math.floor(pos)
    hi = math.ceil(pos)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def describe(values: Sequence[float]) -> dict[str, float | int] | None:
    """n/min/mean/p50/p95/p99/max, or None when there is nothing to describe."""
    if not values:
        return None
    return {
        "n": len(values),
        "min": min(values),
        "mean": sum(values) / len(values),
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "p99": percentile(values, 99),
        "max": max(values),
    }


def throughput(
    first_created: float | None, last_finished: float | None, finished: int
) -> float | None:
    """Finished runs per second over [first created, last finished]."""
    if first_created is None or last_finished is None or finished == 0:
        return None
    window = last_finished - first_created
    return finished / window if window > 0 else None


def _diff(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else a - b


def run_latencies(row: dict[str, Any]) -> dict[str, float | None]:
    return {
        "queue": _diff(row.get("claimed"), row.get("created")),
        "start": _diff(row.get("started"), row.get("claimed")),
        "exec": _diff(row.get("a_finished"), row.get("a_started")),
        "e2e": _diff(row.get("finished"), row.get("created")),
        "grade": _diff(row.get("e_finished"), row.get("a_finished")),
        "e2e_graded": _diff(row.get("e_finished"), row.get("created")),
    }


def is_success(row: dict[str, Any], expected_outcome: str, graded: bool) -> bool:
    """COMPLETED with the workload's expected outcome (and, for graded workloads,
    a SUCCEEDED evaluation that says task_success)."""
    if row["status"] != "COMPLETED" or row["outcome"] != expected_outcome:
        return False
    if graded:
        return row.get("e_status") == "SUCCEEDED" and row.get("task_success") is True
    return True


def failure_reason(row: dict[str, Any], expected_outcome: str, graded: bool) -> str:
    """A short label for why a run did not succeed."""
    if row["status"] != "COMPLETED":
        parts = [row["status"], row.get("dead_letter_reason"), row.get("failure_reason"),
                 row.get("a_reason")]  # fmt: skip
        return ":".join(str(p) for p in parts if p)
    if row["outcome"] != expected_outcome:
        return f"outcome={row['outcome']}"
    if graded:
        return f"evaluation={row.get('e_status')}:{row.get('failure_category')}"
    return "unknown"


def summarize(
    rows: Sequence[dict[str, Any]], *, expected_outcome: str, graded: bool
) -> dict[str, Any]:
    n = len(rows)
    terminal = [r for r in rows if r["status"] in TERMINAL]
    lat: dict[str, list[float]] = {k: [] for k in LATENCY_KINDS}
    for r in rows:
        for kind, v in run_latencies(r).items():
            if v is None or (kind in GRADED_KINDS and not graded):
                continue
            lat[kind].append(v)
    created = [r["created"] for r in rows if r["created"] is not None]
    finished = [r["finished"] for r in terminal if r["finished"] is not None]
    graded_done = [r["e_finished"] for r in rows if r.get("e_finished") is not None]
    first_created = min(created) if created else None
    reasons: dict[str, int] = {}
    successes = 0
    for r in rows:
        if is_success(r, expected_outcome, graded):
            successes += 1
        else:
            label = failure_reason(r, expected_outcome, graded)
            reasons[label] = reasons.get(label, 0) + 1
    last_finished = max(finished) if finished else None
    return {
        "runs": n,
        "terminal": len(terminal),
        "succeeded": successes,
        "success_rate": successes / n if n else None,
        "retried": sum(1 for r in rows if r["attempt_count"] > 1),
        "retry_rate": sum(1 for r in rows if r["attempt_count"] > 1) / n if n else None,
        "window_s": _diff(last_finished, first_created),
        "throughput_rps": throughput(first_created, last_finished, len(finished)),
        "graded_throughput_rps": (
            throughput(first_created, max(graded_done), len(graded_done))
            if graded and graded_done
            else None
        ),
        "latency_s": {k: describe(v) for k, v in lat.items() if v or k in ("queue", "e2e")},
        "failures": dict(sorted(reasons.items())),
    }


def to_columns(rows: Sequence[dict[str, Any]], origin: float | None) -> dict[str, list[Any]]:
    """Compact raw timings: per-run latencies (s) plus the outcome, columnar.
    `created_s` / `finished_s` are offsets from the first created_at."""
    cols: dict[str, list[Any]] = {
        k: [] for k in ("created_s", "finished_s", *LATENCY_KINDS, "attempts", "status", "outcome")
    }
    for r in rows:
        lat = run_latencies(r)
        cols["created_s"].append(_diff(r["created"], origin))
        cols["finished_s"].append(_diff(r["finished"], origin))
        for k in LATENCY_KINDS:
            cols[k].append(lat[k])
        cols["attempts"].append(r["attempt_count"])
        cols["status"].append(r["status"])
        cols["outcome"].append(r["outcome"])
    return cols
