# ADR-008: Artifact bytes on a volume, metadata in Postgres; trajectory writes fenced on the attempt row

**Status:** Accepted (Phase 5). Validated in a scratch environment first, then implemented in Phase 5 Steps 2 to 7 (see [Implementation](#implementation)). The validation history below is kept as it was written, so some sentences are phrased as proposals.

Related: ADR-002 (Postgres is the source of truth), ADR-006 (evaluation; this ADR replaces its D3 storage choice), [ADR-009](ADR-009-reliability-and-recovery.md) (the reliability design that uses it).

## Context

Phase 5 adds leases, recovery and cancellation. Each owner proves it still holds a run with a fencing token (`runs.lease_epoch`) and renews its lease with a short heartbeat `UPDATE runs`. Users cancel through a short `UPDATE runs` too. Neither may be delayed long enough to expire a healthy lease or make cancellation feel stuck. The thresholds we validate against are:

- heartbeat lateness plus round-trip time under 5 s;
- a cancellation request waiting under 1 s;
- no transaction holding the `runs` row for more than 1 s.

Workspace snapshots can be 16 MiB (ADR-006 D3). Before any production code, we measured the proposed transaction protocol under load in a scratch environment. The load was two concurrent 16 MiB writes and deletes, Docker exec and container churn, 17 MiB exec streams, Redis stream traffic, and trajectory appends. Two problems remained after the artifact INSERT and DELETE had been moved off the `runs` lock:

1. **Write-ahead log (WAL) pressure.** With snapshots in `bytea`, every 16 MiB snapshot is also 16 MiB of WAL. In a 7-minute run that came to 642 MiB, and the WAL buffers filled 181 times. Every small commit then waited behind it, heartbeats and cancellation included. The slowest cancellation request (2.3 s) was not waiting on any lock.
2. **Trajectory appends blocked heartbeats.** The trajectory fence took `FOR SHARE` on the `runs` row. Heartbeats then queued behind appends, and the appends were themselves slow to commit.

Measured results, all on Postgres 16.11 under the same load:

| Variant | Heartbeat max | Cancel wait max | `runs` lock held max | WAL |
|---|---|---|---|---|
| `bytea`, runs fence | 5.2 s | 4.2 s | 1.19 s | 405 MiB / 437 s |
| `bytea`, attempt-row fence (B) | 1.6 s | **2.3 s** | 0.42 s | 642 MiB / 431 s |
| files + metadata row, attempt-row fence (A+B) | 1.5 s | 0.84 s | 0.67 s | **3 MiB** / 424 s |

The A+B run also completed far more cycles: 884 saves of 16 MiB, against 36 with `bytea`.

That measurement wrote files to the macOS host disk. Repeating it with the worker inside the Docker VM — files on the VM's disk with Linux `fsync` — exposed a third problem:

3. **A starved worker held locks across its own pauses.** The VM's load average reached 20 on 8 vCPUs. The worker's event loop stalled for up to 5 s, and Postgres sat waiting for the worker's next statement while the finish/cancel transaction held the `runs` row. Heartbeats running in that same process stalled with it.

This led to decisions C–E below. The final validation results are at the end of this ADR.

## Decision

### A. Snapshot bytes go to an artifact volume; Postgres keeps the metadata row

- **One storage interface:** `ArtifactStore` with `put`, `open` and `delete`. The v1 implementation is a named Docker volume mounted into every worker container (single-host compose). An object store such as MinIO or S3 can implement the same interface later, without changing the transaction protocol.
- **The key includes the content hash:** `<attempt_id>/<kind>-<sha256>.tar.gz`. A retry can never overwrite different content in place.
- **Write protocol:** write to `<key>.tmp`, `fsync` the file, rename it into place, then `fsync` the directory. All of this happens before any database statement and with no database lock held.
- **The metadata row is authoritative.** `attempt_artifacts` keeps `(attempt_id, kind)` uniqueness, `size_bytes` and `sha256`, and gains `storage_key`. A file without a row does not exist as far as the system is concerned. A row whose file is missing or has a different hash is a platform error and is never graded.
- **Phase 4 rows stay readable.** Existing rows keep their `content`. New rows set `storage_key` instead. A CHECK constraint requires exactly one of the two.

### B. Trajectory appends fence on the attempt row

A trajectory append locks `run_attempts` `FOR SHARE` (`id`, `status = 'RUNNING'`, `lease_epoch`) and never locks `runs`. Invariant I1 makes this equivalent to the old check: while an attempt is `RUNNING` at epoch *e*, its run is `RUNNING` at epoch *e*. The foreign-key check of the INSERT takes `KEY SHARE` on that same row, which the `FOR SHARE` lock already covers.

### The transactions

| Step | Statements | Locks held |
|---|---|---|
| **Save artifact** | write file (above); then one autocommit statement: `INSERT … SELECT … FROM (SELECT id FROM run_attempts WHERE id=$1 AND status='RUNNING' AND lease_epoch=$2 FOR SHARE) a WHERE NOT EXISTS (cancel flag set) ON CONFLICT (attempt_id, kind) DO NOTHING`. If 0 rows: an existing row with the same SHA-256 counts as success; otherwise the save is fenced and the file is removed. | the attempt row, only while the short statement runs |
| **Trajectory append** | attempt row `FOR SHARE`, then INSERT | the attempt row |
| **Heartbeat, cancel API** | one `UPDATE runs` guarded by epoch and status | `runs` only |
| **Finish (FINISH mode)** | attempt row `FOR NO KEY UPDATE` → `runs` `FOR UPDATE` → if the cancel flag is set, roll back and restart in CANCEL mode; otherwise finish | attempt, then `runs`, briefly |
| **Cancel (CANCEL mode)** | attempt row `FOR NO KEY UPDATE` → delete the metadata row → `runs` `FOR UPDATE` → `CANCELLED`; **after commit**, unlink the file | attempt, then `runs`, briefly |
| **Recovery** | one `SELECT … FOR UPDATE OF r, a SKIP LOCKED`; never touches artifacts | skips locked rows |

**Lock order:**
- Finish and cancel lock the attempt row, then the `runs` row.
- Heartbeat and the cancel API lock only `runs`.
- Saving an artifact and appending to the trajectory lock only the attempt row.
- Recovery never waits.

No transaction holds `runs` while waiting for an attempt row, so no wait cycle is possible.

### C. Finish and cancel are one server-side call each

Each T-terminal mode is a single plpgsql function call, and it commits in the same round trip. Every lock is taken and released inside one statement, so no client stall can extend a lock hold. A FINISH that finds the cancel flag returns `restart_cancel` without changing anything, and the worker then calls CANCEL. If the worker dies after the server has received the call, the call still runs to completion atomically. The artifact files are unlinked by the worker after the call returns; if it dies first, the sweeper removes them.

### D. Heartbeats run in their own process

Each worker renews its leases from a separate process, the **lease keeper**, which has its own database connection. Sandbox streaming and file I/O in the worker process cannot delay lease renewal. The worker still decides when it must stop; the keeper only renews and reports.

**Process.**
- The worker starts one keeper per worker process at startup (`spawn` start method, so no event-loop state is inherited).
- The keeper has its own asyncpg connection, `application_name = agenteval-keeper-<worker_id>`.
- The two talk over one duplex pipe.

**Messages, worker → keeper.**
- `hold(kind, id, epoch)`: start renewing a lease. `kind` is `run` or `evaluation`; `epoch` is the token the claim returned.
- `release(kind, id, epoch)`: stop renewing it. Sent after the final transition, or after a fence or a self-stop.
- `ping(t)`: sent every second from the worker's event loop, to prove the worker is alive and making progress.
- `stop`: shutdown.

**Messages, keeper → worker.**
- `beat(kind, id, epoch, t_sent, result)`. `t_sent` is the keeper's `time.monotonic()` taken just before the renewal was sent (CLOCK_MONOTONIC is system-wide, so both processes share it). `result` is one of:
  - `ok(cancel_requested)`;
  - `fenced`: the guarded update matched 0 rows;
  - `error(kind)`: database or connection failure.
- `alive(t)`, every second.

**Renewal.** Every `heartbeat_s` (10 s), the keeper renews everything it holds in one round trip per table. For example:
`UPDATE runs SET lease_expires_at = now() + ttl, heartbeat_at = now() WHERE (id, lease_epoch) IN (…) AND status IN ('CLAIMED','RUNNING') RETURNING id, cancel_requested_at`
plus the same for `attempt_evaluations`. A held lease missing from `RETURNING` is reported as `fenced`. After an `error`, the keeper retries after 1 s and keeps reconnecting with backoff.

**Self-stop deadline (worker side).**
- For each lease, `fence_at = t_sent + ttl − margin`, using the `t_sent` of the last `ok` beat. At claim time it is seeded from the claim's own send time.
- Only an `ok` beat moves `fence_at` forward. Errors, a silent keeper or a dead keeper never extend it.
- At `fence_at`, the worker cancels with reason `lease_lost`, kills the sandbox and makes no further writes. The runtime guard checks `fence_at` before every model and tool call.
- `fenced` means `lease_lost` immediately.
- `ok(cancel_requested = true)` cancels with the stored `cancel_reason`.

**Worker liveness gate (keeper side).**
- The keeper renews only while the worker's last `ping` is younger than `worker_liveness_timeout_s` (**15 s**: about three times the worst event-loop stall measured, 5.3 s, and below `ttl − margin` = 20 s).
- A worker that is alive but stuck therefore stops being renewed: its leases expire and recovery takes over.
- Once unstuck, the worker sees no new `ok` beats, reaches `fence_at` and stops itself.

**Process death.**
- **Keeper dies** (pipe EOF, or no `alive` for 3 s):
  - the worker keeps working on its existing `fence_at` deadlines;
  - it starts a new keeper and re-sends `hold` for every current lease with its epoch;
  - if the restart fails, every lease simply self-stops at its `fence_at`.
- **Worker dies** (pipe EOF, or `os.getppid()` changes): the keeper exits at once without renewing anything, so the leases expire and recovery runs.
- **Both die:** the leases expire and recovery runs.

**Shutdown.** The worker releases each lease after its final transition and sends `stop`. The keeper is also a daemon process, so it never outlives the worker.

**Setting.** `AGENTEVAL_WORKER_LIVENESS_TIMEOUT_S` (15). Startup refuses a value that is not below `ttl − margin`.

### E. Writer deadline protects against the sweeper

A save inserts its metadata row only within `SAVE_DEADLINE` of renaming the file. The sweeper's grace period is longer than `SAVE_DEADLINE` plus the statement timeout, so it can never delete a file whose row is still about to be inserted.

### Deployment note

On a shared single host, the API, heartbeat process, worker and Postgres need CPU priority over sandbox containers. The validation used Docker CPU weights (4× the default) and `nice -10` for the heartbeat and API processes.

`docker-compose.yml` applies the weights: `cpu_shares: 4096` (4× Docker's default of 1024) on `postgres`, `api` and `worker`. The lease keeper and the reconciler run inside the worker container, so they share its weight. Sandbox containers are created by the worker and keep the default 1024. Weights only decide who wins under CPU contention; they reserve nothing on an idle host. The `nice -10` part of the validation setup is not reproduced in compose.

### Invariants kept

- A `CANCELLED` attempt has no artifact row, no workspace capture and no evaluation.
- A stale owner cannot save, append, heartbeat or finish. Its attempt row no longer matches (`status`, `lease_epoch`), and a statement that was waiting re-checks the latest row version once the lock is released.
- Artifacts on `ABANDONED` attempts may remain; they are never graded.

## Timeouts and grace period (values adopted)

| Setting | Proposed | Bounds |
|---|---|---|
| Artifact statement timeout (`AGENTEVAL_ARTIFACT_INSERT_TIMEOUT_S`) | **10 s** | the metadata `INSERT`, including any wait for the attempt-row lock |
| `SAVE_DEADLINE` | **15 s** | from the file rename to the start of the metadata statement; checked by the writer |
| Sweeper grace period | **120 s** | the minimum age, by file mtime, before an unreferenced file or `.tmp` may be deleted |

**How the values follow from the measurements.** Three runs under the validation load, 1,162 saves in total:

- **Statement timeout, 10 s.**
  - The metadata statement took p50 47–94 ms, p99 0.4–1.0 s, and **max 2.0 s**.
  - Its only waits are short lock waits: finish/cancel holds the attempt row for at most about 0.1 s inside Postgres (decision C), and trajectory appends are short.
  - 10 s gives five times the worst observed value. Cancellation overshot the timeout by at most 35 ms.
  - 10 s also sits well inside the 30 s lease, so a stuck statement fails and frees its lock long before recovery would act.
- **`SAVE_DEADLINE`, 15 s.**
  - Nothing runs between the rename and the statement except the worker's own scheduling.
  - The worst worker event-loop stall measured in any run was **5.3 s**; 15 s is about three times that.
  - A save that misses the deadline inserts no row and removes its file. As with any failed snapshot save, the attempt records `workspace_capture = 'persist_failed'`; missing the deadline never affects correctness.
- **Sweeper grace, 120 s.**
  - Safety needs grace > `SAVE_DEADLINE` + statement timeout + the timeout overshoot, i.e. about 25 s. At that age, no save can still insert a row pointing at the file.
  - 120 s is about five times that bound. It also covers a `.tmp` whose writer is alive but slow: every write refreshes the mtime, and the slowest write measured was 8.1 s for a whole 16 MiB file.
  - Sweeps keep running on the existing 300 s orphan-sweep interval.
- Whole saves (file plus row) took p50 0.5–1.2 s, with a max of **21 s** in the soak with 8 concurrent owners. The file write is not timed out separately: it holds no database lock, and the task's wall-clock timeout bounds a hung disk.
- These exact values were not run under load in the scratch environment. The deadline and grace mechanism itself is tested deterministically with small values, and startup refuses a grace that is not larger than the deadline plus the statement timeout.

**How the failure cases are handled:**

- **Timeout:** Postgres cancels the statement and rolls back, so no row exists. The writer removes its file. As for every failed snapshot save, the run's outcome is unaffected: the attempt finishes normally with `workspace_capture = 'persist_failed'`, and its evaluation is created as ERROR `capture_persist_failed`, which is not retried.
- **Partial file writes:** bytes only ever go to `<key>.tmp`. The final name appears only after `fsync` and rename, so a reader can never see a partial file. A crash leaves at most a `.tmp`, which the sweeper removes after the grace period. If the sweeper removes a `.tmp` whose writer was stalled for more than 120 s, the writer's rename fails and the save is recorded as `persist_failed`. Every read checks size and SHA-256.
- **Orphan cleanup:** unreferenced files come from three places: a crash between the rename and the row, a fenced save whose own delete failed, and a crash between the CANCEL commit and the unlink. All are swept after the grace period. Rows on `ABANDONED` attempts keep their files; they are never graded, and garbage collection stays out of scope.
- **Cancellation:**
  - Once the flag is visible, the save statement inserts nothing.
  - A save already holding the attempt row makes CANCEL wait for it, then CANCEL deletes the row.
  - The CANCEL call deletes the rows and changes the state in one round trip; the files are unlinked afterwards.
  - A `CANCELLED` attempt never has a row.
- **Stale workers:**
  - Every write is guarded by the attempt's `status` and `lease_epoch`. After recovery, a stale save inserts 0 rows and removes its own file, and a stale heartbeat or finish changes nothing.
  - A statement that was waiting re-checks the latest row version once the lock is released.
  - None of this depends on timeouts.

## Failure semantics

| Crash or failure point | Result | Cleanup |
|---|---|---|
| During the file write | only `<key>.tmp` exists | sweeper removes `.tmp` older than the grace period |
| After the rename, before the metadata row | file with no row | sweeper removes unreferenced files older than the grace period |
| Metadata statement fenced (cancelled, recovered, stale epoch) | 0 rows; the writer removes its file | sweeper, if the removal itself fails |
| After the metadata row commits | row and file; the attempt is later `ABANDONED` by recovery | kept (never graded); garbage collection is out of scope |
| Metadata statement times out or misses `SAVE_DEADLINE` | no row; the writer removes its file; capture `persist_failed`, evaluation ERROR `capture_persist_failed` | sweeper, if the removal itself fails |
| Worker killed during the CANCEL call | the server finishes the call atomically (decision C): `CANCELLED`, no row | sweeper removes the files |
| After CANCEL commits, before the unlink | `CANCELLED`, no row; file left behind | sweeper |
| Row present, file missing or wrong hash | evaluation becomes a platform error (`artifact_missing`) | none; the row is the evidence |

**The sweeper** runs in the reconciler. It deletes only files that no row references and that are older than the grace period. It lists files before reading the keys, and it never deletes a file a row points to.

## Implementation

Implemented in Phase 5 as follows:

| Part | Step | Where |
|---|---|---|
| Attempt-row fence for trajectory appends (B), server-side finish and cancel calls (C) | 2, 3 | migrations `0004`/`0005` (the plpgsql `p5_finish` and `p5_cancel`), `repositories/runs.py` |
| Lease keeper process and worker self-stop (D) | 4 | `reliability/keeper*.py`, `lease_manager.py` |
| Reconciler recovery (never waits, never touches artifacts) | 5 | `reconciler.py`, `repositories/recovery.py` |
| `ArtifactStore`, the save protocol, the sweeper (A, E) | 6 | `agenteval_core/artifacts/`, executor, reconciler step 5 |

Settings keep the values above (`AGENTEVAL_ARTIFACT_INSERT_TIMEOUT_S` 10, `AGENTEVAL_ARTIFACT_SAVE_DEADLINE_S` 15, `AGENTEVAL_ARTIFACT_SWEEP_GRACE_S` 120). The reliability behaviour as a whole is described in [ADR-009](ADR-009-reliability-and-recovery.md).

## Consequences

- **Benefits:**
  - Heartbeat, cancellation and finish latency no longer depends on snapshot size.
  - WAL volume stops growing with snapshot size.
  - Postgres backups shrink.
- **New infrastructure: one volume.** Every worker that writes or grades snapshots must mount it. This is fine for the single-host compose deployment. A multi-host deployment would need an object store behind `ArtifactStore`.
- **Two stores can disagree.** The row-is-authoritative rule, hash-addressed keys and the sweeper make every disagreement either harmless (an unreferenced file) or explicit (a missing file becomes a platform error).
- **Backups:** the volume must be backed up together with the database. A restored database whose files are missing produces platform errors, not wrong grades.
- **Schema change:** this amends the earlier "no schema change" plan for Phase 5 (one migration adds `storage_key` and the CHECK constraint).

## Alternatives considered

| Option | Why not |
|---|---|
| Keep `bytea` and change only the trajectory fence (B alone) | Measured: cancellation waits up to 2.3 s from WAL and CPU pressure, which fails the 1 s requirement |
| `synchronous_commit = off` for heartbeats | Hides the symptom for heartbeats only; cancellation and finish stay slow; loses durability of lease renewals |
| Smaller snapshot cap | Changes what can be graded and only scales the problem down |
| MinIO or S3 now | A new service and credentials to run; the volume behind the same interface is enough for one host |

## Validation

**Setup:**
- a dedicated scratch Postgres 16.11;
- the worker-side harness in a container on the Docker VM;
- the heartbeat and cancel-API processes separate from the worker;
- the same synthetic load as every earlier run.

**Results:** all of the following passed.
- **Under load (439 s, 203 cycles):**
  - heartbeat during a save: max 0.96 s;
  - heartbeat during a delete: max 0.50 s;
  - `runs` lock held: max 0.99 s, measured from lock to reply received; inside Postgres max 0.12 s;
  - cancel wait: max 0.68 s during a save, 0.76 s during a delete;
  - 0 deadlocks; WAL 2 MiB.
- **Deterministic concurrency tests:** 40/40.
- **10-minute randomized soak:** 0 deadlocks, 0 invariant violations, every row's file present, 55 recoveries.
- **SIGKILL tests:** 15/15, at 10 kill points.

The checks:

1. Heartbeat during save and delete: under 5 s.
2. Cancel API wait during save and delete: under 1 s.
3. `runs` lock held: under 1 s.
4. Deterministic concurrency tests, including file-specific cases:
   - file without a row;
   - fenced save removes its file;
   - CANCEL unlinks after commit;
   - idempotent retry with the same hash;
   - different content under the same `(attempt, kind)`;
   - missing or corrupt file becomes a platform error;
   - the sweeper never deletes a referenced file.
5. A 10-minute randomized soak with zero deadlocks, no cancellation-invariant violation, and no row whose file is missing.
6. SIGKILL before, during and after the file write, the metadata statement and the CANCEL delete. Locks must release, recovery must proceed, and only the outcomes in the failure table may appear.

Saves under this load took p50 1.0 s and max 4.8 s (file write p50 0.9 s, metadata statement p50 59 ms). The proposed timeout, deadline and grace values above follow from these measurements. Lease values (TTL 30 s, heartbeat 10 s, self-fence margin 10 s) do not change.
