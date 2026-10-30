# ADR-009: Reliability and recovery (attempts, leases, fencing, reconciler, cancellation, dead letters)

**Status:** Accepted (Phase 5, implemented in Steps 1 to 10)

Related: ADR-001 (Redis Streams deliver ids only), ADR-002 (Postgres is the source of truth), ADR-005 (agent runtime), ADR-006 (evaluation), [ADR-008](ADR-008-artifact-storage-and-trajectory-fence.md) (artifact storage, the lease keeper and the trajectory fence, validated under load before any of this was built).

## Context

Before Phase 5 a run had one attempt. A worker crash left the run `RUNNING` forever, a lost stream message left it `QUEUED` forever, a transient model failure ended the run, and nobody could cancel one. Evaluations had the same gaps: a crash during grading left a `PENDING` or `RUNNING` row and the batch never completed.

Phase 5 had to make the system survive the common failures without a second source of truth and without new infrastructure. The requirements, each of which is tested:

- A worker that dies (SIGKILL, host loss, OOM) must not lose its run. Another worker takes over after a bounded time.
- A worker that is alive but slow, partitioned or paused must not be able to overwrite the result of whoever took over (a "zombie").
- Duplicate delivery and duplicate execution attempts must be harmless.
- Transient failures retry with backoff. Permanent failures stop, and a stopped run can be inspected and replayed.
- Cancellation must be deterministic against completion, and cancelling must not wait on slow work.
- A failed grading must never lose the attempt or its trajectory, and must itself be recoverable.

## Decision

Everything below keeps Postgres as the only authority. Redis carries ids, and every state change is one guarded `UPDATE` (or one server-side function call), so a stale or repeated message, worker or reconciler can only be refused.

### 1. Three state machines

The machines are data in `agenteval_core/reliability/transitions.py`; the tests check the repositories against those tables. A transition code names one guarded write.

**Run (`runs.status`)**

| Code | Transition | Who | Meaning |
|---|---|---|---|
| R1 | QUEUED → CLAIMED | worker | conditional claim; `lease_epoch` + 1, lease set |
| R2 | QUEUED → CANCELLED | API | cancel before any worker took it |
| R3 | CLAIMED → RUNNING | worker | start: `attempt_count` + 1, attempt row created (A1) |
| R4a | CLAIMED → QUEUED | reconciler | claim lease expired before an attempt started, under the limit (`unstarted_claims` + 1) |
| R4b | CLAIMED → FAILED | reconciler | same, at the limit: `claim_abandoned`, dead-lettered |
| R5 | CLAIMED → CANCELLED | worker, reconciler | cancel flag seen before the attempt started |
| R6 | CLAIMED → FAILED | worker | run or task inconsistent at claim time (`internal_inconsistent`) |
| R7 | RUNNING → COMPLETED | worker | finish with a completed outcome |
| R8 | RUNNING → RETRYING | worker | retryable failure, budget left |
| R9 | RUNNING → RETRYING | reconciler | lease expired, budget left |
| R10 | RUNNING → FAILED | worker, reconciler | non-retryable, or budget exhausted: dead-lettered |
| R11 | RUNNING → CANCELLED | worker, reconciler | cancel flag set |
| R12 | RETRYING → QUEUED | reconciler | `next_attempt_at` reached |
| R13 | RETRYING → CANCELLED | API | cancel while waiting to retry |
| R14 | FAILED → QUEUED | API | replay |

**Attempt (`run_attempts.status`):** A1 creates it `RUNNING` (inside R3). A2 `RUNNING → FINISHED` (inside R7, R8, R10). A3 `RUNNING → ABANDONED` (by the reconciler, inside R9, R10, R11). A4 `RUNNING → CANCELLED` (by the owner, inside R11).

**Evaluation (`attempt_evaluations.status`):**

| Code | Transition | Meaning |
|---|---|---|
| E1, E2, E3 | none → PENDING, SKIPPED, ERROR | recorded for a `FINISHED` attempt, by the fast path or by the reconciler (A1 recovery) |
| E4 | PENDING → RUNNING | claim: `lease_epoch` + 1, `eval_attempts` + 1, lease set |
| E5 | RUNNING → SUCCEEDED | graded; clears `error_reason` |
| E6 | RUNNING → ERROR | non-retryable error, or retries exhausted |
| E7 | RUNNING → PENDING | transient failure, budget left; `next_attempt_at` set |
| E8a, E8b | RUNNING → PENDING, ERROR | released at worker shutdown, budget left or exhausted |
| E9a, E9b | RUNNING → PENDING, ERROR | lease expired, budget left or exhausted (reconciler) |

