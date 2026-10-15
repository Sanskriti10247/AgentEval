# ADR-006: Evaluation as a separate record, graded in a fresh container

**Status:** Accepted (Phase 4, implemented)

> **Proposed change:** [ADR-008](ADR-008-artifact-storage-and-trajectory-fence.md) would move snapshot bytes (D3) out of Postgres onto an artifact volume, keeping the metadata row. Until it is accepted, D3 stands.

Related: ADR-003 (sandbox isolation), ADR-005 (agent runtime), ADR-007 (evaluation secrecy). The full reference is [docs/architecture/evaluation.md](../architecture/evaluation.md); the API is in [docs/api/evaluations.md](../api/evaluations.md).

## Context

Phase 3 records *what an agent did*. Phase 4 has to say *whether it solved the task*, objectively and reproducibly, and how agents compare on the same tasks. Inspecting the Phase 3 code showed that three things in the original plan couldn't work as written:

1. **`AgentRunner` closes the sandbox when the attempt ends.** Grading in that container would need a hook before the close, and the trajectory alone can't be re-checked later: it records actions, not the workspace the agent left behind.
2. **An `evaluation_failed` run status conflicts with the state machine.** `COMPLETED` and `FAILED` are terminal, enforced by CHECK constraints, and `status_for()` is the single outcome-to-status mapping.
3. **The agent owns `/workspace`.** It can rewrite visible tests or drop a `sitecustomize.py` there. Grading inside the agent's own container would mean grading in an environment the agent shaped.

## Decisions

The decisions were approved before implementation (D1–D11), refined by a spike (C1–C8), and amended at review in Steps 5 and 6 (A1, A2, A5, A6; R1, R2, R3, R7).

| # | Decision |
|---|---|
| D1 | **Evaluation is a separate record** (`attempt_evaluations`) with its own status, written only after `finish_attempt` has committed. The run state machine is unchanged. |
| D2 | **Grade in a fresh evaluation container.** The snapshot goes into `/workspace`; hidden files go into `/eval`, root-owned and read-only. Never the agent's container. |
| D3 | **Workspace snapshots are stored in Postgres** (`attempt_artifacts`, tar.gz, at most 16 MiB compressed). |
| D4 | **Only agent tasks are evaluated.** Command tasks (S1) are unchanged. |
| D5 | **LLM-as-judge is deferred.** Grading is deterministic only. |
| D6 | **Scoring v1:** 0.60 task_success + 0.20 test_score + 0.10 reliability + 0.10 efficiency. These are *benchmark-policy* weights: configurable per evaluation batch, hashed, and stored with every score. |
| D7 | **Success rate = passed ÷ graded,** with a Wilson 95% interval. Platform failures, evaluator errors and ungraded attempts are excluded from the denominator and reported separately. |
| D8 | **Scores are append-only, with one row per (evaluation, config hash),** so database-only rescoring is possible. *The rescoring endpoint is deferred (R1).* |
| D9 | **Datasets go DRAFT → FROZEN.** Evaluation batches require a frozen dataset, and compare requires the same dataset and the same scoring hash. |
| D10 | **One primary failure category per attempt, from a fixed precedence;** other matches become tags (see the taxonomy below). |
| D11 | **Strict secrecy:** hidden evaluation data never appears in API responses or model-visible data. See ADR-007. |

### Spike findings applied (C1–C8)

| # | Finding → rule |
|---|---|
| C1 | **A snapshot is complete or fails explicitly.** Unreadable files and directories are made owner-readable first (`chmod -R u+rX`, with the changed paths recorded). Any tar failure, *including a warning* (GNU tar skips unix sockets with exit 0), is an explicit `SnapshotError`. |
| C2 | **Hidden Python tests run as** `python -I -X pycache_prefix=/eval/.nopyc` from an evaluator-generated runner. `/workspace` is *appended* to `sys.path`, and `/eval/.nopyc` can't be created, so no bytecode is read or written. (The spike forged a later hidden test's `.pyc` through a writable cache.) |
| C3 | **A `python_tests` check passes only on exit 0 AND a parsed summary showing exactly `expected_tests` tests, all passing.** A missing summary is a failure. |
| C4 | **`io` checks compare stdout and the exit code on the host,** outside the candidate's process. |
| C5 | **Static checks run first. Before each executing check, `/workspace` is restored from the snapshot and `/tmp` is wiped.** |
| C6 | **`argv[0]` must be a bare program name.** `command` checks require `trusted_environment: true`, because they have no in-process integrity guarantees. |
| C7 | **Residual risk, documented rather than claimed fixed:** candidate code runs *inside* the test process, so it can still cheat in-process (for example by monkeypatching the test framework). `io` checks are the robust choice when integrity matters most. |
| C8 | **Agent-task files get real mtimes.** S1 command-task files deliberately keep mtime 0, so S1 execution stays identical. |

