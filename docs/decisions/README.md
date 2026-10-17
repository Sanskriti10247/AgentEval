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

Related documents that are not ADRs: the [security review](../security-review.md) (findings and accepted risks), the [evaluation architecture](../architecture/evaluation.md), the [deployment guide](../deployment.md), the [API reference](../api/) and the [benchmark report](../benchmarks/README.md).
