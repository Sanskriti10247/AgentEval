# Evaluation architecture (Phase 4)

How AgentEval decides whether an agent solved a task, how it scores and compares agents, and what it guarantees. The decisions and their rationale are in [ADR-006](../decisions/ADR-006-evaluation-and-scoring.md) (evaluation and scoring) and [ADR-007](../decisions/ADR-007-evaluation-secrecy.md) (secrecy). The HTTP API is in [docs/api/evaluations.md](../api/evaluations.md).

> **Terminology.** A **benchmark evaluation** (Phase 4, built) runs one agent version over a frozen dataset and measures *agent quality*: success rate, scores, failure categories. A **performance benchmark** (Phase 7, not built) will measure *platform* throughput, latency and worker scaling. This document is about the former only, and quotes no performance numbers.

## 1. Concepts

| Concept | Table | Notes |
|---|---|---|
| Dataset | `datasets` | `(name, version)` unique; `DRAFT → FROZEN`. Tasks join a dataset at creation (`dataset_id`), only while it's DRAFT. |
| Evaluation spec | `tasks.evaluation` (JSONB) | per agent task; internal and write-only (ADR-007). Agent tasks only; command tasks can't have one. |
| Evaluation batch | `evaluations` | agent version × frozen dataset × `repetitions` (1–10), with `scoring_config` and its SHA-256 hash; optional unique `idempotency_key`. |
| Batch run | `runs` (+ `evaluation_id`, `repetition`) | exactly one per (batch, task, repetition): `UNIQUE(evaluation_id, task_id, repetition)`. |
| Snapshot | `attempt_artifacts` (`kind='workspace'`) | the agent's final `/workspace` as tar.gz, ≤ 16 MiB, plus its sha256. Never extracted on the host. |
| Attempt evaluation | `attempt_evaluations` | one per attempt: `UNIQUE(attempt_id)`; status `PENDING`, `RUNNING`, `SUCCEEDED`, `ERROR` or `SKIPPED`. |
| Score | `scores` | one per (attempt evaluation, scoring-config hash); append-only. |

Batch progress and summaries are **derived** from runs, attempts, evaluations and scores. Nothing about them is stored.

## 2. Evaluation spec reference

```json
{
  "checks": [ ... 1-20 checks, unique names, at least one required ... ],
  "hidden_files": {"relative/path.py": "content"},
  "protected_paths": ["test_calc.py"],
  "timeout_s": 120
}
```

| Field | Rules |
|---|---|
| `checks` | 1–20; unique `name`s matching `^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$`; at least one `required` |
| `hidden_files` | ≤ 100 files, ≤ 1 MiB in total; relative, normalised paths; the names `.agenteval`, `.nopyc` and `conftest.py` are reserved for the evaluator |
| `protected_paths` | unique; each must be one of the task's own `files` (the integrity baseline is their original content) |
| `timeout_s` | 10–600 (default 120); the sum of per-check timeouts must not exceed it |
| whole spec | ≤ 2 MiB as stored (checked by the service before persistence; a CHECK constraint is the backstop) |

Every check has `name`, `weight` (in (0, 100], default 1) and `required` (default true).

| `kind` | Fields | Passes when |
|---|---|---|
| `python_tests` | `framework` `unittest` (default) or `pytest`; `pattern` (default `test*.py`, must match a hidden file); `expected_tests` 1–10,000; `timeout_s` 1–300 (default 60) | the evaluator's runner (`python -I -X pycache_prefix=/eval/.nopyc`) exits 0 **and** the parsed summary shows exactly `expected_tests` tests, all passing (C2, C3). Score = passed ÷ expected. |
| `io` | `argv` (`argv[0]` a bare program name, ≤ 64 args), `stdin` ≤ 64 KiB, `expected_stdout` ≤ 64 KiB, `match` `exact`, `normalized` (default), `contains` or `regex`, `expected_exit_code` (default 0), `timeout_s` (default 30) | stdout matches and the exit code is as expected, **compared on the host** (C4) |
| `command` | `argv`, `expected_exit_code`, `trusted_environment: true` (required), `timeout_s` (default 60) | the exit code matches. No in-process integrity guarantee (C6, C7). |
| `final_answer` | `match` `exact`, `normalized`, `contains`, `regex`, `number` (with `tolerance`) or `json_equals`; `expected` ≤ 64 KiB | the attempt's final answer matches |
| `file` | `path` (relative), `mode` `exists`, `equals`, `contains`, `sha256` or `json_equals`; `expected` (except for `exists`) | the file in the agent's final workspace matches. A symlink never matches. |