### Review amendments

| # | Decision |
|---|---|
| A1 | **The evaluation record is written in its own transaction(s) after `finish_attempt` commits.** A failure to persist the evaluation can never roll back the attempt's outcome. This leaves a documented crash gap (see Consequences). |
| A2 | **Snapshot failure (`too_large`, `incomplete`, `invalid`) is an evaluator failure:** the attempt stays COMPLETED, and the evaluation is ERROR / `evaluation_error`. It is never a candidate failure. |
| A5 | **`run_attempts.sandbox_image_id`** records the image ID Docker resolved for the agent's container. |
| A6 | **No container for final-answer-only specs.** A spec with no executing check, no `file` check and no `protected_paths` takes no snapshot and opens no evaluation container. |
| Invariant | **Evaluation never writes `runs`, `run_attempts` or `trajectory_events`.** |
| R1 | **Rescoring stays deferred** (D8 is a future capability). |
| R2 | **Validation errors are sanitised** (ADR-007). |
| R3 | **`scoring_config` and its hash are exposed** through the API as benchmark policy, not hidden evaluator configuration. |
| R7 | **Datasets may contain tasks without a spec.** Those tasks are ungraded and excluded from the success-rate denominator, but reported separately. |

## Architecture (as built)

```text
Executor.handle (agent task)
  AgentRunner.run
    runtime.run(ctx)                                         (Phase 3, unchanged)
    COMPLETED outcome and the spec needs the workspace (A6):
      reap → export_workspace (C1) → WorkspaceCapture        (snapshot or reason)
    session.close()
  finish_attempt(... sandbox_image_id) → COMMIT               ← the outcome is durable
  EvaluationRunner.evaluate(task, attempt, capture)           ← never raises into the executor
    tx A  attempt_artifacts (snapshot) + attempt_evaluations  ON CONFLICT DO NOTHING
            SKIPPED  platform failure, or no spec
            ERROR    invalid spec, invalid batch scoring config, capture failed (A2)
            PENDING  otherwise
    tx B  PENDING → RUNNING                                    (exactly one winner)
          EvaluationSession (fresh hardened container) → EvaluationWorkspace
            evidence from the pristine snapshot → integrity (protected paths)
            static checks; then each Step 3 ExecPlan on a restored workspace (C5)
          grade(): spec order → classify() → components() + score()
    tx C  RUNNING → SUCCEEDED (+ one scores row) | ERROR
          the container is always removed (success, error, timeout, cancellation)
```

**Where the code lives:**
- **The pure domain** is in `agenteval_core/evaluation/`: spec, checks (plan and judge), matchers, parsers, integrity, metrics, classify, scoring, stats, public, taxonomy.
- **The sandbox side** is `agenteval_worker/sandbox/evaluation.py` (`EvaluationSession` subclasses `SandboxSession`, sharing the timeout wrapper, backstop, reaper and labels) and `sandbox/workspace.py` (`EvaluationWorkspace`).
- **The runner** is `agenteval_worker/evaluation_runner.py`.
- **Persistence** is `repositories/evaluations.py` (writes) and `repositories/batches.py` (the read model).

**Failure attribution:**
- **Platform:** the attempt FAILED. The evaluation is SKIPPED / `platform_*`.
- **Evaluator:** the evaluator couldn't capture, open, drive or finish. The evaluation is ERROR / `evaluation_error`.
- **Candidate:** a check failed, including a check that ran past its *own* timeout. The evaluation is SUCCEEDED with `task_success = false` and a task category.

### Failure taxonomy (D10), precedence top to bottom

| Group | Category | When |
|---|---|---|
| platform | `platform_sandbox` / `platform_model` / `platform_internal` / `platform_cancelled` | the attempt's outcome was `SANDBOX_ERROR` / `MODEL_ERROR` / `INTERNAL_ERROR` (with a `cancelled:` reason for the last) |
| evaluation | `evaluation_error` | a check or the integrity check couldn't run, or the evaluator failed |
| integrity | `tampering` | a protected task file is missing, was replaced by a symlink, or changed (score forced to 0) |
| — | `passed` | every required check passed and integrity holds |
| limits | `max_steps` / `budget_exceeded` / `timeout` | the attempt's outcome |
| behaviour | `refusal` / `no_answer` | `MODEL_REFUSAL`; finished with no answer where a final-answer check exists |
| task result | `tests_failed` / `wrong_answer` / `missing_output` | the first failing required check |

