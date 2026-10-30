# Read API for the dashboard (Phase 6)

List endpoints, `GET /stats/overview` and `GET /stats/activity`, which the web dashboard reads (ADR-010 C). All paths are under `/api/v1`; errors are RFC 7807-style `application/problem+json`; every route needs an API key with the `read` scope ([auth.md](auth.md)). Every response is built from a public schema (D11): no evaluation spec, lease field, storage key, failure detail or check internals can appear.

## Lists

| Method | Path | Filters | Response |
|---|---|---|---|
| GET | `/agents` | none | `{agents, next_after}` |
| GET | `/tasks` | `dataset_id`, `task_type` (`command`, `agent`), `difficulty` (`easy`, `medium`, `hard`) | `{tasks, next_after}` |
| GET | `/datasets` | `status` (`DRAFT`, `FROZEN`) | `{datasets, next_after}` |
| GET | `/evaluations` | `agent_version_id`, `dataset_id` | `{evaluations, next_after}` |
| GET | `/workers` | none | `{workers, next_after}` |

`GET /agents/{id}/versions` is unchanged (all versions of one agent).

**Pagination** is the same as `GET /runs`: keyset, `limit` 1-200 (default 50), and an opaque `after` cursor taken from the previous page's `next_after` (null on the last page). The list key is named after the resource, like `runs` in `GET /runs`. Agents, tasks, datasets and evaluations are ordered newest first on `(created_at, id)` descending. Pages never repeat or skip a row, even while rows are inserted. A malformed `after`, an out-of-range `limit` or an unknown filter value is **422**.

**Item shapes.**
- `agents[]`: `id`, `name`, `created_at`, `latest_version` (`id`, `version`, `model_provider`, `model`, `tools`; null if the agent has no version yet). The latest version is the highest version number; one query serves the whole page.
- `tasks[]`: `id`, `name`, `description`, `task_type`, `difficulty`, `dataset_id`, `image`, `timeout_s`, `memory_mb`, `cpu_limit`, `created_at`, `has_evaluation`. No files, instructions, commands or evaluation spec; `GET /tasks/{id}` returns the full public task.
- `datasets[]`: `id`, `name`, `version`, `description`, `status`, `frozen_at`, `created_at`, `task_count` (no `task_ids`).
- `evaluations[]`: exactly the `GET /evaluations/{id}` body, including `progress`, `max_cost_usd` and `context`. Progress for a whole page is computed with three grouped queries and the contexts with one, not one per batch.
- `workers[]`: `id`, `hostname`, `version`, `concurrency`, `started_at`, `last_heartbeat_at`, `stopped_at`, `stop_reason`, `alive`.

**Workers are ordered live first**: workers with `stopped_at` null, then stopped ones; inside each group newest `started_at` first, ties by id descending. `alive` is true when the worker has not stopped and its last heartbeat is newer than 3 x `AGENTEVAL_LEASE_TTL_S` (90 s by default), measured on the database clock. A worker that crashed shows `alive: false` with `stopped_at` null until the reconciler marks it `lost`.

## Metric conventions

