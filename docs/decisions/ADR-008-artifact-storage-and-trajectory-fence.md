# ADR-008: Artifact bytes on a volume, metadata in Postgres; trajectory writes fenced on the attempt row

**Status:** Proposed (Phase 5). Validation is in progress; nothing is implemented yet.

Related: ADR-002 (Postgres is the source of truth), ADR-006 (evaluation; this ADR would replace its D3 storage choice).

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

The A+B run also completed far more cycles: 884 saves of 16 MiB, against 36 with `bytea`. That last row is a measurement, not yet a pass. The files were written to the macOS host disk, where `fsync` does not force data to stable storage, and the soak and crash tests have not run against this design yet (see Validation).

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

### Invariants kept

- A `CANCELLED` attempt has no artifact row, no workspace capture and no evaluation.
- A stale owner cannot save, append, heartbeat or finish. Its attempt row no longer matches (`status`, `lease_epoch`), and a statement that was waiting re-checks the latest row version once the lock is released.
- Artifacts on `ABANDONED` attempts may remain; they are never graded.

## Failure semantics

| Crash or failure point | Result | Cleanup |
|---|---|---|
| During the file write | only `<key>.tmp` exists | sweeper removes `.tmp` older than the grace period |
| After the rename, before the metadata row | file with no row | sweeper removes unreferenced files older than the grace period |
| Metadata statement fenced (cancelled, recovered, stale epoch) | 0 rows; the writer removes its file | sweeper, if the removal itself fails |
| After the metadata row commits | row and file; the attempt is later `ABANDONED` by recovery | kept (never graded); garbage collection is out of scope |
| During CANCEL-mode delete (before commit) | rollback: row and file intact; recovery later finishes the cancellation | none |
| After CANCEL commits, before the unlink | `CANCELLED`, no row; file left behind | sweeper |
| Row present, file missing or wrong hash | evaluation becomes a platform error (`artifact_missing`) | none; the row is the evidence |

**The sweeper** runs in the reconciler. It deletes only files that no row references and that are older than a grace period. The grace period is longer than the longest possible save. It never deletes a file a row points to.

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

## Validation (required before implementation)

All checks run again on the A+B design, on a dedicated scratch Postgres, with the worker-side harness running in a container on the Docker VM. This means files are written to the VM's disk with Linux `fsync` semantics.

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

The artifact statement timeout is chosen from these measurements. Lease values (TTL 30 s, heartbeat 10 s, self-fence margin 10 s) do not change.
