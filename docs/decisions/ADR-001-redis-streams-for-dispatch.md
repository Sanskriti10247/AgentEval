# ADR-001: Redis Streams for dispatch (not Kafka, Celery, or a plain list)

**Status:** Accepted (vertical slice)

## Context
The API must hand runs to a pool of workers. Delivery needs acknowledgement,
must survive a worker restart, and must be easy to reason about.
The stack is fixed on Redis, and Kafka would only come in with a concrete need.

## Decision
Use one Redis stream, `runs.dispatch`, with one consumer group, `workers`.
- Each message carries only a `run_id`.
- Workers read with `XREADGROUP ... BLOCK`.
- A worker acks a message only after the attempt is durably `RUNNING` in Postgres.

## Why
- Streams give acknowledged delivery with a pending-entries list. A plain `LIST` has neither, so both would have to be built by hand.
- `BLOCK` removes polling cost.
- The consume loop is about 50 lines of our own code, which keeps the worker lifecycle visible.

## Alternatives
| Option | Why not (for now) |
|---|---|
| Redis `LIST` + `BLMOVE` | Workable, but visibility and recovery have to be built by hand |
| Celery / RQ | Hides the worker lifecycle and retry semantics the project exists to show |
| Kafka | Operationally heavy, with no benefit at this scale |
| Postgres `FOR UPDATE SKIP LOCKED` | Genuinely simpler (one system, no dual write). **This is the documented fallback** if Redis stops paying its way |

## Consequences
- **Two systems means a dual write.** The API commits the run to Postgres first and then `XADD`s. If the `XADD` fails, the API returns 503 and the run stays `QUEUED`. A client retry with the same `Idempotency-Key` re-dispatches it. Phase 5 added a reconciler (redispatch of lost `QUEUED` runs and reclaim of pending entries), so recovery no longer depends on the client; see [ADR-009](ADR-009-reliability-and-recovery.md).
- **Duplicate messages are expected and harmless.** The claim is a conditional `UPDATE ... WHERE status='QUEUED'` (ADR-002).
- **Note on redis-py ≥ 8:** its default `socket_timeout` is 5 s. A blocking read must use a longer socket timeout (`redis_for_blocking_reads`), or it fails after 5 s.