These hold for every figure in this page and in the evaluation summary ([evaluations.md](evaluations.md#get-evaluationsidsummary)).

- **Percentiles are nearest-rank.** The p-th percentile of n values is the value at rank ceil(p x n) (1-based) of the values sorted ascending, so it is always a value that was observed. In SQL this is `percentile_disc`. With 7 values, p50 is the 4th and p95 and p99 are the 7th. Nothing is interpolated, so a p95 is never a duration that no run had.
- **Pass rate** (`success_rate`) is passed / graded. Graded means the latest attempt of a COMPLETED or FAILED run has an evaluation with status SUCCEEDED. Unfinished runs, ungraded attempts (no evaluation spec), platform failures and evaluator errors are not in the denominator (D7).
- **Finished** means a run in a terminal state other than CANCELLED: COMPLETED or FAILED.
- **Run rates** are over finished runs: `completion_rate` = COMPLETED / finished, `failure_rate` = FAILED / finished, `timeout_rate` = finished runs whose outcome is TIMEOUT / finished (a timed-out run is COMPLETED: the agent hit the task's time limit), `retry_rate` = finished runs whose final attempt number is above 1 / finished (a run retried after a platform failure, or replayed).
- **Every rate is null when its denominator is 0**, and every rate comes with the counts it was computed from.
- **Simulated** means the agent version's `model_provider` is `fake`. The fake provider has a price row of its own (`agent/pricing.py`) so budgets and costs exercise real arithmetic; that cost is simulated, never money spent, and responses label it with `simulated: true` and keep it apart from real cost where they break cost down.

## `GET /stats/overview?window_h=24`

`window_h` is 1-720 (default 24), else 422. Counts and queue depth are current; success, latency, rates and usage cover the last `window_h` hours. Each group is one aggregate SQL statement; there are no per-row queries. An empty database returns zeros and nulls, not errors.

```json
{"window_h": 24, "generated_at": "…",
 "evaluations": {"total": 3, "cancel_requested": 0},
 "runs": {"total": 9, "by_status": {"QUEUED": 1, "CLAIMED": 0, "RUNNING": 0, "RETRYING": 0,
                                     "COMPLETED": 6, "FAILED": 2, "CANCELLED": 0}},
 "success": {"graded": 3, "passed": 1, "success_rate": 0.3333},
 "run_latency_ms": {"count": 7, "p50": 400.0, "p95": 700.0, "p99": 700.0, "mean": 400.0},
 "rates": {"finished": 8, "completed": 6, "failed": 2, "timed_out": 0, "retried": 0,
           "completion_rate": 0.75, "failure_rate": 0.25, "timeout_rate": 0.0, "retry_rate": 0.0},
 "usage": {"attempts": 7, "cost_usd": "0.070000", "tokens_in": 700, "tokens_out": 140,
           "by_provider": {"fake": {"attempts": 7, "tokens_in": 700, "tokens_out": 140,
                                    "cost_usd": "0.070000", "simulated": true}}},
 "active_workers": 1,
 "workers": {"alive": 1, "stale": 1},
 "queue": {"runs_queued": 1, "runs_retrying": 0, "evaluations_pending": 0, "total": 1,
           "oldest_queued_s": 12.4}}
```

| Field | Definition |
|---|---|
| `evaluations.total` | All evaluation batches ever created. `cancel_requested`: those with a cancel request. Batches have no stored status (progress is derived), so there is no by-status split. |
| `runs.total`, `runs.by_status` | All runs, ad hoc and batch, command and agent, by current status. Every status is present, zero if none. All-time. |
| `success.graded` | Runs with status COMPLETED or FAILED and `finished_at` in the window whose latest attempt's evaluation has status SUCCEEDED. SKIPPED, ERROR, PENDING and RUNNING evaluations (and runs without one) are not graded, as in the batch summary (D7). |
| `success.passed` | Of those, the ones with `task_success` true. |
| `success.success_rate` | `passed / graded`; null when `graded` is 0. |
| `run_latency_ms` | `finished_at - started_at` in milliseconds, over COMPLETED and FAILED runs with a `started_at` and a `finished_at` in the window (a run that failed before starting has none). `count` is the sample size; `p50`, `p95`, `p99` are nearest-rank (`percentile_disc`) and `mean` the arithmetic mean, all null when `count` is 0. It is the run's wall-clock from first start to its end, retries and backoff included, and excludes queue wait and grading. |
| `rates` | Run rates (see the conventions above) over COMPLETED and FAILED runs whose `finished_at` is in the window: `finished`, `completed`, `failed`, `timed_out`, `retried` and the four rates. `retried` uses the run's `attempt_count`, which is the number of its final attempt. |
| `usage` | Sum over attempts with `finished_at` in the window: `attempts` (count), `cost_usd` (sum of attempt cost; command-task attempts have none), `tokens_in` and `tokens_out` (including cache reads and writes). Retried attempts are paid for and counted. The totals mix simulated and real cost. |
| `usage.by_provider` | The same sums per agent version `model_provider`, each with `simulated` (true for `fake`). Only agent attempts appear: a command task's run has no agent version, so the per-provider figures add up to the totals minus command-task attempts. A provider with no attempt in the window is absent. |
| `active_workers` | Workers that count as `alive` (above). |
| `workers.alive`, `workers.stale` | Workers not stopped whose last heartbeat is within the liveness window (3 x `AGENTEVAL_LEASE_TTL_S`), and those whose heartbeat is older. `alive` equals `active_workers`. A stale worker has probably crashed; the reconciler marks it stopped (`lost`). |
| `queue.runs_queued`, `queue.runs_retrying` | Runs currently QUEUED and RETRYING. |
| `queue.evaluations_pending` | Attempt evaluations currently PENDING (waiting for grading or for a retry). |
| `queue.total` | The sum of the three. |
| `queue.oldest_queued_s` | Seconds since the oldest QUEUED run was created (database clock); null when nothing is QUEUED. A run put back to QUEUED by a replay or a retry keeps its creation time, so this measures how long work has been outstanding, not the age of the latest dispatch. |

## `GET /stats/activity?window_h=24`

What the dashboard draws as live activity. `window_h` is 1-720 (default 24), else 422.

```json
{"window_h": 24, "bucket_s": 3600, "generated_at": "…",
 "buckets": [{"start": "2026-10-30T09:00:00Z", "finished": 4, "completed": 3, "failed": 1,
              "cancelled": 0, "graded": 3, "passed": 2, "p95_ms": 900.0}, …],
 "evaluations": [{"id": "…", "name": "calc-demo", "created_at": "…", "agent_name": "coder",
                  "agent_version": 2, "provider": "fake", "model": "fake-model", "simulated": true,
                  "runs_total": 10, "graded": 8, "passed": 6, "success_rate": 0.75,
                  "p95_ms": 800.0}]}
```

- **`bucket_s`** is 3600 for a window up to 48 h, 21600 up to 7 days and 86400 beyond.
- **`buckets`** cover the whole window, oldest first, with empty ones present and zero-filled. Bucket boundaries are multiples of `bucket_s` in Unix time (whole UTC hours or days), from the boundary at or before `now - window_h` to the one containing now, so there are `window_h x 3600 / bucket_s` buckets, plus one when the window does not start on a boundary. The first bucket can therefore reach back up to one bucket before the window.
- **Runs are bucketed by `finished_at`.** `finished`, `completed` and `failed` count COMPLETED and FAILED runs (as in `rates`); `cancelled` counts CANCELLED runs that have a `finished_at` (one cancelled while QUEUED or RETRYING, or stopped by its worker). `graded` and `passed` follow the same rule as `success` above. `p95_ms` is the nearest-rank p95 of run latency (`finished_at - started_at`) of the bucket's finished runs, null when there is none.
- **`evaluations`** are the batches created in the window, newest first, at most 50. `runs_total` counts all of the batch's runs, `graded` and `passed` follow the pass-rate rule, `success_rate` is null when nothing is graded, and `p95_ms` is the nearest-rank p95 of the `duration_ms` of the finished runs' latest attempts (the same sample as the summary's `latency_ms`).

## CORS

The API answers CORS for the origins in `AGENTEVAL_CORS_ORIGINS` (a JSON list, default `["http://localhost:3000"]`) and for no others. Only `GET` and `POST` are allowed, with the request headers `Content-Type` and `Idempotency-Key`; credentials are not allowed. A preflight from another origin, for another method or with other headers is refused (400).