Invariants (CHECK constraints, not conventions): `RETRYING` has no outcome and no error message; a lease exists exactly in `CLAIMED`/`RUNNING` (runs) and `RUNNING` (evaluations); a cancel flag exists only in `CLAIMED`, `RUNNING` or `CANCELLED`; `CANCELLED` requires the flag; only a `FINISHED` attempt with a completed outcome can have a workspace capture; `max_attempts` is 1 to 10; no `PENDING` evaluation is ever out of budget.

### 2. Failure taxonomy, retry policy and backoff

Every failure is a reason from a closed enum (`reliability/taxonomy.py`). Retryability comes from one policy table keyed by the reason, never from a free-text message. The run-level reasons (`internal_inconsistent`, `claim_abandoned`) and the attempt-level reasons have separate CHECK lists generated from the same enum, so the schema and the code cannot drift.

| Category | Reasons | Retryable |
|---|---|---|
| model, transient | `model_rate_limited`, `model_overloaded`, `model_server_error`, `model_timeout`, `model_connection`, `model_conflict` | yes |
| model, permanent | `model_bad_request`, `model_authentication`, `model_permission`, `model_not_found`, `model_request_too_large`, `model_protocol` | no |
| configuration | `config_provider_disabled`, `config_model_unpriced`, `config_invalid`, `evaluation_budget_exhausted` (migration 0011: the run's evaluation had spent its `max_cost_usd`) | no |
| sandbox | `sandbox_create_failed`, `sandbox_exec_failed`, `sandbox_reaper_failed`, `sandbox_lost` | yes |
| sandbox | `sandbox_image_not_found` | no |
| worker, internal | `worker_capability`, `worker_backstop`, `internal_unexpected`, `persistence_trajectory`, `worker_shutdown` | yes |
| lease | `lease_expired` (A3 only) | yes |
| cancellation | `cancelled_by_user`, `cancelled_by_batch` (A4 only) | no (cancellation is not a failure) |
| run level | `internal_inconsistent`, `claim_abandoned` | no |
| legacy | `unclassified` (backfill of pre-Phase-5 rows) | no |

**Outcomes.** A `COMPLETED` outcome (the agent finished, hit a limit, was refused, timed out) is never retried: the task was fairly tested. Only `FAILED` outcomes (`MODEL_ERROR`, `SANDBOX_ERROR`, `INTERNAL_ERROR`) are candidates, and each reason may only be stored with the outcomes the table allows.

**Decision after a failed attempt** (`policy.after_failed_attempt`): cancel requested → R11; retryable and `attempt_count < max_attempts` → RETRYING (R8, or R9 when the reconciler abandoned it); otherwise FAILED (R10) with a `dead_letter_reason` of `retries_exhausted` (a retryable reason ran out of budget) or `non_retryable`.

**Two retry layers.** Model calls are retried inside a step by the runtime (3 retries, 2 s base, 30 s cap, 1 s jitter, `retry-after` honoured, never past the deadline, every try recorded as a `model_error` event). If those run out, the attempt fails with the mapped reason and the run-level policy above decides about a new attempt.

**Backoff between attempts** (`reliability/backoff.py`): `min(2 s * 2^(n-1), 60 s) * U(0.8, 1.2)`, where `n` is the number of the attempt that just failed. The jitter source is injected so tests are deterministic. R8 and R9 store it as `next_attempt_at`; R12 acts when it is due.

**Budgets.** `runs.max_attempts` (1 to 10, API default 3) is pinned on the run, and on every run of a batch (`run_max_attempts`). Shutdown and lease expiry consume an attempt. Expiry of a claim before an attempt started does not: `unstarted_claims` has a fixed maximum of 3 (`MAX_UNSTARTED_CLAIMS`, deliberately not a setting) and then R4b.

### 3. Leases, fencing epochs, the lease keeper and self-stop

- **Lease.** A claim sets `lease_expires_at = now() + ttl` and bumps `runs.lease_epoch`, the fencing token. The attempt row records the epoch it was created under. Evaluations have the same columns.
- **Fencing.** Every write by an owner is guarded by the row's status and epoch: heartbeat, trajectory append, artifact save, finish, cancel. Recovery bumps the epoch, so a stale owner's statement matches zero rows. A statement that was waiting on a lock re-checks the latest row version once it gets it. Correctness never depends on clocks or timeouts.
- **Lock order.** Terminal owner transactions take the attempt row first, then the `runs` row (ADR-008). Heartbeat and the cancel API lock only `runs`. Appends and artifact saves lock only the attempt row. Recovery uses `SKIP LOCKED` and never waits, so no wait cycle exists. (The validation runs found one cycle in an earlier version: a terminal transaction held the attempt row `FOR UPDATE` and waited for `runs`, while a trajectory append held `runs` `FOR SHARE` and its foreign-key check waited for the attempt row. Terminal transactions now take `FOR NO KEY UPDATE`, which a foreign-key `KEY SHARE` does not conflict with, and appends fence on the attempt row instead of `runs` (ADR-008 B).)
- **Finish and cancel are single server-side calls** (`p5_finish`, `p5_cancel`), so every lock is taken and released inside one statement. A finish that finds the cancel flag returns `restart_cancel` without changing anything and the worker then calls cancel.
- **The lease keeper** (ADR-008 D) renews every lease the worker holds from a separate process with its own connection, so sandbox streaming or file I/O in the worker cannot delay a heartbeat. It renews only while the worker keeps pinging (`worker_liveness_timeout_s`), so a worker that is alive but stuck loses its leases. Each renewal reports whether a cancel was requested.
- **Self-stop.** The worker computes `fence_at = t_sent + ttl - margin` from the send time of the last successful renewal (seeded from the claim). Only a successful renewal moves it forward. At `fence_at`, or on a `fenced` report, the worker cancels the attempt with reason `lease_lost`, kills the sandbox and writes nothing more. This is what stops a zombie before the reconciler or a second worker even matters; the epoch fence is the backstop.
- **Startup validation** (`LeaseTiming.validate()`): heartbeat < ttl - margin, margin >= 2 x reconcile interval, liveness timeout < ttl - margin. The worker refuses to start otherwise. The artifact timings are checked the same way (sweep grace > save deadline + statement timeout).

### 4. The reconciler

It runs as an asyncio task in **every** worker, ticking every `reconcile_interval_s`. Each Postgres step is its own short transaction that first takes `pg_try_advisory_xact_lock(RECONCILER_LOCK_NS, step)`; if another worker holds it, the step is skipped this tick. Side effects (XADD, container kills, XACK, `XGROUP DELCONSUMER`) happen only after the commit. An exception in one step is logged and never stops the other steps or the worker. A late renewal that races a recovery is fenced by the epoch, not by luck.

| Step (advisory key) | What it does |
|---|---|
| 1a (11) | `CLAIMED` runs with an expired lease: R5 if flagged, else R4a (back to `QUEUED`, republished) or R4b. `SKIP LOCKED`, 50 per tick |
| 1b (12) | `RUNNING` runs with an expired lease: attempt to `ABANDONED` (A3, `lease_expired`), then R9, R10 or R11 by policy; epoch + 1. After commit, kill containers labelled with the attempt. Never touches artifacts |
| 2 (2) | `RUNNING` evaluations with an expired lease: E9a or E9b; kill their containers. |
| A1 (21) | `FINISHED` agent attempts with no evaluation row after `a1_grace_s`: record one (E1, E2 or E3), with the same decision code as the fast path |
| 3 (3) | `RETRYING` runs whose `next_attempt_at` passed: R12, republished |
| 4 (4) | `QUEUED` runs never published or last published more than `redispatch_after_s` ago: publish and stamp `dispatched_at`. Then `XAUTOCLAIM` pending stream entries idle for as long and acknowledge them (Postgres, not the entry, is the truth) |
| 6 (6) | `PENDING` evaluations that are due and not recently published: publish on the evaluation stream and stamp |
| 7 (7) | workers silent for 3 x ttl are marked stopped with `stop_reason = 'lost'`; consumers of stopped workers with nothing pending are removed with `XGROUP DELCONSUMER`; every XADD trims with `MAXLEN ~ stream_maxlen` |
| 5 (5) | every `orphan_sweep_interval_s`: delete unreferenced artifact files older than the grace period (ADR-008) |
| 8 | every `orphan_sweep_interval_s`: remove orphaned sandbox containers by their deadline label (safe with several workers on one daemon) |

Duplicate stream messages are harmless everywhere: a message only names an id, and the claim (R1 or E4) is a guarded update that decides.

### 5. Cancellation (G7)

`POST /runs/{id}/cancel` and `POST /evaluations/{id}/cancel` are single short writes against `runs` and never wait on artifact work.

- `QUEUED` and `RETRYING` runs are cancelled at once (R2, R13).
- `CLAIMED` and `RUNNING` runs only get the flag (`cancel_requested_at`, `cancel_reason` of `user` or `batch`). The owner sees it at R3 (R5), on its next lease beat, or at finish (`restart_cancel`, then A4 and R11). If the owner is gone, the reconciler applies R5 or R11 when the lease expires.
- **Cancel versus completion is deterministic.** Finish guards require `cancel_requested_at IS NULL`. If the cancel committed first, cancellation wins and the finishing worker's attempt becomes `CANCELLED`. If completion committed first, the cancel API returns 409 and writes nothing. Every guard is in SQL, so the race has exactly one winner.
- A cancelled attempt has no outcome, no artifact row, no workspace capture and no evaluation, and cancellation never changes the metrics or scores of other attempts. A repeat cancel is a 200 no-op.
- **Latency** is bounded by one heartbeat interval plus the runtime's next check (an agent run stops between steps and a blocked tool call is aborted; for a command task the worker kills the container by label). A batch cancel sets the batch flag and then cancels each unfinished run in chunks of 100; a repeat finishes anything a crashed call left.

### 6. Dead letters and replay

- **A dead-lettered run is `FAILED` with `dead_letter_reason` set** (`retries_exhausted` or `non_retryable`). There is no `DEAD_LETTER` status, so `status_for()`, the state machine and every client keep the same set of statuses. `GET /runs?dead_letter=true` is the queue view (a partial index on failed runs serves it).
- **Replay** (R14) is `POST /runs/{id}/replay` with `additional_attempts` of 1 to 9 (default 1; the total stays at most 10). One transaction locks the run, checks the idempotency key against `run_replays`, applies a guarded `FAILED → QUEUED`, raises `max_attempts`, clears the outcome, message and dead-letter fields and resets `unstarted_claims`; the id is published after the commit. Attempt numbers continue and every earlier attempt, trajectory and evaluation is kept. A repeat with the same key is a 200 no-op; the same key for another run or a different body is 409.

### 7. Artifacts

Workspace snapshots are files on a volume with a metadata row in Postgres, and the row is authoritative (ADR-008). The reliability consequences: capture happens before the attempt finishes and records `workspace_capture` explicitly; a failed save never changes the attempt's outcome; grading loads the snapshot by row, so **any** worker can grade **any** attempt, which is what makes evaluation recovery possible; a missing or corrupt file is a platform error, never a wrong grade.

### 8. Evaluation reliability

Grading is split into `record` (creates the evaluation row; E1 to E3) and `grade` (claim E4, grade, then E5, E6 or E7). The agent attempt is already durable before either runs, so **a grading failure can never lose the original trajectory**.

- The budget `max_eval_attempts` (1 to 10, default 3) is pinned on the row (from the batch's `eval_max_attempts`, or `AGENTEVAL_EVAL_MAX_ATTEMPTS`).
- A grading error maps to a closed `EvaluationErrorReason`. Retryable: `sandbox_failed`, `budget_timeout`, `internal_unexpected`, `worker_shutdown`, `lease_expired`. Not retryable (grading again cannot change the result): invalid spec or scoring config, every `capture_*`, `artifact_missing`, `artifact_corrupt`, `check_error`. A retryable error with budget left goes to `PENDING` (E7) with backoff; otherwise `ERROR` (E6) with `error_kind` `retries_exhausted` or `non_retryable`.
- Shutdown during grading releases the row (E8a or E8b) and consumes the grading attempt. A crash is recovered by step 2 (E9a or E9b). Every move to `PENDING` requires budget left, so a `PENDING` row is never out of budget.
- Evaluations are published on their own stream (`evaluations.dispatch`, same consumer group name) and consumed under the same concurrency semaphore as runs.

### Failure semantics

What happens at each failure point, and who recovers it.

| Failure | Immediate effect | Recovered by | Result |
|---|---|---|---|
| API commits the run, `XADD` fails | run stays `QUEUED`, API returns 503 | client retry with the same key, or reconciler step 4 | run is published again |
| Stream message lost, trimmed or never read | run stays `QUEUED` | step 4 (redispatch after `redispatch_after_s`) | runs once |
| Message delivered twice, or to two workers | second claim matches zero rows | the claim itself | exactly one attempt |
| Worker dies between reading a message and acking it | entry stays pending | step 4 `XAUTOCLAIM` (acked; the run is covered by redispatch or by its lease) | no lost or double run |
| Worker dies after R1, before R3 | run stays `CLAIMED`, lease expires | step 1a: R4a or R4b | requeued, or dead-lettered after 3 unstarted claims |
| Worker dies mid-run (SIGKILL, host loss) | keeper dies with it, heartbeats stop | step 1b: A3, then R9 or R10 | retried on another worker, or dead-lettered; sandbox killed by label |
| Worker alive but stuck or partitioned (zombie) | keeper stops renewing; the worker self-stops at `fence_at`; any late write is fenced by the epoch | the same recovery as a dead worker | the stale owner changes nothing |
| Keeper process dies | worker keeps its existing `fence_at` and restarts the keeper | worker, or self-stop at `fence_at` | lease kept or safely lost |
| Model API transient failure | in-step retries with backoff | runtime, then R8 | new attempt after backoff |
| Model API permanent failure | attempt fails `MODEL_ERROR` with a non-retryable reason | R10 | dead-lettered, replayable |
| Tool call hangs or times out | tool bounded by its timeout and the host backstop; sandbox aborted | runtime | attempt ends `TIMEOUT` or `SANDBOX_ERROR` as defined by the outcome table |
| Sandbox create or exec failure | `SANDBOX_ERROR` with a reason | R8 if the reason is retryable, else R10 | retried or dead-lettered |
| Worker shutdown (SIGTERM) | drain; in-flight agent runs are cancelled with `worker_shutdown` shortly before the drain deadline | R8 (consumes an attempt); a run hard-cancelled before it can record is recovered by lease expiry | retried |
| Postgres unavailable when finishing | `p5_finish` retried with backoff (0.2, 0.5, 1, 2 s) for transient errors; otherwise the result is logged and the run stays `RUNNING` | lease expiry then step 1b | retried; the lost attempt is `ABANDONED` |
| Postgres down while a worker holds a lease | renewals fail, `fence_at` is not extended, worker self-stops | step 1b once the database is back | retried |
| Snapshot save fails, times out, or loses a race | no row, file removed; `workspace_capture = 'persist_failed'`; the run's outcome is unchanged | none needed; evaluation becomes `ERROR capture_persist_failed` (not retried) | explicit and visible |
| Crash between artifact rename and row, or after cancel commit | unreferenced file | step 5 sweeper after the grace period | file removed |
| Crash between attempt finish and evaluation record | `FINISHED` attempt without an evaluation | step 2 (A1) after `a1_grace_s` | evaluation recorded and graded |
| Crash or shutdown during grading | evaluation `RUNNING` with a lease, container left | E8a or E8b (shutdown), step 2 E9a or E9b (crash) | graded by another worker, or `ERROR retries_exhausted` |
| Grading error | trajectory and attempt untouched | E7 (retryable, budget left) or E6 | `PENDING` again with backoff, or `ERROR` with a structured reason |
| Cancel races completion | one guarded statement wins | the SQL guards | 202 and `CANCELLED`, or 409 and the result stands |
| Reconciler crashes or two reconcilers overlap | the step is skipped or retried next tick | advisory locks, guarded updates | no double application |
| Redis down | claims and dispatch stall | Postgres state is intact; step 4 republishes after Redis returns | no state lost |

### Settings

All are environment variables with the prefix `AGENTEVAL_` (see `.env.example`). Startup validates the lease and artifact combinations.

| Setting | Default | Meaning |
|---|---|---|
| `LEASE_TTL_S` | 30 | lease length |
| `HEARTBEAT_S` | 10 | keeper renewal interval |
| `SELF_FENCE_MARGIN_S` | 10 | the worker stops this long before the lease can expire |
| `RECONCILE_INTERVAL_S` | 5 | reconciler tick |
| `WORKER_LIVENESS_TIMEOUT_S` | 15 | the keeper stops renewing if the worker has not pinged within this time |
| `REDISPATCH_AFTER_S` | 60 | republish a `QUEUED` run, or reclaim a pending entry, idle this long |
| `ORPHAN_SWEEP_INTERVAL_S` | 300 | orphan container and artifact sweeps |
| `STREAM_MAXLEN` | 100000 | approximate stream trim length |
| `RUN_MAX_ATTEMPTS` | 3 | default run budget (1 to 10) |
| `EVAL_MAX_ATTEMPTS` | 3 | default grading budget (1 to 10) |
| `A1_GRACE_S` | 60 | how long a `FINISHED` attempt may lack an evaluation row |
| `EVALUATION_STREAM` | `evaluations.dispatch` | evaluation dispatch stream |
| `ARTIFACT_DIR` | `/var/lib/agenteval/artifacts` | snapshot files (a named volume in compose) |
| `ARTIFACT_INSERT_TIMEOUT_S` | 10 | metadata statement timeout |
| `ARTIFACT_SAVE_DEADLINE_S` | 15 | rename to metadata statement |
| `ARTIFACT_SWEEP_GRACE_S` | 120 | minimum age of an orphan file before deletion |
| `WORKER_DRAIN_TIMEOUT_S` | 330 | total drain time at shutdown |
| `WORKER_CANCEL_GRACE_S` | 20 | in-flight agent runs are cancelled this long before the drain deadline |

Fixed in code, not settings: `MAX_UNSTARTED_CLAIMS` 3, the backoff curve (base 2 s, cap 60 s, jitter 0.8 to 1.2), the in-step model retry policy (3 retries, 2 s base, 30 s cap), `MAX_ATTEMPTS_CAP` 10, replay `additional_attempts` 1 to 9.

The 30 s, 10 s and 10 s lease values come from the approved design and the Step 0 measurements (ADR-008: the worst measured event-loop stall was 5.3 s). They were not tuned to hide contention. `docker-compose.e2e.yml` overrides them (15, 2, 5, 1, 8) only so the end-to-end suite does not wait minutes for recovery; the values pass `LeaseTiming.validate()`.

## Consequences

- **Benefits:**
  - Every crash and failure in the table above has a named owner and a tested outcome.
  - A stale or duplicate actor is refused by a guarded statement, not by timing.
  - Any worker can recover or grade anything: there is no per-worker state that matters, so a crashed worker is replaced by restarting it.
  - A failed run keeps its history and can be replayed.
- **Costs and limits:**
  - **At-least-once execution, not exactly-once.** A recovered attempt re-runs the agent from the start. Model calls cost money again and tool side effects inside the sandbox repeat; the sandbox is discarded, so nothing leaks between attempts.
  - **Recovery latency is about one ttl** (30 s by default) plus the backoff. Shorter leases recover faster but make a healthy worker likelier to lose its lease under load.
  - **Cancel latency is up to one heartbeat** (10 s) for a running run.
  - **The reconciler runs in every worker.** There is no separate scheduler to deploy, but the cluster needs at least one live worker to recover anything.
  - **Single host.** Container kills by label, the artifact volume and the sandbox sweeps assume one Docker daemon. Multi-host needs an object store behind `ArtifactStore` and per-host sweeps.
  - **The ttl-bound self-stop depends on the monotonic clock** of the worker host. Fencing does not.
  - **No metrics or tracing yet** (Phase 6); recovery is visible only through logs, the `workers` table and the run and attempt rows.
  - **Not hardened:** this is a research sandbox, not a multi-tenant security boundary (see the README).

## Alternatives considered

| Option | Why not |
|---|---|
| **Celery (or RQ, Dramatiq)** for dispatch and retries | Hides exactly the worker lifecycle, retry, ack and recovery semantics the project exists to show. Its retry state would also live outside Postgres, and fencing a stale worker against Postgres rows would still have to be built |
| **Redis-only state** (job hashes, Redis locks and TTLs as leases) | Redis is not durable by default and is not transactional with the result write. A lease in Redis cannot fence a write to Postgres. Postgres stays the single source of truth (ADR-002) |
| **A separate `DEAD_LETTER` run status** | Another status to map through `status_for()`, every CHECK, the summaries and every client, for no extra information. `FAILED` plus `dead_letter_reason` is one nullable column and one partial index |
| **A dead-letter stream or table** | A second place to keep consistent with the run. The run row is the record, and the listing is a query |
| **Postgres `FOR UPDATE SKIP LOCKED` as the queue** | Would remove the dual write, and is still the documented fallback from ADR-001. Redis Streams stayed because the reconciler already covers the dual-write gap |
| **Heartbeats from the worker's own event loop** | Measured to stall for seconds under load (ADR-008 D); the keeper process keeps renewals independent |
| **A dedicated reconciler service** | One more deployable and a single point of failure. A reconciler in every worker, serialised by advisory locks, needs nothing else |
| **Exactly-once execution** | Not achievable across a crash for side-effecting agent runs; guarded writes make the result exactly-once instead |
| **Retrying completed outcomes** (for example `MAX_STEPS`) | Those results are about the agent, not the platform. Retrying would hide the signal the benchmark measures |

## Validation

The design was validated in a scratch environment before it was built (ADR-008, "Validation"), then implemented and tested at three levels. Paths are relative to the repository root.

- **Unit tests** for the pure parts: `packages/core/tests/test_reliability_transitions.py`, `test_reliability_policy.py`, `test_reliability_taxonomy.py`, `test_reliability_fencing.py`, `test_reliability_keeper.py`, `test_retry.py`, `test_fake_provider.py` (the per-attempt error filter).
- **Integration tests** against real Postgres, Redis and Docker (`tests/integration/`):
  - transitions and constraints: `test_phase5_constraints.py`, `test_migration_0004.py` to `test_migration_0009.py`, `test_terminal_functions.py`
  - leases and fencing: `test_leases.py`, `test_run_repository_leases.py`, `test_trajectory_fence.py`
  - reconciler: `test_reconciler.py`, and `test_reconciler_crash.py` (a worker killed mid-run is recovered and retried by another worker)
  - artifacts: `test_artifact_save.py`, `test_artifact_capture.py`, `test_artifact_sweeper.py`
  - evaluation recovery: `test_evaluation_reliability.py`
  - API: `test_cancel_api.py`, `test_replay_api.py`, `test_run_budgets_api.py`, `test_runs_list_api.py`, `test_f13_summaries.py`
- **End-to-end tests** on the running compose stack: `tests/e2e/test_reliability_e2e.py`, six scenarios with real SIGKILLs under the short timings of `docker-compose.e2e.yml`:

| Scenario | What it shows |
|---|---|
| P1 | SIGKILL the worker during a run: the attempt is `ABANDONED` (`lease_expired`), the run completes as attempt 2 on the restarted worker, the dead worker's sandbox is gone |
| P2 | a transient model failure outlasts the in-step retries: attempt 1 ends `MODEL_ERROR`, the run goes `RETRYING`, the backoff is respected, attempt 2 completes |
| P3 | cancel a running run (`CANCELLED`, attempt `CANCELLED`, no evaluation, no artifact) and a batch (every run terminal, batch complete) |
| P4 | a non-retryable failure is `FAILED` with `dead_letter_reason`, listed by `GET /runs?dead_letter=true`, replayed with `additional_attempts`, then completes with its history intact |
| P5 | the same run id published seven times (some after completion): exactly one attempt |
| P6 | SIGKILL during grading: the evaluation is recovered (E9a) and graded by the restarted worker; the agent attempt is not re-run |

The model failures in P2 and P4 are scripted: the fake provider's `error` step takes an optional `attempts` list, so one agent version fails attempt 1 and succeeds on attempt 2.

What has **not** been measured: recovery behaviour under sustained load, multi-worker scaling, and throughput. Those belong to Phase 7, and no performance numbers are claimed here.