Diagnostic tags (never a primary category): `loop_detected`, `high_tool_error_rate`, `never_ran_tests`, `model_retries`, `permissions_normalized`.

### Scoring v1 (D6)

Components are each in [0, 1] and stored separately:

| Component | Definition |
|---|---|
| `task_success` | 1 if the evaluation passed, else 0 |
| `test_score` | weighted mean of the check scores (`python_tests` gives passed ÷ expected) |
| `reliability` | 1 − the agent's tool-error rate |
| `efficiency` | 1 − mean(steps ÷ max_steps, cost ÷ max_cost); **0 unless the task succeeded** |

`overall` = Σ weight × component, as Decimal quantised to 5 places. **Tampering forces `overall` to 0.**

## Alternatives considered

| Choice | Alternative | Why this one |
|---|---|---|
| A separate evaluation record after the commit | An `evaluation_failed` run status | The run state machine stays intact, the outcome is durable first, and evaluator errors stay distinguishable from agent failures |
| A fresh evaluation container | The agent's own container | Grading isn't done in an environment the agent shaped; hidden files never coexist with the agent; it enables future re-checks from the snapshot |
| Snapshots in Postgres `bytea` | An object store (e.g. MinIO) | No new infrastructure. The cap bounds growth; this is the migration point if it ever matters |
| An in-process evaluator in the worker | A separate evaluator service or queue | Not needed at this scale; remains the scale-out path |
| Deterministic checks only | An LLM judge now | Cost, non-determinism and judge bias; and with no real model calls in the standard workflow, it could only be exercised with fakes |

## Consequences

- **A run shows COMPLETED a few seconds before its evaluation exists.** Batch progress is derived, never stored.
- **One extra container per graded attempt,** never alive at the same time as the agent's container, so peak resource use doesn't change.
- **Crash gap (A1):** if a worker dies between the `finish_attempt` commit and tx A, the attempt has no evaluation row and its in-memory snapshot is lost. If it dies after tx A or tx B, the row stays PENDING or RUNNING. Both cases can be found by query; **nothing recovers them yet (Phase 5).** Such a batch never reports `complete`.
- **Shutdown during evaluation** marks it ERROR ("interrupted: worker_shutdown") on a best-effort basis. There's no cooperative cancellation.
- **Evaluation containers use the task's image *tag*.** The agent's resolved image ID is recorded (A5), but nothing verifies that grading ran on the same image.
- **A `python_tests` check fails closed** if the task's image lacks pytest. The cause is the environment, but it's recorded as `tests_failed`.
- **`normalized_paths` is persisted only in the evaluation's `integrity`,** not with the artifact.

## Deferred

- rescoring through the API (D8, R1)
- re-running checks from stored snapshots
- attempt retries, leases and heartbeats, recovery of stuck evaluations, cancellation and dead-letter handling (Phase 5)
- authentication, authorization and rate limiting (Phase 8)
- LLM-as-judge (D5)
- metrics and tracing (Phase 6)
- performance and throughput benchmarks (Phase 7; not the benchmark *evaluations* built here)
- the web UI (Phase 8)

## Deviations found while implementing

1. **Module layout:** the plan had an `agenteval_worker/evaluation/` package and `SandboxSession.open_for_evaluation`. As built: `evaluation_runner.py`, and an `EvaluationSession` subclass with its own `open()`.
2. **Check kinds:** the plan had a single `command` kind with output parsers. The spike (C2–C7) led to `python_tests`, `io`, `command` (trusted environment only), `final_answer` and `file`, with `protected_paths` as a spec field.
3. **Tar warnings are fatal to an export.** GNU tar skips unix sockets with only a warning and exit 0, which C1 forbids.
4. **`image_id` moved from `EvaluationSession` up to `SandboxSession`,** so the agent's session can record it (A5).
5. **`TaskCreate` and `TaskRead` live in `schemas/task_api.py`,** because `evaluation/spec.py` imports the path helpers in `schemas/task.py` (an import cycle otherwise). Step 3 code is unchanged.
6. **An invalid batch scoring config** is also recorded as ERROR / `evaluation_error` (the same rule as A2).
