# API reference

The interactive OpenAPI docs are served at <http://localhost:8000/docs> on a running stack, and the schema there is the source of truth. This page lists every route and the rules a schema cannot express. Three pages go deeper:

- [auth.md](auth.md): keys, scopes, rate limits and key management.
- [evaluations.md](evaluations.md): datasets, evaluation specs, benchmark evaluations, results, summaries, compare, and run cancel, replay and listing.
- [read.md](read.md): the list endpoints, `/stats/overview` and `/stats/activity` that the dashboard reads, and the metric conventions (percentiles, rate denominators, simulated cost).

## Conventions

Errors are RFC 7807-style `application/problem+json`. Every `/api/v1` route needs an API key with the route's scope: `read` for `GET` (and for `POST /evaluations/estimate`, which writes nothing), `write` for creating, cancelling, replaying and freezing, and `admin` for `/keys`. A missing, invalid, revoked or expired key gets 401 with `WWW-Authenticate: Bearer`, a key without the scope gets 403, and a key over its per-minute budget gets 429 with `Retry-After` and `X-RateLimit-*`.

Request bodies are capped at `AGENTEVAL_MAX_REQUEST_BYTES` (default 5 MiB); a larger body gets 413 before it is parsed. A value Postgres cannot store, such as a NUL character, is a 422 with fixed text. Validation errors (422) never echo the submitted input, and errors inside an evaluation spec get a fixed message and a truncated location, so no hidden content comes back ([ADR-007](../decisions/ADR-007-evaluation-secrecy.md)).

CORS allows only the origins in `AGENTEVAL_CORS_ORIGINS` (default `http://localhost:3000`), for `GET` and `POST`, with the `Authorization`, `Content-Type` and `Idempotency-Key` request headers and no cookies.

