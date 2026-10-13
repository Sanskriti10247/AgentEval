# Evaluation API (Phase 4)

These are the dataset, evaluation-spec, benchmark-evaluation and result endpoints. All paths are under `/api/v1`. The OpenAPI schema at `/docs` is the source of truth; this page explains behaviour that a schema can't. The architecture is in [docs/architecture/evaluation.md](../architecture/evaluation.md).

- **Errors** are RFC 7807-style `application/problem+json`: `{type, title, status, detail}`.
- **Validation errors (422)** are sanitised; see [below](#validation-errors-422).
- **There is no authentication or rate limiting yet (Phase 8).**

Examples are trimmed from real responses of the README walkthrough (FakeProvider); ids and timings are elided (`…`).

## Datasets

| Method | Path | Result |
|---|---|---|
| POST | `/datasets` | `{name 1–200, version 1–50, description?}`. **201** with a DRAFT dataset; **409** if `(name, version)` exists |
| GET | `/datasets/{id}` | **200**; **404** |
| POST | `/datasets/{id}/freeze` | DRAFT → FROZEN. **200** (idempotent: freezing a FROZEN dataset returns it unchanged); **409** if the dataset has no tasks; **404** |

```json
{"id": "…", "name": "calc-demo", "version": "1", "description": null,
 "status": "FROZEN", "frozen_at": "…", "created_at": "…", "task_ids": ["…", "…"], "task_count": 2}
```

**Membership is fixed at task creation.** A task joins a dataset through `POST /tasks` with `dataset_id`, and only while the dataset is DRAFT. Adding a task holds the dataset row `FOR SHARE`, and freezing is a guarded `UPDATE`, so the two serialise: no task can ever join a frozen dataset. Datasets may contain tasks without a spec. Those stay ungraded (R7).

## Tasks with evaluation specs

`POST /tasks` (agent tasks) accepts three new fields:

| Field | Notes |
|---|---|
| `dataset_id` | a DRAFT dataset: **409** if it's frozen, **404** if unknown |
| `difficulty` | `easy`, `medium` or `hard` |
| `evaluation` | the evaluation spec ([reference](../architecture/evaluation.md#2-evaluation-spec-reference)). **Write-only.** |

**Validation, all before anything is stored:**
- the spec schema
- `protected_paths` must be task files: **422** `protected_paths must name task files`
- the whole spec must be ≤ 2 MiB as stored: **422** `evaluation spec exceeds 2097152 bytes`
- command tasks can't have `evaluation` or `dataset_id` (**422**)

**`TaskRead` (POST and GET)** adds `dataset_id`, `difficulty` and `has_evaluation: bool`. **It never contains the spec.**

## Evaluation batches

### `POST /evaluations`

```json
{"name": "calc-demo", "dataset_id": "…", "agent_version_id": "…", "repetitions": 2,
 "scoring_config": {"version": "v1", "weights": {"task_success": "0.6", "test_score": "0.2",
                                                "reliability": "0.1", "efficiency": "0.1"}}}
```

- **`scoring_config` is optional** and defaults to scoring v1. The weights must be ≥ 0 and sum to exactly 1.
- **Header `Idempotency-Key`** (optional, 1–255 characters).
- **One transaction** creates the batch row and one QUEUED run per (task, repetition). Each run's `idempotency_key` is `eval:{evaluation}:{task}:{repetition}`. The runs are then dispatched.

| Status | When |
|---|---|
| **202** | a new batch |
| **200** | a replay of the same key with the same request (name, dataset, agent version, repetitions, scoring hash). Runs that are still QUEUED are re-dispatched |
| **409** | the dataset isn't FROZEN; or the key was already used for a different request |
| **404** | unknown dataset or agent version |
| **422** | an invalid scoring config or repetitions (1–10); or more than 1000 runs (tasks × repetitions) |
| **503** + `Retry-After: 1` | the batch is stored but dispatch failed. Retry with the **same key** and the runs are re-dispatched |

**Concurrency and duplicates:**
- Concurrent requests with one key create exactly one batch.
- `UNIQUE(evaluation_id, task_id, repetition)` guarantees one run per cell.
- A duplicate stream message is harmless: the claim is conditional, and each attempt has at most one evaluation.
- **A batch created without a key whose dispatch fails can't be re-dispatched** (a known limitation, the same as `POST /runs`).

### `GET /evaluations/{id}`

The batch, plus **derived** progress (never stored):

```json
{"id": "…", "name": "calc-demo", "dataset_id": "…", "agent_version_id": "…", "repetitions": 2,
 "scoring_config": {"version": "v1", "weights": {"task_success": "0.60", "test_score": "0.20",
                                                "reliability": "0.10", "efficiency": "0.10"}},
 "scoring_config_hash": "6a5118ea…", "created_at": "…",
 "progress": {"runs_total": 4,
              "runs_by_status": {"QUEUED": 0, "CLAIMED": 0, "RUNNING": 0, "COMPLETED": 4, "FAILED": 0},
              "evaluations_by_status": {"PENDING": 0, "RUNNING": 0, "SUCCEEDED": 4, "ERROR": 0, "SKIPPED": 0},
              "awaiting_evaluation": 0, "complete": true}}
```

- **`scoring_config` and its hash are exposed** as benchmark policy (R3).
- **`complete`** means every run is terminal and none awaits evaluation. A crash between an attempt's commit and its evaluation record keeps `complete` false (a recovery gap, Phase 5).

### `GET /evaluations/{id}/results?after=&limit=`

One row per run, ordered by (task, repetition). It's keyset-paged: `limit` 1–500 (default 100), and `next_after` is an opaque cursor, null on the last page. A malformed cursor is **422**.

```json
{"run_id": "…", "task_id": "…", "repetition": 1, "run_status": "COMPLETED", "run_outcome": "AGENT_FINISHED",
 "attempt_no": 1, "duration_ms": …, "steps": 3, "tokens_in": 300, "tokens_out": 60, "cost_usd": "0.000600",
 "evaluation": {
   "status": "SUCCEEDED", "task_success": true, "failure_category": "passed", "tags": [],
   "checks": [{"position": 1, "passed": true, "score": 1.0, "duration_ms": …}],
   "score": {"overall": "0.98497", "task_success": "1", "test_score": "1.00000", "reliability": "1.00000",
             "efficiency": "0.84970", "integrity_gate_applied": false},
   "metrics": {"model_calls": 3, "model_errors": 0, "tool_calls": 2, "tool_status_counts": {"ok": 2},
               "agent_tool_errors": 0, "tool_error_rate": 0.0, "max_identical_streak": 1,
               "loop_detected": false, "ran_tests": true, "last_tests_passed": true, "limits_reached": []}}}
```

- **`evaluation` is a `PublicEvaluation`,** or null when no evaluation is recorded yet (or the run failed before any attempt).
- **`score`** is the score under *this batch's* scoring config.
- **`checks`** are identified by position only.

### `GET /evaluations/{id}/summary`

```json
{"evaluation_id": "…", "complete": true, "attempts": 4, "graded": 4, "passed": 4,
 "success_rate": 1.0, "ci95": [0.5101091635454027, 1.0],
 "platform_failures": 0, "platform_failure_rate": 0.0, "evaluation_errors": 0, "ungraded": 0, "in_progress": 0,
 "mean_score": {"overall": "0.98497", "task_success": "1.00000", "test_score": "1.00000",
                "reliability": "1.00000", "efficiency": "0.84970"},
 "latency_ms": {"p50": …, "p95": …}, "cost_usd": {"total": "0.002400", "mean_per_attempt": "0.000600"},
 "tokens": {"tokens_in": 1200, "tokens_out": 240}, "failure_categories": {"passed": 4},
 "per_task": [{"task_id": "…", "task_name": "hidden-tests", "difficulty": null, "attempts": 2, "graded": 2,
               "passed": 2, "success_rate": 1.0, "mean_overall": "0.98497"}, …]}
```

**How the counts are defined (D7):**
- **`success_rate`** = passed ÷ graded, with a Wilson 95% `ci95`.
- **`platform_failures`, `evaluation_errors` and `ungraded`** (tasks without a spec) are excluded from the denominator and reported separately.
- **`in_progress`** attempts aren't counted yet.
- **`mean_score`** averages graded attempts under the batch's config.
- **`latency_ms`** is the nearest-rank p50/p95 of attempt `duration_ms`.
- **`failure_categories`** uses only the closed `FailureCategory` vocabulary.

Exact definitions are in the [architecture doc](../architecture/evaluation.md#5-success-rate-semantics-d7).

### `GET /evaluations/compare?ids=a,b[,…]`

Takes 2–5 distinct batch ids. **They must share a dataset and a scoring-config hash, otherwise 422.** The response has each batch's full summary, plus per-task success rates and deltas against the first id:

```json
{"dataset_id": "…", "scoring_config_hash": "6a5118ea…",
 "evaluations": [{"evaluation_id": "E1", "agent_version_id": "…", "summary": {…}}, …],
 "per_task": [{"task_id": "…", "success_rate": {"E1": 1.0, "E2": 0.0}, "delta_vs_first": {"E1": 0.0, "E2": -1.0}}]}
```

A task with nothing graded has a null rate and a null delta.

## Runs and attempts (additions)

| Endpoint | Additions |
|---|---|
| `GET /runs/{id}` | `evaluation_id`, `repetition` (null for ad-hoc runs) |
| `GET /runs/{id}/attempts` | `sandbox_image_id`: the image ID the agent's sandbox actually ran (agent attempts) |
| `GET /runs/{id}/attempts/{n}/evaluation` | the attempt's `PublicEvaluation` + `{attempt_no, created_at, finished_at}`, scored under the run's batch config (or scoring v1 for ad-hoc runs). **404** when no evaluation is recorded (command tasks, not yet evaluated, or the run failed before an attempt) |

Ad-hoc agent runs (`POST /runs`) of a task that has a spec are evaluated the same way, without belonging to a batch.

## Public projection rules (D11)

Hidden evaluation data never leaves the evaluator ([ADR-007](../decisions/ADR-007-evaluation-secrecy.md)):

1. **Every evaluation in a response is built by `public_view()`.** `AttemptEvaluationRead` adds only the attempt number and timestamps.
2. **Every route has a typed response model** made of `Public*` types, ids, closed-vocabulary enums and numbers. A test freezes the field set of every reachable model.
3. **Specs are write-only.** No response contains hidden files, check names, expected values, argv, stdin, timeouts or protected paths.
4. **Aggregates use closed vocabularies only.**
5. **Snapshots are never served.**
6. **422 responses are sanitised** (next section).
7. **Service error messages carry ids and states only.**
8. **Enforced by tests:** a route sweep with a planted marker across every route (including error paths), and an E2E crawl of the running stack.

**A public evaluation may reveal:** the status; `task_success`; the failure category and tags (closed vocabulary); per-position `passed`, `score` and `duration_ms`; score components; trajectory metrics.

**It never reveals:** check names, kinds, expected or actual text, details, error messages, `failure_detail`, or integrity paths.

## Validation errors (422)

Every 422 is `{"detail": [{"type", "loc", "msg"}]}`, **without `input` or `ctx`**. FastAPI's default would echo the submitted value.

For errors inside `body.evaluation`:
- **`loc`** keeps at most a known spec field and a list index, for example `["body", "evaluation", "checks", 2]`. Dict keys (hidden file names) and unknown keys are dropped.
- **`msg`** is always `invalid evaluation spec (<type>)`, because validator messages can quote check names or patterns.

```json
{"detail": [{"type": "value_error", "loc": ["body", "evaluation", "hidden_files"],
             "msg": "invalid evaluation spec (value_error)"}]}
```

Errors outside the spec keep their message, but still never their input.
