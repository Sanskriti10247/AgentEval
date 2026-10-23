# Architecture Decision Records

Each ADR records one significant decision: the context, what was chosen, the alternatives considered and the consequences. They are written when the decision is made and corrected, not rewritten, when implementation teaches something (ADR-008 keeps its validation history for that reason).

| ADR | Decision | Phase | Read it for |
| --- | --- | --- | --- |
| [ADR-001](ADR-001-redis-streams-for-dispatch.md) | Redis Streams for dispatch (not Kafka, Celery, or a plain list) | vertical slice | why a broker this small is enough, and why delivery is at least once |
| [ADR-002](ADR-002-postgres-source-of-truth.md) | PostgreSQL is the source of truth | vertical slice | guarded updates, CHECK constraints, Redis as delivery only |
| [ADR-003](ADR-003-docker-execution-isolation.md) | Docker for execution isolation | vertical slice | the container controls and what they do not protect against |
| [ADR-004](ADR-004-modular-monolith-and-workers.md) | Modular monolith plus workers | vertical slice | one package, two process types |
| [ADR-005](ADR-005-agent-runtime.md) | Hand-written agent runtime on the existing worker | 3 | the loop, tools, limits, trajectory, provider interface |
| [ADR-006](ADR-006-evaluation-and-scoring.md) | Evaluation as a separate record, graded in a fresh container | 4 | check kinds, failure categories, scoring, statistics |
| [ADR-007](ADR-007-evaluation-secrecy.md) | Evaluation secrecy (D11) | 4 | why hidden specs are write-only and every response is a projection |
| [ADR-008](ADR-008-artifact-storage-and-trajectory-fence.md) | Artifact bytes on a volume, metadata in Postgres; trajectory writes fenced on the attempt row | 5 | snapshot storage, the fencing protocol, the sweeper |
| [ADR-009](ADR-009-reliability-and-recovery.md) | Reliability and recovery: attempts, leases, fencing, reconciler, cancellation, dead letters | 5 | the transition tables and the failure-semantics table |
| [ADR-010](ADR-010-observability.md) | Prometheus metrics, OpenTelemetry traces and the web dashboard | 6 | metric families, span tree, what is never recorded |
| [ADR-011](ADR-011-benchmarking.md) | Platform benchmarks: method, harness and reporting | 7 | what was measured, how, and what the numbers cannot say |
| [ADR-012](ADR-012-production-polish.md) | Authentication, authorization, rate limiting, CI/CD and deployment | 8 | API keys and scopes, rate limits, the pipeline, the production compose file |

Related documents that are not ADRs: the [security review](../security-review.md) (findings and accepted risks), the [evaluation architecture](../architecture/evaluation.md), the [database schema](../architecture/schema.md), the [deployment guide](../deployment.md), the [API reference](../api/README.md), [observability](../observability.md), [testing](../testing.md) and the [benchmark report](../benchmarks/README.md).

## Deviations from the original plan

Implementation changed a few things the approved plan had said. Most are recorded in the ADR they belong to; this list collects them in one place.

The vertical slice (S1):

1. Files are copied in as a tar streamed over `exec` stdin, not with `put_archive`. Docker rejects `put_archive` when the root filesystem is read-only, and keeping the read-only rootfs won ([ADR-003](ADR-003-docker-execution-isolation.md)).
2. The worker's Redis client sets `socket_timeout` longer than its `XREADGROUP BLOCK`. redis-py 8 defaults it to 5 s, which broke 5 s blocking reads ([ADR-001](ADR-001-redis-streams-for-dispatch.md)).
3. `watchfiles` is an explicit worker dependency. The plan assumed it came with `uvicorn[standard]`, which only the API installs.
4. Runs have an `error_message` column, so a run that fails before any attempt exists (`CLAIMED → FAILED`) still records why.
5. The orphan sweep is deadline-based (an `agenteval.deadline` label), so it is safe with several workers sharing one Docker daemon ([ADR-003](ADR-003-docker-execution-isolation.md)).

The agent runtime, Phase 3 ([ADR-005](ADR-005-agent-runtime.md#deviations-from-the-approved-design-found-while-implementing)):

6. A post-tool process reaper was added after the §15 spike showed that `timeout` alone misses background children and `setsid` escapees.
7. `Workspace.abort()` was added so cancellation can interrupt a tool call in progress.
8. Tool commands set `PYTHONDONTWRITEBYTECODE=1`, and tool writes use real mtimes. Otherwise a same-size edit within the same second ran stale bytecode.
9. Tool schemas are sent to Anthropic as a strict-compatible copy. Unsupported constraints are removed and described in words; the full schema is still enforced.
10. The Anthropic effort level is stored explicitly (default `high`), so a version's hash says exactly what is sent. Haiku 4.5 takes no effort or thinking parameters.
11. A latent S1 consumer bug was fixed: shutdown didn't start the drain while every concurrency slot was busy. The drain is now phased within the same 330 s total.
12. Over-cap agent limits are rejected (422), not clamped.

Evaluation, Phase 4 ([ADR-006](ADR-006-evaluation-and-scoring.md#deviations-found-while-implementing), [ADR-007](ADR-007-evaluation-secrecy.md)):

13. Hidden evaluation data is never readable through the API. The first design allowed it until authentication existed; it was amended to strict secrecy before implementation.
14. Check kinds were split into `python_tests`, `io`, `command` (trusted environment only), `final_answer` and `file`, after the spike found in-process grading attacks (C2–C7).
15. Any tar warning fails a snapshot export. GNU tar skips unix sockets with exit 0, which would be a silent loss.
16. Rescoring was deferred, though it was in the original API design.
17. Task request and response schemas moved to `schemas/task_api.py` to break an import cycle with the evaluation spec.