**Matching:**
- `normalized` ignores case and collapses whitespace.
- `regex` is a full match on stripped text, with DOTALL.
- Candidate text is capped at 64 KiB before matching.
- An author's regex runs on Python's `re`, which has no timeout. That ReDoS risk sits with the task author.

## 3. Lifecycle

```text
POST /evaluations ─► batch row + one QUEUED run per (task, repetition), ONE transaction ─► XADD each
worker: claim → attempt → AgentRunner (agent container, FakeProvider or a real model)
        COMPLETED outcome and the spec needs the workspace (A6):
          reap → normalise permissions (C1) → tar.gz export (≤ 16 MiB) → header validation
        close the agent container
        finish_attempt(outcome, aggregates, sandbox_image_id) → COMMIT
        EvaluationRunner.evaluate:
          tx A  snapshot artifact + attempt_evaluations row (ON CONFLICT DO NOTHING)
          tx B  PENDING → RUNNING (guarded UPDATE: exactly one evaluator grades)
                fresh evaluation container ─ evidence ─ checks ─ classify ─ score
          tx C  RUNNING → SUCCEEDED (+ score row) | ERROR, then remove the container
```

### Which row tx A creates

| Situation | Row | Category |
|---|---|---|
| The attempt FAILED (`SANDBOX_ERROR`, `MODEL_ERROR`, `INTERNAL_ERROR`) | `SKIPPED` | `platform_sandbox`, `platform_model`, `platform_internal`, or `platform_cancelled` (an `INTERNAL_ERROR` with a `cancelled:` reason) |
| The task has no spec (R7) | `SKIPPED` | the limit category if a limit was hit, otherwise none |
| The spec or the batch's scoring config is invalid | `ERROR` | `evaluation_error` |
| The spec needs the workspace but capture failed (too large, incomplete, invalid) | `ERROR` | `evaluation_error` (A2): the attempt stays COMPLETED |
| Otherwise | `PENDING` → graded | — |

A duplicate evaluator sees the row already exists (`ON CONFLICT DO NOTHING`) and stops. A second claim matches 0 rows. A replayed finish matches 0 rows, and a replayed score insert does nothing.

### Grading

1. **A6:** a spec with no executing check, no `file` check and no `protected_paths` is graded from the attempt row alone. There's no snapshot and no container.
2. **Otherwise, open a fresh `EvaluationSession`.** It validates the snapshot's sha256 and headers, and each `/eval` path, **before** a container is created. Then it creates and starts the container, installs and locks `/eval`, and restores `/workspace`.
3. **Collect evidence** for `file` checks and protected paths from the **pristine** snapshot. Only regular files within 64 KiB are read; symlinks, including symlinked parents, are never followed.
4. **Integrity:** each protected path must exist, not be a symlink, and match the sha256 of the task's original file. Otherwise the attempt is `tampering`.
5. **Run static checks** (`final_answer`, `file`), then each executing check (C5). Before each one, `/workspace` is restored and `/tmp` wiped. The check runs under the Phase 3 timeout wrapper and backstop, and the reaper always runs afterwards.
6. **Classify:**
   - `grade()` puts the results back in **spec order**, because public check positions rely on it.
   - It then runs `classify()` (the precedence in ADR-006) and, for SUCCEEDED, `components()` and `score()` under the batch's config (default: scoring v1).
7. **Bound the whole grading** by `spec.timeout_s` + 60 s for setup. Running past it is ERROR (`evaluation did not finish within …`). A check that runs past its **own** timeout is the candidate's failure (`tests_failed`).
8. **Clean up.** The container is removed in a shielded `finally` on success, error, timeout and cancellation. If `open()` is still running, its result is awaited and removed. Containers also carry the deadline label for the orphan sweep.

### The evaluation container

It has the agent container's settings (ADR-003): no network, the memory and CPU limits of the task, `pids_limit` 128, a read-only rootfs, uid 1000, all capabilities dropped, `no-new-privileges`, tini as PID 1. On top of that:

