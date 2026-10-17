# Read API for the dashboard (Phase 6)

List endpoints and `GET /stats/overview`, which the web dashboard reads (ADR-010 C). All paths are under `/api/v1`; errors are RFC 7807-style `application/problem+json`; every route needs an API key with the `read` scope ([auth.md](auth.md)). Every response is built from a public schema (D11): no evaluation spec, lease field, storage key, failure detail or check internals can appear.

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
- `evaluations[]`: exactly the `GET /evaluations/{id}` body, including `progress`. Progress for a whole page is computed with three grouped queries, not one per batch.
- `workers[]`: `id`, `hostname`, `version`, `concurrency`, `started_at`, `last_heartbeat_at`, `stopped_at`, `stop_reason`, `alive`.

**Workers are ordered live first**: workers with `stopped_at` null, then stopped ones; inside each group newest `started_at` first, ties by id descending. `alive` is true when the worker has not stopped and its last heartbeat is newer than 3 x `AGENTEVAL_LEASE_TTL_S` (90 s by default), measured on the database clock. A worker that crashed shows `alive: false` with `stopped_at` null until the reconciler marks it `lost`.

## `GET /stats/overview?window_h=24`

`window_h` is 1-720 (default 24), else 422. Counts and queue depth are current; success, latency and usage cover the last `window_h` hours. Each group is one aggregate SQL statement; there are no per-row queries. An empty database returns zeros and nulls, not errors.

```json
{"window_h": 24, "generated_at": "…",
 "evaluations": {"total": 3, "cancel_requested": 0},
 "runs": {"total": 9, "by_status": {"QUEUED": 1, "CLAIMED": 0, "RUNNING": 0, "RETRYING": 0,
                                     "COMPLETED": 6, "FAILED": 2, "CANCELLED": 0}},
 "success": {"graded": 3, "passed": 1, "success_rate": 0.3333},
 "run_latency_ms": {"count": 7, "p50": 400.0, "p95": 670.0},
 "usage": {"attempts": 7, "cost_usd": "0.070000", "tokens_in": 700, "tokens_out": 140},
 "active_workers": 1,
 "queue": {"runs_queued": 1, "runs_retrying": 0, "evaluations_pending": 0, "total": 1}}
```

| Field | Definition |
|---|---|
| `evaluations.total` | All evaluation batches ever created. `cancel_requested`: those with a cancel request. Batches have no stored status (progress is derived), so there is no by-status split. |
| `runs.total`, `runs.by_status` | All runs, ad hoc and batch, command and agent, by current status. Every status is present, zero if none. All-time. |
| `success.graded` | Runs with status COMPLETED or FAILED and `finished_at` in the window whose latest attempt's evaluation has status SUCCEEDED. SKIPPED, ERROR, PENDING and RUNNING evaluations (and runs without one) are not graded, as in the batch summary (D7). |
| `success.passed` | Of those, the ones with `task_success` true. |
| `success.success_rate` | `passed / graded`; null when `graded` is 0. |
| `run_latency_ms` | `finished_at - started_at` in milliseconds, over COMPLETED and FAILED runs with a `started_at` and a `finished_at` in the window (a run that failed before starting has none). `count` is the sample size; `p50`, `p95` are `percentile_cont` (linear interpolation) and null when `count` is 0. It is the run's wall-clock from first start to its end, retries and backoff included, and excludes queue wait and grading. |
| `usage` | Sum over attempts with `finished_at` in the window: `attempts` (count), `cost_usd` (sum of attempt cost; command-task attempts have none), `tokens_in` and `tokens_out` (including cache reads and writes). Retried attempts are paid for and counted. |
| `active_workers` | Workers that count as `alive` (above). |
| `queue.runs_queued`, `queue.runs_retrying` | Runs currently QUEUED and RETRYING. |
| `queue.evaluations_pending` | Attempt evaluations currently PENDING (waiting for grading or for a retry). |
| `queue.total` | The sum of the three. |

## CORS

The API answers CORS for the origins in `AGENTEVAL_CORS_ORIGINS` (a JSON list, default `["http://localhost:3000"]`) and for no others. Only `GET` and `POST` are allowed, with the request headers `Content-Type` and `Idempotency-Key`; credentials are not allowed. A preflight from another origin, for another method or with other headers is refused (400).
