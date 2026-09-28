# ADR-002: PostgreSQL is the source of truth

**Status:** Accepted (vertical slice)

## Context
Job state is spread across the API, Redis and N workers. When they disagree, one of them has to be authoritative.

## Decision
Postgres is authoritative for every persistent fact: tasks, runs, attempts, status, outcome and ownership. Redis only *delivers* run ids.

Every state change is a single guarded `UPDATE`:

```sql
UPDATE runs SET status='CLAIMED', worker_id=:w, claimed_at=now()
WHERE id=:id AND status='QUEUED' RETURNING ...
```

- If 0 rows come back, the caller lost a race or the message was stale. The worker acks and skips.
- `start_attempt` and `finish_attempt` are also fenced on `worker_id`, and `finish_attempt` additionally on `attempt_no`, so a worker that no longer owns a run can't write to it.
- `UNIQUE(run_id, attempt_no)` means attempt N can exist only once.

The database also enforces the semantics with CHECK constraints:
- `outcome` is non-NULL exactly when the run is terminal.
- `COMPLETED` only with a task-caused outcome.
- `FAILED` only with a platform-fault outcome.

## Why
- Correctness under concurrency comes from one transactional system, not from coordinating two.
- Redis can be flushed or lose messages without losing or corrupting job state.

## Consequences
- The PG write rate grows with job transitions (and, in Phase 5, with heartbeats). This is fine at project scale and will be measured in Phase 7.
- Every read endpoint reads from Postgres, never Redis.