| Mount | Options |
|---|---|
| `/workspace` | tmpfs `rw,noexec,nosuid,nodev`, 64 MiB, uid 1000 |
| `/tmp` | tmpfs `rw,noexec,nosuid,nodev`, 16 MiB |
| `/eval` | tmpfs `rw,noexec,nosuid,nodev`, 8 MiB, owned by root; after installation, directories 0555 and files 0444, verified |

It is labelled `agenteval.role=evaluation`, plus the run and attempt ids.

### Snapshots

- **Export** (the agent's container, after the reaper):
  - `chmod -R -c u+rX` makes everything owner-readable; changed paths are recorded and tagged `permissions_normalized`.
  - `tar -czf` streams out with a size cap. Each phase has a 20 s timeout, so the export fits inside the executor's backstop even after a TIMEOUT outcome.
  - A timeout, a non-zero exit, **any tar warning on stderr**, going over the cap, or failing validation each raise `SnapshotError(incomplete | too_large | invalid)`.
- **Validation** (on the host, headers only, never extracted): it rejects absolute or `..` names, device nodes, hardlinks to anything outside the archive, and unknown member types, as well as more than 20,000 members or more unpacked data than the workspace holds.
- **Import:** the archive is streamed into `tar -xz --no-same-owner` inside the evaluation container, as uid 1000. Even an unvalidated archive can't write outside `/workspace` (tested).

## 4. Scoring

| Component | Definition |
|---|---|
| `task_success` | 1 if passed, else 0 |
| `test_score` | weighted mean of the check scores (null if no check has a score) |
| `reliability` | 1 − tool-error rate (`invalid_input`, `unknown_tool`, `denied` and `error` results ÷ tool calls); 1 if there were no tool calls |
| `efficiency` | 1 − mean(steps ÷ max_steps, cost ÷ max_cost_usd); **0 unless the task succeeded**, so failing fast earns nothing |

- **`overall`** = Σ weight × component, with null counted as 0. It uses Decimal arithmetic quantised to 5 places. **Tampering forces `overall` to 0** (`integrity_gate_applied`).
- **Scoring v1 weights** are 0.60, 0.20, 0.10 and 0.10. They are benchmark policy (D6), not universal truth. A batch can supply its own (`version: "v1"`, four weights that sum to exactly 1). The config is hashed (canonical JSON, SHA-256) and stored with every score.
- **Rescoring is deferred (R1).** The storage supports it (append-only, one row per config hash), but no endpoint exists.

**Trajectory metrics** (from the stored events) feed `reliability` and the diagnostic tags. They never decide success:

| Tag | Trigger |
|---|---|
| `loop_detected` | ≥ 3 identical consecutive tool calls |
| `high_tool_error_rate` | an error rate ≥ 0.25 over ≥ 4 calls |
| `never_ran_tests` | no successful `run_tests` call |
| `model_retries` | any `model_error` events |

## 5. Success-rate semantics (D7)

For one batch, every finished run counts once:

| Count | Meaning |
|---|---|
| `graded` | evaluations that SUCCEEDED: the agent was fairly tested |
| `passed` | graded with category `passed` |
| `success_rate` | passed ÷ graded, with a Wilson 95% interval (`ci95`); null when nothing is graded |
| `platform_failures` | `platform_*` (including a run that FAILED before any attempt). Excluded from the denominator; `platform_failure_rate` is over all finished attempts |
| `evaluation_errors` | ERROR evaluations: excluded and reported |
| `ungraded` | SKIPPED without a platform failure (tasks without a spec): excluded and reported |
| `in_progress` | runs not yet terminal, or terminal but with an evaluation that is missing, PENDING or RUNNING. Not counted yet |

So a dead model API can't make an agent look bad, a broken evaluator can't count against a candidate, and a flaky sandbox can't hide a bad agent.

## 6. Verification

| Layer | What's covered |
|---|---|
| Core unit and property tests | spec validation, matchers, parsers, `plan`/`judge`, integrity, metrics, classify precedence (table-driven), scoring, Wilson intervals, the public projection |
| Sandbox tests (real Docker) | C1 and C8, export/import fidelity, archive traversal (validated and unvalidated), hidden-file isolation, no network, mount flags and capabilities, a read-only `/eval` against 9 write attacks, timeout and reaping, snapshot completeness, the C2 attacks (`sitecustomize`, stdlib shadowing, forged and cross-module `.pyc`) |
| Runner integration tests | every row type above, A6, evaluator failures (container, snapshot cap, budget), concurrent duplicate evaluations, fencing, batch configs, `io` and `command` checks, D11 storage, logs and transcript, the invariant that evaluation never mutates runs, attempts or the trajectory |
| API integration tests | datasets and the freeze race, write-only specs, sanitised 422s, the 2 MiB limit, batch idempotency and dispatch failure, progress, results paging, summary and compare on seeded data, the D11 response-model snapshot and route sweep, a full flow through a real worker |
| E2E (running stack) | see below |

### E2E benchmark scenarios (`tests/e2e/`, `make e2e`)

Six tasks share one buggy `calc.py`, and their specs differ:

| Task | Spec |
|---|---|
| T1 | hidden tests + a protected `test_calc.py` |
| T2 | an `io` check + a `command` check |
| T3 | final answer only (A6) |
| T4 | a file check |
| T5 | an impossible hidden test |
| T6 | no spec |

Deterministic FakeProvider agents (`fixer` with 2 repetitions, `quitter`, `cheater`, `looper`, `model-error`, `hoarder`) run through the real API, worker and Docker:

| Scenario | Proves |
|---|---|
| S1 | every (agent, task) cell gets its expected category, outcome, steps, cost and tokens; tampering is caught while the hidden tests still pass |
| S2 | exact D7 summaries: counts, Wilson CI, histogram, cost, tokens, the per-task table |
| S3 | repetitions are deterministic; every `overall` equals Σ weight × component; a custom-weights batch uses its own hash |
| S4 | compare's per-task rates and deltas; 422 for a different dataset or scoring config |
| S5 | a model 401 gives `platform_model` (excluded); a 17 MiB incompressible workspace gives a COMPLETED run with an ERROR evaluation (A2), while a final-answer task by the same agent is graded with no container (A6) |
| S6 | concurrent and replayed batch creation gives one batch; duplicate dispatch still means exactly one attempt and one evaluation per run |
| S7 | dataset and task rules over HTTP; sanitised 422s, including over 2 MiB |
| S8 | D11: more than 100 GETs, trajectories included, never return hidden data |
| S9 | through a **SELECT-only** Postgres probe: record shapes, evaluation-after-attempt ordering, `sandbox_image_id` equal to the local image ID, snapshots exactly where expected, and no leftover containers |

"Evaluation never rewrites an attempt" can't be proven black-box, so the runner integration tests cover it with before/after snapshots.

## 7. Limitations and deferred work

| Item | Status |
|---|---|
| Rescoring through the API (D8) | **deferred (R1)** |
| Re-running checks from stored snapshots | deferred (needs a new job type) |
| Attempt retries, leases and heartbeats, recovery of stuck PENDING/RUNNING evaluations and of the A1 crash gap, cancellation API, dead-letter handling | **Phase 5** |
| Authentication, authorization, rate limiting | **Phase 8** |
| LLM-as-judge | **deferred (D5)** |
| Metrics, tracing, dashboards | Phase 6 |
| Performance and throughput benchmarks | Phase 7 |

Known as-built risks:
- **The crash gap and stuck evaluations** mean a batch never reports `complete`.
- **Mutable image tags at grading time.** The agent's resolved image ID is recorded, but grading isn't verified against it.
- **pytest checks fail closed** in an image without pytest.
- **C7 in-process cheating:** candidate code can read `/eval` while it's being graded.
- **`PublicCheck` scores and positions** reveal hidden test and check counts.
- **Summaries are computed in memory** over at most 1000 runs per batch.
- **A batch created without `Idempotency-Key`** whose dispatch fails can't be re-dispatched.
- **Shutdown during grading** marks it ERROR on a best-effort basis only.
- **An unexpected evaluator exception is logged with its traceback,** which may contain internal evaluator details. Logs are operator-only, never API or model-visible.
- **The sandbox is not a hardened multi-tenant boundary** (shared kernel, Docker socket on the worker).
