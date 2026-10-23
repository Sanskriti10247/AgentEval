# Database schema

Postgres holds all persistent state. The models in `packages/core/agenteval_core/models/` are the source, and Alembic migrations `0001` to `0010` build the schema. The diagram shows the main tables and the columns the rest of the documentation refers to, not every column.

```mermaid
erDiagram
    agents ||--o{ agent_versions : "has immutable"
    datasets ||--o{ tasks : contains
    datasets ||--o{ evaluations : "evaluated in"
    agent_versions ||--o{ evaluations : "evaluated by"
    agent_versions ||--o{ runs : "executes"
    tasks ||--o{ runs : "is run as"
    evaluations ||--o{ runs : "batches"
    runs ||--o{ run_attempts : "has"
    runs ||--o{ run_replays : "replayed by"
    run_attempts ||--o{ trajectory_events : "records"
    run_attempts ||--o{ attempt_artifacts : "snapshots"
    run_attempts ||--o{ attempt_evaluations : "graded by"
    attempt_evaluations ||--o{ scores : "scored as"

    agents {
        uuid id PK
        text name UK
    }
    agent_versions {
        uuid id PK
        uuid agent_id FK
        int version
        text model_provider
        text model
        text_array tools
        jsonb limits
        text config_hash
    }
    datasets {
        uuid id PK
        text name
        text version
        text status "DRAFT or FROZEN"
    }
    tasks {
        uuid id PK
        uuid dataset_id FK
        text task_type "command or agent"
        text image
        jsonb files
        jsonb evaluation "write-only hidden spec"
        int timeout_s
    }
    evaluations {
        uuid id PK
        uuid dataset_id FK
        uuid agent_version_id FK
        int repetitions
        jsonb scoring_config
        text scoring_config_hash
        text idempotency_key UK
    }
    runs {
        uuid id PK
        uuid task_id FK
        uuid agent_version_id FK
        uuid evaluation_id FK
        text status
        text outcome
        text worker_id
        bigint lease_epoch
        timestamptz lease_expires_at
        int max_attempts
        text dead_letter_reason
        text idempotency_key UK
    }
    run_attempts {
        uuid id PK
        uuid run_id FK
        int attempt_no
        text status
        text outcome
        text failure_reason
        int steps
        decimal cost_usd
        text final_answer
    }
    trajectory_events {
        uuid id PK
        uuid attempt_id FK
        int seq
        text event_type
        text tool
        jsonb input
        jsonb output
        int duration_ms
    }
    attempt_artifacts {
        uuid id PK
        uuid attempt_id FK
        text kind
        text sha256
        text storage_key
    }
    attempt_evaluations {
        uuid id PK
        uuid attempt_id FK
        text status
        bool task_success
        text failure_category
        jsonb checks
        bigint lease_epoch
    }
    scores {
        uuid id PK
        uuid attempt_evaluation_id FK
        jsonb components
        decimal overall
    }
    run_replays {
        uuid id PK
        uuid run_id FK
        int replay_no
    }
    workers {
        text id PK
        timestamptz last_heartbeat_at
        timestamptz stopped_at
        text stop_reason
    }
    api_keys {
        uuid id PK
        text prefix UK
        bytea secret_sha256
        text_array scopes
        timestamptz revoked_at
    }
```

`workers` and `api_keys` stand alone. A run records the id of the worker that owns it, but there is no foreign key, so a worker row can be pruned without touching history.

## Design notes

- The database enforces state, not just the code. CHECK constraints tie `status`, `outcome`, `failure_reason` and `dead_letter_reason` together, so an impossible combination cannot be written.
- Every state change is a guarded `UPDATE`, or a server-side function for the terminal writes. The `WHERE` clause carries the expected status and `lease_epoch` ([ADR-002](../decisions/ADR-002-postgres-source-of-truth.md), [ADR-009](../decisions/ADR-009-reliability-and-recovery.md)).
- The evaluation is a separate row from the attempt (`attempt_evaluations`, then `scores`), so grading never rewrites what the attempt recorded.
- Scores store each component separately, with the scoring configuration and its hash, never only one number.
- Large artifacts are files, not rows. Workspace snapshots live on a volume, with a metadata row (`storage_key`, size, SHA-256) in Postgres ([ADR-008](../decisions/ADR-008-artifact-storage-and-trajectory-fence.md)).

## Migrations

| Migration | Adds |
| --- | --- |
| `0001` | tasks, runs, attempts |
| `0002` | the agent runtime (agents, versions, trajectory) |
| `0003` | datasets, evaluations, artifacts, attempt evaluations, scores |
| `0004`–`0009` | reliability: leases, failure reasons, budgets, worker stop reasons, evaluation leases, replays |
| `0010` | `api_keys` |

Several of these refuse to downgrade once their data exists; see [deployment.md](../deployment.md#downgrades).