## Routes

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/health`, `/ready` | Open (no key). Liveness (`{"status": "ok", "read_only": false, "public_demo": false, "demo_limits": null}`; the flags and limits describe the demo modes, see [auth.md](auth.md)); readiness checks Postgres and Redis (503 if either is down). `/metrics` is open too |
| POST, GET | `/api/v1/keys` | `admin`. Create a key (201; the full `key` is returned once) / list keys (never secrets) |
| DELETE | `/api/v1/keys/{prefix}` | `admin`. Revoke; 204, idempotent; 404 unknown |
| POST | `/api/v1/agents` | 201; 409 if the name is taken |
| GET | `/api/v1/agents` | Newest first, keyset-paged (`after`, `limit` 1–200); each agent has its `latest_version` (number, provider, model, tools); `{agents, next_after}` |
| GET | `/api/v1/agents/{id}` | |
| POST | `/api/v1/agents/{id}/versions` | 201 new version; 200 when an equivalent config already exists; 422 if it can't run |
| GET | `/api/v1/agents/{id}/versions`, `…/versions/{n}` | Versions are immutable |
| POST | `/api/v1/tasks` | `task_type` `command` (default) or `agent`. 201; 422 on validation or an image not on the allowlist |
| GET | `/api/v1/tasks` | Newest first, keyset-paged; filters `dataset_id`, `task_type`, `difficulty`; public fields only (`has_evaluation`, never the spec); `{tasks, next_after}` |
| GET | `/api/v1/tasks/{id}` | |
| POST | `/api/v1/runs` | Body `{task_id, agent_version_id?, max_attempts?}` (budget 1–10, default `AGENTEVAL_RUN_MAX_ATTEMPTS` = 3, pinned on the run), optional `Idempotency-Key` header |
| GET | `/api/v1/runs` | Newest first; filters `status`, `dead_letter` (the dead-letter view), `evaluation_id`, `task_id`; keyset-paged by an opaque `after` cursor, `limit` 1–200; `{runs, next_after}` |
| GET | `/api/v1/runs/{id}` | Includes `max_attempts`, `replay_count`, `dead_letter_reason`, `failure_reason`, `next_attempt_at` |
| POST | `/api/v1/runs/{id}/cancel` | 202 accepted (queued or retrying: cancelled now; claimed or running: flagged, the worker stops it) / 200 repeat / 409 already finished / 404 |
| POST | `/api/v1/runs/{id}/replay` | Give a FAILED run `additional_attempts` (1–9, default 1; total cap 10) more attempts and queue it again; optional `Idempotency-Key`; 202 replayed / 200 repeat of the key / 409 not FAILED or key reused / 422 over the cap / 404 |
| GET | `/api/v1/runs/{id}/attempts` | Exit code and output (command tasks); steps, tokens, cost and final answer (agent tasks) |
| GET | `/api/v1/runs/{id}/attempts/{n}/events` | The trajectory, paged by `after_seq` (default -1) and `limit` (1–1000, default 100); `next_after_seq` is null on the last page |
| GET | `/api/v1/runs/{id}/attempts/{n}/evaluation` | The attempt's public evaluation; 404 if none is recorded |
| POST | `/api/v1/datasets` | 201 DRAFT dataset; 409 if `(name, version)` exists |
| GET | `/api/v1/datasets` | Newest first, keyset-paged; filter `status`; `task_count` per dataset; `{datasets, next_after}` |
| GET | `/api/v1/datasets/{id}` | Includes `task_ids` |
| POST | `/api/v1/datasets/{id}/freeze` | DRAFT → FROZEN (idempotent); 409 if it has no tasks |
| POST | `/api/v1/evaluations` | A benchmark evaluation: agent version × frozen dataset × repetitions, with optional budgets `run_max_attempts` / `eval_max_attempts` (1–10, default 3) and spending cap `max_cost_usd` (default `AGENTEVAL_REAL_EVAL_MAX_COST_USD` for real models); optional `Idempotency-Key`; 202 new / 200 replay / 409 / 422 provider not enabled / 403 real model in the public demo / 503 |
| POST | `/api/v1/evaluations/estimate` | `read` scope. Upper bounds (runs, model requests, cost) and availability for a planned evaluation; writes nothing; 404 / 409 / 422 as for create |
| GET | `/api/v1/evaluations` | Newest first, keyset-paged; filters `agent_version_id`, `dataset_id`; each batch with its progress; `{evaluations, next_after}` |
| GET | `/api/v1/evaluations/{id}` | The batch, scoring config and hash, spending cap, `context` (agent, model, dataset, evaluator and pricing versions), derived progress |
| POST | `/api/v1/evaluations/{id}/cancel` | Cancel the batch and its unfinished runs; finished runs and scores are untouched; 202 / 200 repeat / 404 |
| GET | `/api/v1/evaluations/{id}/results` | Public per-run results, keyset-paged |
| GET | `/api/v1/evaluations/{id}/summary` | Success rate and confidence interval, failure categories, score means, latency (p50/p95/p99, mean), run rates, cost (labelled `simulated` for fake models), per task |
| GET | `/api/v1/evaluations/compare?ids=a,b` | 2–5 batches on the same dataset and scoring config (else 422); each with its context and whether its interval separates from the first; config differences and warnings |
| GET | `/api/v1/providers` | Model providers, whether this server runs them (and why not), models with list prices, `real_provider_concurrency`, `default_real_budget_usd` |
| GET | `/api/v1/workers` | Live workers first, keyset-paged; each has an `alive` flag (not stopped, heartbeat within 3 × lease TTL); `{workers, next_after}` |
| GET | `/api/v1/stats/overview` | `window_h` 1–720 (default 24): evaluation and run counts, success rate, run rates, run latency p50/p95/p99/mean, cost and tokens (also per provider), active and stale workers, queue depth and oldest queued age |
| GET | `/api/v1/stats/activity` | `window_h` 1–720 (default 24): finished runs per time bucket, zero-filled, and the evaluations created in the window |

`POST /tasks` also accepts `dataset_id`, `difficulty` and a write-only `evaluation` spec; responses only say `has_evaluation`. Runs carry `evaluation_id` and `repetition`, and attempts carry `sandbox_image_id`.

## Submitting a run

`POST /api/v1/runs` answers:

| Status | Meaning |
| --- | --- |
| 202 | A new run was created. |
| 200 | A replay of an existing `Idempotency-Key`. |
| 409 | The key was already used for a different task, agent version or `max_attempts`. |
| 404 | Unknown task or agent version. |
| 422 | An agent task without an `agent_version_id`, or a command task with one. |
| 503 | The run was persisted but dispatch failed. Retry with the same key and the run is re-dispatched. |

## Task limits

Tasks are validated at creation:

- `timeout_s` is 1–300 for command tasks and 1–1800 for agent tasks; `memory_mb` is 64–1024; `cpu_limit` is 0.1–2.0.
- `command` and `test_command` take up to 64 arguments.
- `files` is `{relative_path: content}`, with no absolute paths or `..`, at most 100 files and at most 1 MB in total.
- Agent tasks need `instructions` and have no `command`.

## Agent versions

Everything is checked when the version is created, so a bad config never reaches a worker:

- Tools must be from the built-in six.
- `shell` requires a non-empty `shell_allowlist` of bare program names, and an allowlist requires `shell`.
- The provider must be enabled and the model priced.
- Provider settings must be valid: a well-formed script for `fake`; for `anthropic`, the effort level, stored explicitly and defaulting to `high`.
- `limits` must be within the global caps: `AGENTEVAL_AGENT_MAX_STEPS_CAP` (100), `…_MAX_TOTAL_TOKENS_CAP` (2,000,000) and `…_MAX_COST_USD_CAP` (5.00). Over-cap limits are rejected, never clamped, so a version runs with exactly its stored config.

Per-version limits and their defaults:

| Limit | Default |
| --- | --- |
| `max_steps` | 30 |
| `max_total_tokens` | 500,000 |
| `max_cost_usd` | 1.00 |
| `max_tokens_per_call` | 16,000 |
| `tool_timeout_s` | 60 |
| `model_call_timeout_s` | 300 |
