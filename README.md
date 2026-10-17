# AgentEval

[![CI](https://github.com/Sanskriti10247/AgentEval/actions/workflows/ci.yml/badge.svg)](https://github.com/Sanskriti10247/AgentEval/actions/workflows/ci.yml)
[![E2E](https://github.com/Sanskriti10247/AgentEval/actions/workflows/e2e.yml/badge.svg)](https://github.com/Sanskriti10247/AgentEval/actions/workflows/e2e.yml)

A platform for running, observing, evaluating and benchmarking AI agents inside controlled execution environments.

![The AgentEval dashboard comparing three agents on one benchmark](docs/images/compare.png)

## Contents

1. [Overview](#1-overview)
2. [Why AgentEval exists](#2-why-agenteval-exists)
3. [Architecture](#3-architecture)
4. [Architecture diagram](#4-architecture-diagram)
5. [Features](#5-features)
6. [Tech stack](#6-tech-stack) (and the [repository layout](#repository-layout))
7. [Execution lifecycle](#7-execution-lifecycle)
8. [Agent architecture](#8-agent-architecture)
9. [Evaluation architecture](#9-evaluation-architecture)
10. [Database schema](#10-database-schema)
11. [Failure handling](#11-failure-handling) (and [observability](#observability))
12. [Security model](#12-security-model)
13. [Testing strategy](#13-testing-strategy)
14. [Benchmarks](#14-benchmarks)
15. [Local setup](#15-local-setup) (and the [dashboard](#dashboard), [deployment](#deployment))
16. [API documentation](#16-api-documentation)
17. [Screenshots](#17-screenshots)
18. [Demo](#18-demo)
19. [Engineering tradeoffs](#19-engineering-tradeoffs)
20. [Future work](#20-future-work)

## 1. Overview

AgentEval answers one question: *given the same benchmark, how reliably, quickly and cheaply does each agent solve the tasks, and why does it fail when it doesn't?*

You define an **agent** (a model, a system prompt, a set of tools, limits), a **dataset** of tasks, and optionally a hidden **evaluation** per task. The platform then:

1. queues one run per task and repetition and executes them asynchronously on a pool of workers;
2. runs each attempt in its own locked-down Docker container, where the agent works through a small set of controlled tools;
3. records the whole trajectory (every model call and tool call, with usage, cost, timings and errors);
4. grades the result objectively, in a *separate* fresh container, against hidden tests or expected outputs, and classifies each failure;
5. retries platform failures with backoff, recovers from crashed workers, and dead-letters what it cannot recover;
6. summarises and compares agents (success rate with a confidence interval, failure categories, latency, cost);
7. shows it all in a web dashboard, with Prometheus metrics and OpenTelemetry traces behind it.

Two kinds of task go through the same pipeline: **command tasks** run one command in a sandbox, and **agent tasks** hand instructions to an agent. The default model provider is a deterministic scripted fake, so everything runs offline and free; real Anthropic models are opt-in.

It is a portfolio project, built to show the engineering behind such a system: a state machine in Postgres, leases and fencing, at-least-once execution with exactly-once results, sandboxing, objective evaluation, observability, tests and benchmarks. It is **not** a hardened multi-tenant service (see the [Security model](#12-security-model)), and its [benchmark numbers](#14-benchmarks) come from one laptop.

## 2. Why AgentEval exists

Evaluating an agent is a systems problem as much as a modelling one, and the failure modes are mostly not about the model:

- **A score is only meaningful if the run was fair.** An agent that "failed" because a container died or a worker crashed was never tested. AgentEval keeps the *platform's* verdict (`COMPLETED` or `FAILED`) apart from the *task's* verdict (`passed`, `tests_failed`, `wrong_answer`, ...), retries only platform faults, and excludes them from the success rate instead of counting them against the agent.
- **Agents run untrusted code.** It has to be isolated, bounded in time, memory and output, and kept away from credentials and from the answer key.
- **The answer key has to stay secret.** If the agent can read the hidden tests, the benchmark measures nothing. Hidden data lives in a separate evaluation container and never appears in any API response.
- **Results need an explanation.** A single number hides why an agent failed. Every attempt gets one failure category, its score components are stored separately, and the trajectory is kept.
- **Long, flaky, expensive work needs reliability engineering:** leases, retries, idempotency and recovery, so a run is never lost, never recorded twice, and its cost is tracked.

It deliberately avoids the fashionable parts that would not earn their place at this scale: no Kubernetes, no Kafka, no agent framework (the loop is one readable module, `agent/runtime.py`), one Postgres, one Redis, a handful of Docker containers.

## 3. Architecture

```text
client ──HTTP──► api (FastAPI) ──1. INSERT run(QUEUED), COMMIT──► PostgreSQL  (source of truth)
                     │
                     └─2. XADD runs.dispatch {run_id}──► Redis Stream (delivery only)
                                                              │ XREADGROUP (group "workers")
                                                              ▼
                               worker (asyncio, N replicas, semaphore-bounded)
                                 3. UPDATE runs … WHERE status='QUEUED'   (conditional claim)
                                 4. attempt row + RUNNING, COMMIT, then XACK
                                 5. execute:
                                    command task → one exec in a sandbox container
                                    agent task   → AgentRunner: sandbox session + model loop,
                                                   trajectory events written as it goes
                                 6. persist outcome + terminal status (one fenced transaction)
                                 7. agent tasks: evaluate in a fresh container, then persist the
                                    evaluation + score (separate transactions, after step 6)
client ──GET /api/v1/runs/…, /evaluations/…──► api ──► PostgreSQL
```

- **Modular monolith + workers.** A single domain package, `agenteval_core`, is used by two process types, `api` and `worker` (ADR-004).
- **Postgres is authoritative.** Redis only delivers run ids, and every state change is a guarded `UPDATE` (ADR-002).
- **Redis Streams** handle dispatch (runs on `runs.dispatch`, gradings on `evaluations.dispatch`). Duplicate or lost messages are harmless: the claim is conditional and the reconciler republishes (ADR-001).
- **One Docker container per attempt**, with no network, resource limits, a read-only rootfs and a non-root user (ADR-003).
- **The agent runtime is hand-written,** not a framework. It is provider-neutral and lives in core (ADR-005).
- **Evaluation is a separate record, written after the attempt is durable,** and graded in a fresh hardened container. It never changes the run's outcome (ADR-006).
- **Hidden evaluation data never leaves the evaluator.** Specs are write-only, and every API response goes through one public projection (ADR-007).
- **Every write by a lease owner is fenced** by an epoch, and a reconciler in every worker recovers what crashes leave behind (ADR-008, ADR-009).

## 4. Architecture diagram

```mermaid
flowchart LR
    client(["Client / dashboard"])
    subgraph api_box["API (FastAPI)"]
        api["routes, services, repositories<br/>API keys, scopes, rate limits"]
    end
    pg[("PostgreSQL<br/>source of truth")]
    redis[("Redis Streams<br/>delivery only")]
    subgraph workers["Workers (N replicas)"]
        direction TB
        consumer["consumer: claim, lease, fence"]
        runner["executor / agent runner<br/>evaluation runner"]
        recon["reconciler + lease keeper"]
    end
    docker["Docker daemon"]
    subgraph sandboxes["Per-attempt containers"]
        agentc["agent sandbox<br/>no network, tools only"]
        evalc["evaluation container<br/>hidden tests"]
    end
    llm(["Model provider<br/>fake or Anthropic"])
    vol[("Artifact volume<br/>workspace snapshots")]
    obs["Prometheus / Jaeger<br/>(opt-in)"]

    client -- "REST" --> api
    api -- "1. INSERT run QUEUED" --> pg
    api -- "2. XADD run id" --> redis
    redis -- "XREADGROUP" --> consumer
    consumer -- "3. guarded UPDATE" --> pg
    consumer --> runner
    runner -- "create / exec" --> docker
    docker --> agentc
    docker --> evalc
    runner -- "model calls" --> llm
    runner -- "attempt, trajectory, evaluation, score" --> pg
    runner -- "snapshot" --> vol
    recon -- "recover, retry, republish" --> pg
    recon --> redis
    api -. "/metrics" .-> obs
    workers -. "metrics, traces" .-> obs
```

Reading it: the client talks only to the API. The API commits a run to Postgres *before* it publishes the run id to Redis, so a lost message is a delay, never a lost run. Workers claim runs with a conditional `UPDATE`, execute them in containers they create through the Docker daemon, and write every result back to Postgres under a fencing epoch. The reconciler in each worker repairs whatever crashes and lost messages leave behind.

## 5. Features

- **Asynchronous execution** on any number of workers (`docker compose up -d --scale worker=3`), with a bounded number of concurrent runs per worker.
- **Sandboxed execution:** one container per attempt with no network, a read-only root filesystem, dropped capabilities, resource limits and wall-clock timeouts, always removed afterwards.
- **A hand-written agent loop** with six controlled tools, step, token, cost and time limits, cancellation, and a provider-neutral model interface (a deterministic fake, and Anthropic).
- **Full trajectories:** every model call and tool call as a structured event with usage, cost, duration and errors. Reasoning ("thinking") blocks are never stored.
- **Objective evaluation:** hidden tests, output comparison, final-answer and file checks, tamper detection, a failure category per attempt, and a transparent, configurable score with every component stored.
- **Benchmark evaluations:** run an agent version over a frozen dataset with repetitions, then get the success rate with a Wilson 95% interval, failure categories, latency, cost, and per-task results; compare two to five agents on the same dataset.
- **Reliability:** budgeted attempts with exponential backoff and jitter, fenced leases and a separate heartbeat process, a reconciler that recovers crashes and lost messages, idempotency keys, cancellation, dead letters and replay.
- **Observability:** structured JSON logs with run, task, attempt and worker ids, Prometheus metrics, OpenTelemetry traces across the Redis hop, and a Next.js dashboard.
- **Access control:** API keys with `read`, `write` and `admin` scopes, per-key rate limits, key redaction in logs.
- **Delivery:** GitHub Actions CI (lint, types, unit, integration, dashboard, dependency audit, image builds), release images, and a production compose file with Caddy and automatic TLS.
- **Benchmarks of the platform itself:** a reproducible harness and a measured throughput and worker-scaling report.

## 6. Tech stack

| Layer | Choice | Why |
| --- | --- | --- |
| Language, API | Python 3.12, FastAPI, Pydantic 2 | typed request and response models, async I/O |
| Database | PostgreSQL 16, SQLAlchemy 2 (async, asyncpg), Alembic | authoritative state, guarded updates, CHECK constraints, migrations (ADR-002) |
| Queue | Redis 7 Streams | delivery and consumer groups; never the source of truth (ADR-001) |
| Execution | Docker (Docker SDK for Python) | per-attempt isolation with resource limits (ADR-003) |
| Agent runtime | hand-written loop, `anthropic` SDK in the worker only | understandable, provider-neutral (ADR-005) |
| Observability | `prometheus-client`, OpenTelemetry (OTLP), structlog | metrics, traces, JSON logs (ADR-010) |
| Dashboard | Next.js 16 (React 19), TypeScript, Tailwind CSS 4, TanStack Query, Recharts | read-only view over the REST API |
| Edge | Caddy 2 | automatic TLS, headers, body cap (production only) |
| Tooling | uv, ruff, mypy, pytest (+ testcontainers, hypothesis), Vitest, GitHub Actions | |

Every runtime dependency has a reason recorded in the ADRs; there is no agent framework and no message broker beyond Redis.

### Repository layout

```text
packages/core/agenteval_core/
  config, db, states (state machine), hashing, auth (API-key format and hashing),
  logging (with secret redaction), pagination
  models/, schemas/, repositories/, services/, queue/
  reliability/  state machines, failure taxonomy, retry policy and backoff, lease timing,
                lease keeper process
  artifacts/    ArtifactStore (snapshot files on a volume; metadata row in Postgres)
  agent/        runtime loop, limits, context, events, prompts, pricing, retry policy
  providers/    ModelProvider protocol, FakeProvider, Anthropic settings (no SDK here)
  tools/        Tool contract, ToolSet/registry, the six tools, Workspace protocol
  evaluation/   pure grading domain: spec, checks (plan/judge), matchers, parsers,
                integrity, metrics, classify, scoring, stats, public (the D11 projection)
  observability/ Prometheus metrics, scrape-time gauges, OpenTelemetry tracing
packages/core/migrations/       Alembic: 0001 (tasks, runs, attempts), 0002 (agent runtime),
                                0003 (datasets, evaluations, artifacts, attempt evaluations, scores),
                                0004-0009 (reliability: leases, failure reasons, budgets, worker stop
                                reasons, evaluation leases, replays), 0010 (api_keys)
apps/api/agenteval_api/         FastAPI app, deps, auth (scope guard), ratelimit, keys (operator CLI),
                                middleware (metrics, tracing, body limit), error mapping
                                (sanitised 422s), routes/
apps/worker/agenteval_worker/   main (signals, drain), consumer, executor, agent_runner,
                                evaluation_runner, reconciler, lease_manager, trajectory writer,
                                sandbox/ (session, evaluation, workspace), providers/
apps/web/                       Next.js dashboard (app/, components/, lib/, tests/)
benchmarks/                     the Phase 7 harness (run, matrix, sampler, report) and its tests
scripts/                        demo.sh (guided walkthrough), capture-screenshots.mjs
infrastructure/                 docker/ (api, worker, web, sandbox Dockerfiles), caddy/, monitoring/
tests/integration/              testcontainers Postgres 16 + Redis 7 (real migrations)
tests/e2e/                      against the running compose stack
docs/                           architecture/, api/, decisions/ (ADR-001 ... ADR-012, indexed in
                                decisions/README.md), benchmarks/, images/, deployment.md,
                                security-review.md
.github/workflows/              ci.yml, e2e.yml, release.yml
docker-compose.yml              the dev stack (CPU weights, artifact volume)
docker-compose.prod.yml         production override (Caddy, passwords, limits, hardening)
docker-compose.e2e.yml          short lease timings, applied by `make e2e`
docker-compose.observability.yml  trace export to Jaeger, applied by `make observability-up`
```

## 7. Execution lifecycle

A run goes `QUEUED` → `CLAIMED` → `RUNNING` → a terminal state, possibly through `RETRYING`. The sequence for one run:

1. `POST /api/v1/runs` inserts the run as `QUEUED` and **commits**, then publishes its id on the `runs.dispatch` stream. If the publish fails the API answers 503, and retrying with the same `Idempotency-Key` re-dispatches the same run.
2. A worker reads the id and claims the run with a conditional `UPDATE ... WHERE status='QUEUED'` that also sets a lease and bumps `lease_epoch`. Only one worker can win; duplicate messages are refused.
3. The worker inserts the attempt row (`RUNNING`), acknowledges the message, and executes the task: a command task is one `exec` in a sandbox, an agent task is the agent loop in a sandbox session.
4. A separate **lease keeper** process renews the worker's leases. If renewals stop, the worker stops itself before the lease can expire.
5. The outcome, the terminal status and the attempt row are written in **one fenced transaction**: a stale owner's write matches zero rows once recovery has bumped the epoch.
6. For agent tasks with an evaluation spec, the workspace is snapshotted, the sandbox closed, and the attempt is graded in a fresh container ([section 9](#9-evaluation-architecture)); the evaluation is a separate record written after the attempt is durable.

These are two separate columns, because they answer two separate questions.

| `runs.status` (platform lifecycle) | Meaning |
| --- | --- |
| `QUEUED` | persisted, waiting for a worker |
| `CLAIMED` | exactly one worker owns it (its conditional `UPDATE` won) |
| `RUNNING` | an attempt is executing |
| `RETRYING` | the last attempt failed with a retryable reason and budget is left; waiting for its backoff |
| `COMPLETED` | the platform produced a **trustworthy outcome** (the task may still have failed) |
| `FAILED` | the platform **could not** produce a trustworthy outcome (and, if `dead_letter_reason` is set, will not retry it; it can be replayed) |
| `CANCELLED` | a cancel request won: no outcome, no evaluation |

| `outcome` | Task type | Meaning | Status |
| --- | --- | --- | --- |
| `SUCCESS` | command | exit 0 within the time limit | COMPLETED |
| `NON_ZERO_EXIT` | command | exit ≠ 0, including an OOM kill (`oom_killed=true`, exit 137) | COMPLETED |
| `TIMEOUT` | both | the wall-clock limit was hit | COMPLETED |
| `AGENT_FINISHED` | agent | the model gave a final answer (no tool calls) | COMPLETED |
| `MAX_STEPS` | agent | the step limit was reached | COMPLETED |
| `BUDGET_EXCEEDED` | agent | the token or USD budget was reached | COMPLETED |
| `MODEL_REFUSAL` | agent | the model refused (the category is recorded) | COMPLETED |
| `MODEL_ERROR` | agent | the provider failed (retries or time exhausted, a non-retryable error, an unexpected stop) | FAILED |
| `SANDBOX_ERROR` | both | the sandbox couldn't be created, driven or kept alive | FAILED |
| `INTERNAL_ERROR` | both | cancellation (e.g. `cancelled: worker_shutdown`), an invalid config at run time, or a bug | FAILED |

**`COMPLETED` never means the task was solved.** It means the result is attributable to the task or the agent. Whether an agent actually solved its task is decided by the [evaluation](#9-evaluation-architecture), recorded separately from the run.

`FAILED` outcomes are platform faults, so the task was never fairly tested; they are the only ones that are retried (and only when the failure reason is retryable). A `COMPLETED` outcome is never retried.

The mapping lives in one pure function, `states.status_for()`, and the database enforces it with CHECK constraints.

Legal transitions (the full table with transition codes is in [ADR-009](docs/decisions/ADR-009-reliability-and-recovery.md)):

```mermaid
stateDiagram-v2
    [*] --> QUEUED: POST /runs
    QUEUED --> CLAIMED: worker wins the conditional UPDATE
    CLAIMED --> RUNNING: attempt row created
    CLAIMED --> QUEUED: claim lease expired (at most 3 times)
    CLAIMED --> FAILED: inconsistent run or task, or claims exhausted
    RUNNING --> COMPLETED: trustworthy outcome (the task may still have failed)
    RUNNING --> RETRYING: retryable failure, budget left
    RUNNING --> FAILED: non-retryable failure, or budget exhausted (dead letter)
    RETRYING --> QUEUED: backoff elapsed
    FAILED --> QUEUED: POST /runs/{id}/replay
    QUEUED --> CANCELLED: cancel
    CLAIMED --> CANCELLED: cancel
    RETRYING --> CANCELLED: cancel
    RUNNING --> CANCELLED: cancel (worker stops on its next lease beat)
    COMPLETED --> [*]
    CANCELLED --> [*]
```

Each run has one or more **attempts**; an attempt is one execution of the task in one sandbox, and carries the trajectory, the output and the evaluation:

```mermaid
stateDiagram-v2
    [*] --> RUNNING: claim won, container requested
    RUNNING --> FINISHED: outcome recorded (fenced write)
    RUNNING --> ABANDONED: lease expired (worker crash), recovered by the reconciler
    RUNNING --> CANCELLED: cancel won the race
    FINISHED --> [*]
    ABANDONED --> [*]
    CANCELLED --> [*]
```

An `ABANDONED` attempt makes the run `RETRYING` (or `FAILED` when the budget is spent); the next attempt is a new row. The attempt's [evaluation](#9-evaluation-architecture) is a separate record with its own status (`PENDING`, `RUNNING`, `SUCCEEDED`, `ERROR`, `SKIPPED`), so grading never changes what the attempt recorded.

`CLAIMED → QUEUED` happens when a claim's lease expires before an attempt starts (at most 3 times, then `FAILED` with `claim_abandoned`). `CLAIMED → FAILED` also happens when the run or task is inconsistent at claim time.

## 8. Agent architecture

### Agent runs

```text
AgentRunner (worker)
  validate the agent version → open SandboxSession (task files copied)
  provider.start(system prompt, tool specs, task instructions)
  AgentRuntime.run:
    run_started
    loop: stop if cancelled / past deadline / max_steps / token or cost budget
          model call (in-step retries)                     → model_call
          refusal → MODEL_REFUSAL   no tool calls → AGENT_FINISHED
          tool calls, one at a time, then one results message → tool_call …
    run_finished
  close the sandbox (always)
→ finish_attempt(outcome, steps, tokens, cost, final answer)
```

- **Agent versions are immutable.** A run references one exact config: model, system prompt, tools, shell allowlist, limits and provider settings.
- **Equivalent configs share a version.** The config is normalised (defaults filled in, lists sorted) and hashed, so an equivalent submission returns the existing version.
- **Model providers sit behind a neutral `ModelProvider` interface:**
  - **`fake`** (the default) replays a JSON script stored in the version. It is deterministic, offline and free.
  - **`anthropic`** calls the Messages API and is opt-in (see [Real models](#real-models-opt-in)).
- **The trajectory** is `run_started`, `model_call`, `model_error`, `tool_call`, `limit_reached` and `run_finished` events. It holds model usage and cost, visible text, tool inputs and outputs, errors and limits. **Thinking blocks are never stored.**

### Tools

| Tool | What it does |
| --- | --- |
| `read_file` | `cat` a workspace file |
| `write_file` | create or overwrite a file (parent directories created) |
| `list_dir` | sorted listing, depth 1–3 |
| `search` | `grep` with regex or literal patterns; results sorted by file then line, capped |
| `shell` | run one command from the version's allowlist, as an argv list (no shell string) |
| `run_tests` | run the task's `test_command`; offered only when the task has one |

- **Paths are relative to `/workspace` and validated.**
- **Model-supplied values reach the sandbox only as argv elements or positional `sh` parameters,** never spliced into a shell string.
- **Agent mistakes come back to the model as error results** (`unknown_tool`, `invalid_input`, `denied`, `error`, `timeout`), so it can react. A failing command run by `shell` or `run_tests` is information, not an error.
- **Output** sent to the model is capped at 16 KiB; the trajectory keeps up to 64 KiB.
- **Each tool call starts fresh. No process outlives its tool call:**
  - Each command runs under `timeout --kill-after=1 <n>`.
  - Afterwards a reaper SIGKILLs every process except PID 1, the container's keepalive and itself.
  - So an agent can't leave a server running between calls, and shell state (current directory, environment) doesn't carry over.
- **Python bytecode caching is off for tool commands** (`PYTHONDONTWRITEBYTECODE=1`), so an edit is always seen by the next run (ADR-005).

### Real models (opt-in)

Real model calls are **off by default** and are never needed by the standard tests. To enable Anthropic models:

```bash
# .env
AGENTEVAL_ENABLE_ANTHROPIC_PROVIDER=true    # read by both api (validation) and worker
ANTHROPIC_API_KEY=sk-ant-...                # passed to the worker container only
```

- **Supported models:** `claude-opus-5`, `claude-opus-5-5`, `claude-sonnet-5`, `claude-fable-5-1` and `claude-haiku-4-5`. Each is in the price table (list prices verified 2026-09-28).
- **What requests contain:** a system block with a cache breakpoint plus automatic caching; tools with `strict: true`; adaptive thinking and the version's effort level where the model supports them (Haiku 4.5 takes neither).
- **What requests never contain:** SDK retries (the runtime retries and records each try), refusal fallbacks (a result must come from the model the version names), forced `tool_choice`, or streaming.
- **The worker refuses to start** if the provider is enabled without a key.
- **Live smoke test:** `apps/worker/tests/test_anthropic_live.py` makes one small real request. It runs only with **both** `AGENTEVAL_LIVE_ANTHROPIC=1` and `ANTHROPIC_API_KEY` set, under `-m live`, and it costs money.

## 9. Evaluation architecture

```text
attempt COMPLETED, and the spec needs the workspace → snapshot /workspace (tar.gz, ≤ 16 MiB) → close
finish_attempt COMMIT → EvaluationRunner (its own transactions):
  record PENDING (or SKIPPED: platform failure / no spec; ERROR: invalid spec, failed capture)
  claim → fresh container: snapshot → /workspace, hidden files → /eval (root-owned, read-only)
        → evidence + integrity → checks (workspace restored before each) → classify → score
  SUCCEEDED (+ score) | ERROR; the container is always removed
```

- **Check kinds:** `python_tests` (hardened runner; exit 0 *and* exactly the expected test count), `io` (stdout compared on the host), `command` (trusted environment only), `final_answer`, `file`. Plus `protected_paths` for tamper detection.
- **One failure category per attempt,** with a fixed precedence: platform → evaluation error → tampering → passed → limits → behaviour → task result. Platform, evaluator and candidate failures stay distinct.
- **Scoring v1** = 0.60 task_success + 0.20 test_score + 0.10 reliability + 0.10 efficiency. Every component is stored separately; tampering forces 0. The weights are benchmark policy, configurable per batch and hashed.
- **Success rate** = passed ÷ graded, with a Wilson 95% interval. Platform failures, evaluator errors and tasks without a spec are excluded from the denominator and reported separately.

Details: [docs/architecture/evaluation.md](docs/architecture/evaluation.md) · API: [docs/api/evaluations.md](docs/api/evaluations.md) · decisions: ADR-006, ADR-007.

## 10. Database schema

Postgres holds all persistent state. The main tables and their relationships (columns shown are the ones the rest of this README refers to; `packages/core/agenteval_core/models/` is the source, and Alembic migrations `0001` to `0010` build it):

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

`workers` and `api_keys` stand alone (a run records the id of the worker that owns it, but there is no foreign key, so a worker row can be pruned without touching history). Design notes:

- **State is enforced by the database,** not just by code: CHECK constraints tie `status`, `outcome`, `failure_reason` and `dead_letter_reason` together, so an impossible combination cannot be written.
- **Every state change is a guarded `UPDATE`** (or a server-side function for the terminal writes): the `WHERE` clause carries the expected status and `lease_epoch`.
- **The evaluation is a separate row** from the attempt (`attempt_evaluations`, then `scores`), so grading never rewrites what the attempt recorded.
- **Scores store each component separately,** with the scoring configuration and its hash, never only one number.
- **Large artifacts are files, not rows:** workspace snapshots live on a volume, with a metadata row (`storage_key`, size, SHA-256) in Postgres (ADR-008).

## 11. Failure handling

The full design, the transition tables and the failure-semantics table are in [ADR-009](docs/decisions/ADR-009-reliability-and-recovery.md). In short:

- **Postgres decides; Redis only delivers ids.** Every state change is one guarded `UPDATE` or one server-side function call. A duplicate message, a stale worker or an overlapping reconciler can only be refused.
- **Attempts and retries.** A run has an attempt budget (`max_attempts`, 1 to 10, default 3). An attempt that fails with a *retryable* reason (transient model errors, sandbox faults, worker shutdown, a lost lease) goes `RETRYING`, waits `min(2 s × 2^(n-1), 60 s) × U(0.8, 1.2)`, and becomes a new attempt. Model calls are also retried inside a step. Retryability comes from a closed enum of failure reasons, never from error text. Completed outcomes (the agent finished, hit a limit, was refused, timed out) are never retried.
- **Leases and fencing.** A claim sets a lease and bumps `lease_epoch`. A separate *lease keeper* process renews the worker's leases. If renewals stop, the worker stops itself shortly before the lease can expire, and every write a stale owner attempts (heartbeat, trajectory, artifact, finish) matches zero rows once recovery has bumped the epoch.
- **The reconciler** runs in every worker, one advisory lock per step. It abandons and retries runs whose lease expired (worker crash), requeues claims that never started, republishes `QUEUED` runs and due retries, reclaims pending stream entries, recovers expired and missing evaluations, marks silent workers lost, trims the streams and sweeps orphaned containers and artifact files.
- **Cancellation.** `POST /runs/{id}/cancel` cancels a queued or retrying run at once; a claimed or running run is flagged and its worker stops it on the next lease beat. Cancelling and finishing race deterministically: if the finish committed first the API answers 409 and nothing changes. A cancelled attempt has no evaluation and no artifact.
- **Dead letters and replay.** A run that fails non-retryably, or exhausts its budget, is `FAILED` with `dead_letter_reason` (`non_retryable` or `retries_exhausted`). `GET /runs?dead_letter=true` lists them and `POST /runs/{id}/replay` gives one more attempts. History is kept.
- **Evaluation reliability.** Grading has its own budget (`max_eval_attempts`), lease and retries. A grading failure never touches the attempt or its trajectory; a worker crash during grading, or between finishing an attempt and recording its evaluation, is recovered by the reconciler. Snapshots are files on a shared volume with a metadata row, so any worker can grade any attempt.
- **Guarantees, and their limit.** Execution is **at least once**: a recovered attempt re-runs the agent from the start, so model calls are paid for again. Results are recorded **once**, because of the fencing. Recovery takes about one lease TTL (30 s by default).
- **CPU priority.** `docker-compose.yml` gives `postgres`, `api` and `worker` a `cpu_shares` weight of 4096 (four times Docker's default of 1024) so that, on a busy single host, sandbox containers cannot starve heartbeats and cancellations (ADR-008, deployment note). Sandbox containers keep the default weight. Weights only matter under contention.

Settings are listed in [`.env.example`](.env.example) and in ADR-009.

### Observability

Metrics are exported in the Prometheus text format (ADR-010 A). Nothing here changes how runs behave: instrumentation is best-effort and adds no database round trips.

```bash
docker compose --profile observability up -d prometheus   # UI: http://127.0.0.1:9090
curl -s localhost:8000/metrics | head                      # the API, directly
```

- **API:** `GET /metrics`. HTTP requests and latency are labelled by route *template* (`/api/v1/runs/{run_id}`, never the raw path; unmatched paths share one `unmatched` label). `/metrics` is not counted in its own numbers. The system-state gauges are read from Postgres and Redis **on every scrape**; if one source is down, `agenteval_scrape_errors_total{source}` is incremented, that source's gauges disappear, and the scrape still returns 200.
- **Worker:** each worker serves its own endpoint on `AGENTEVAL_WORKER_METRICS_PORT` (default 9464, `0` disables it) from a thread. In compose the port is exposed to the compose network only (Prometheus), never published to the host. The Prometheus UI is bound to `127.0.0.1`. `/metrics` and the worker endpoints are unauthenticated by design (internal network only).
- **Labels are closed sets** (status, outcome, failure reason and category, transition code, tool name, provider and model, stream), never ids; a tool name the model invented is `unknown`, an unpriced model is `other`.

| Family | Type | Labels |
|---|---|---|
| `agenteval_http_requests_total`, `agenteval_http_request_duration_seconds` | counter, histogram | `method`, `route`, `status` (no `status` on the histogram) |
| `agenteval_runs_by_status`, `agenteval_evaluations_by_status` | gauge | `status` (evaluations are the per-attempt grading rows) |
| `agenteval_backlog`, `agenteval_backlog_oldest_age_seconds` | gauge | `kind`: `queued_run`, `retrying_run`, `pending_evaluation` |
| `agenteval_workers_live` | gauge | workers not stopped and heartbeating within 3 lease TTLs |
| `agenteval_stream_length`, `agenteval_stream_pending` | gauge | `stream` |
| `agenteval_scrape_errors_total` | counter | `source`: `postgres`, `redis` |
| `agenteval_auth_failures_total` | counter | `reason`: `missing`, `invalid`, `revoked`, `expired` (401), `forbidden` (403) |
| `agenteval_rate_limited_total`, `agenteval_rate_limit_errors_total` | counter | `kind` (`read`, `write`) for 429s; none for Redis failures (the request was allowed, fail open) |
| `agenteval_runs_finished_total` | counter | `status` (terminal), `outcome` (`none` when cancelled) |
| `agenteval_run_transitions_total` | counter | `code` (ADR-009: R1, R3, R7, R8 ...), `actor` (`worker`, `reconciler`). Retries are R8 and R9; recoveries are the reconciler's R4a, R4b, R5, R9, R10, R11, R12 |
| `agenteval_attempt_failures_total` | counter | `failure_reason`, `category` |
| `agenteval_run_dead_letters_total`, `agenteval_run_cancellations_total` | counter | `reason` |
| `agenteval_run_queue_wait_seconds` | histogram | creation to first claim (a retry's backoff is not queueing) |
| `agenteval_run_duration_seconds` | histogram | `outcome`: one finished attempt |
| `agenteval_evaluation_transitions_total`, `agenteval_evaluation_duration_seconds` | counter, histogram | `code`; `status` |
| `agenteval_model_call_duration_seconds`, `agenteval_model_tokens_total`, `agenteval_model_cost_usd_total`, `agenteval_model_call_errors_total` | histogram, counters | `provider`, `model`; `direction` (`input`, `output`); `kind` |
| `agenteval_tool_calls_total`, `agenteval_tool_call_duration_seconds` | counter, histogram | `tool`, `status` |
| `agenteval_lease_losses_total`, `agenteval_artifact_saves_total`, `agenteval_reconciler_step_errors_total` | counters | `kind`; `result`; `step` |
| `agenteval_worker_inflight`, `agenteval_worker_concurrency` | gauge | `kind` (`run`, which includes its inline grading, and `evaluation`); the configured slots |

Example PromQL:

```promql
# Run success rate over 15 minutes: completed / (completed + failed); cancelled runs are excluded
sum(rate(agenteval_runs_finished_total{status="COMPLETED"}[15m]))
  / sum(rate(agenteval_runs_finished_total{status=~"COMPLETED|FAILED"}[15m]))

# p95 attempt duration, by outcome
histogram_quantile(0.95, sum by (le, outcome) (rate(agenteval_run_duration_seconds_bucket[15m])))

# Backlog now, and how long the oldest queued run has waited
agenteval_backlog{kind="queued_run"}
agenteval_backlog_oldest_age_seconds{kind="queued_run"}

# Retries per minute, and dead letters by reason
sum(rate(agenteval_run_transitions_total{code=~"R8|R9"}[5m])) * 60
sum by (reason) (increase(agenteval_run_dead_letters_total[1h]))

# Spend per hour by model, and worker saturation
sum by (model) (increase(agenteval_model_cost_usd_total[1h]))
sum(agenteval_worker_inflight{kind="run"}) / sum(agenteval_worker_concurrency)
```

Caveats: an age is measured from creation for a queued run (a run requeued after a retry or a recovery keeps its original `created_at`), and from the moment it became due for a retrying run or a pending evaluation. Counters reset when a worker restarts (use `rate`/`increase`), and a run recovered after a crash is counted by the process that applied each transition, so sums across workers are the system totals.

#### Tracing

Traces use OpenTelemetry (ADR-010 B). Spans are **always created** (cheap, in process), so `trace_id` and `span_id` appear on every log line written inside one, and the trace context travels through Redis. **Nothing leaves the process** unless `OTEL_EXPORTER_OTLP_ENDPOINT` (or `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`) is set; `OTEL_TRACES_EXPORTER=none` forces it off. The standard `OTEL_*` variables (service name, sampler, batch tuning, headers) are honoured by the SDK. The services are named `agenteval-api` and `agenteval-worker`.

```bash
make observability-up     # Prometheus + Jaeger; api and worker export traces to Jaeger
# Jaeger UI: http://127.0.0.1:16686   (service: agenteval-api or agenteval-worker)
make observability-down   # removes both, recreates api and worker without export
```

`make observability-up` applies `docker-compose.observability.yml` (which only sets `OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger:4318` on `api` and `worker`) on top of the base file and starts the `observability` profile. An override file, rather than a variable defaulting to empty in the base file, keeps the default stack free of any `OTEL_*` setting: it exports nothing by construction, and `make up` and `make e2e` never depend on a collector. Jaeger keeps traces in memory (gone on restart); its OTLP port is internal to the compose network and the UI is bound to `127.0.0.1` and unauthenticated.

A run submitted through the API is one trace, across the Redis hop (a W3C `traceparent` travels as a field of the stream message):

```text
POST /api/v1/runs                      (server span; named by the route template)
└─ queue.publish                       (producer)
   └─ run.execute                      (consumer, in the worker)
      ├─ run.claim
      ├─ attempt
      │  ├─ agent.step                 (one per model turn)
      │  │  ├─ model.call              (one per provider call, retries included)
      │  │  └─ tool.call               (one per tool call)
      │  ├─ artifact.save
      │  └─ run.finish
      └─ evaluation.grade
         └─ evaluation.check
```

Every reconciler step is its own root span, `reconciler.<step>` (recoveries, retries due, redispatch, sweeps); its `queue.publish` is a child, so a redispatched run's next `run.execute` hangs off that reconciler trace. Steps that found nothing to do are marked `idle` and not exported (there are several per second). A grading delivered on the evaluations stream (after a recovery) is an `evaluation.grade` consumer span joined to its publisher the same way. A message with **no** `traceparent` (published before tracing was configured, or by an older publisher) makes `run.execute` a new root with `trace_origin=new_root`; its `run_id` attribute links it to everything else about the run.

**What is never recorded.** Attributes come from an allow-list in `agenteval_core/observability/tracing.py`: ids (`run_id`, `attempt_no`, `evaluation_id`, `worker_id`), enums (status, outcome, failure reason, transition code, tool name, provider, model), and sizes, durations, token counts and cost. A key outside the list is dropped, so there is nowhere to put prompts, model output, tool inputs or outputs, file contents, hidden checks or error messages from candidate code (D11, ADR-007). A tool name the model invented is recorded as `unknown`, an unpriced model is omitted. An exception is recorded as its **type name only**, never its message or traceback. Spans are best-effort: an exporter failure costs a warning in the log, never a run.

## 12. Security model

This is a **research and portfolio sandbox, not a hardened multi-tenant security boundary.** It runs untrusted code in containers that share the host kernel, and its worker holds the Docker socket, which is root-equivalent on the host. Run it on a dedicated machine, for callers you trust with that machine.

The full review (threat model, trust boundaries, controls per threat, a findings table with fixes and accepted risks, and residual risks) is in **[docs/security-review.md](docs/security-review.md)**. The summary:

- **Sandbox controls** (`apps/worker/agenteval_worker/sandbox/session.py`):
  - no network (`network_mode=none`)
  - memory limit with no swap, CPU quota, `pids_limit=128`
  - read-only rootfs, with size-capped tmpfs at `/workspace` and `/tmp`, mounted `noexec,nosuid,nodev`
  - uid 1000, all capabilities dropped, `no-new-privileges`, Docker's default seccomp profile, tini as PID 1
  - wall-clock limits, output caps, and the container is always removed
- **Agent tools** add a per-call timeout, the post-call reaper, argv-only execution and an allowlist for `shell`.
  - **The allowlist and path checks are policy, not the isolation boundary.** The container is the boundary: an agent allowed `sh` can do anything the container permits.
- **Prompt injection through task files and tool output is expected.** Its reach is limited to the version's tools inside the container, and a runaway agent is stopped by its step, token, cost and time limits.
- **Model credentials** live only in the worker's environment. They are never passed into sandbox containers (a test inspects a live one), never in prompts, and the API image doesn't install the SDK. Structured logs redact `sk-ant-…` values, `ae_…` API keys, `Bearer …` text and credential-named fields (`api_key`, `authorization`, `x-api-key`, …).
- **Image allowlist:** tasks may only use images listed in `AGENTEVAL_IMAGE_ALLOWLIST`. The API checks it at task creation and the worker checks it again before creating any container. The worker never pulls images.
- **The worker mounts `/var/run/docker.sock`, which is host-root equivalent.**
  - The worker is therefore a trusted component and runs as root in its container (with all Linux capabilities dropped in production).
  - The API has no socket access, runs as a non-root user, and its image installs neither the Docker SDK nor the Anthropic SDK.
- **Sandboxes share the host kernel,** so a kernel exploit is not mitigated beyond Docker defaults. gVisor or Firecracker would be the next step.
- **Evaluation container** (ADR-006): the same controls, plus `noexec,nosuid,nodev` on every writable mount, and hidden files in a root-owned, read-only `/eval`. Snapshots are validated header by header on the host and are only ever unpacked inside a sandbox.
  - **Residual risk:** candidate code runs inside the test process, so it can read `/eval` while it's being graded, and could cheat in-process (C7). `io` checks are the robust choice when integrity matters most.
- **Evaluation secrecy** (ADR-007): hidden files, check names, expected values and evaluator configuration never appear in API responses or model-visible data. Specs are write-only, every response goes through one public projection, and 422s are sanitised.
  - Evaluation log events carry only ids, statuses and categories. The one exception is an *unexpected* evaluator exception, which is logged with its traceback (operator-only).
- **Authentication and authorization (ADR-012 A, B).** Every `/api/v1` request needs an API key (`ae_<prefix>_<secret>`, 256 bits of randomness; only its SHA-256 is stored, compared in constant time). Keys have `read`, `write` and `admin` scopes, can expire and can be revoked; a test fails if any route lacks a scope. The first key is minted by an operator CLI inside the API container (no anonymous endpoint). `/health`, `/ready` and `/metrics` are open: `/metrics` is meant for the internal network only (the production proxy answers it 404). The platform is single-tenant (scopes, not per-object ownership).
- **Rate limiting (ADR-012 C).** A per-key, per-minute budget for reads (600) and writes (120) in Redis, 429 with `Retry-After`. It **fails open** if Redis is down (logged and counted), because Redis is never the authority.
- **Input limits.** Request bodies are capped (`AGENTEVAL_MAX_REQUEST_BYTES`, 5 MiB, `413`), tasks and evaluation specs have their own size and count limits, 422 responses never echo the submitted input, and a value Postgres cannot store is a fixed-text 422, not a 500.
- **Production hardening.** Only Caddy is published (TLS, HSTS, `X-Frame-Options`, a framing-and-injection CSP, a 5 MB body cap); Postgres and Redis need passwords and are not published; api, worker, web and Caddy drop all capabilities and set `no-new-privileges`, and the API has a read-only root filesystem. The dashboard sets the same headers when served directly.
- **Dependencies** are audited in CI (`pip-audit` on the locked set, `npm audit` on the production set); the last run found none.
- **Limits of this model.** Keys are bearer credentials: anyone holding one has its scopes, so use TLS (the dashboard and API are plain HTTP on localhost by default) and keep keys out of code. There is no per-user audit beyond the key name, no key rotation automation, and the dashboard keeps its key in `sessionStorage`, which any script running on the dashboard's origin can read. `AGENTEVAL_AUTH_ENABLED=false` removes all of this and is for local experiments only. Compose still binds every development port to `127.0.0.1`.

## 13. Testing strategy

| Layer | Where | What it covers | Needs |
| --- | --- | --- | --- |
| Unit | `packages/core/tests`, `apps/*/tests`, `benchmarks/tests` | state machines, failure taxonomy, retry policy and backoff, lease timing, scoring and statistics, evaluators and parsers, the agent loop against a scripted provider, metrics, key format and redaction, middleware | nothing |
| Integration | `tests/integration` | the real API, Redis, Postgres (testcontainers, real migrations) and, where marked, Docker: every transition and constraint, fencing, locks, each reconciler step, the whole auth and scope matrix, the read API, migrations | Docker |
| Sandbox (real Docker) | `apps/worker/tests` | the container controls from the inside, the tools in a real sandbox, evaluation attacks and secrecy | Docker, `make sandbox-image` |
| End-to-end | `tests/e2e` | the running compose stack over real HTTP: submit, execute, evaluate, read back | `make up && make migrate` |
| Failure | spread across the layers | worker crash (SIGKILL mid-run and mid-grading), timeouts, retries, duplicate dispatch, invalid tasks, model API failure, evaluator failure, cancellation, dead letter and replay | as above |
| Dashboard | `apps/web/tests` | pages render from fixtures, the key screen, error states, response headers | `make web-check` |

**CI** (`.github/workflows/ci.yml`) runs the Docker-free layers and the full integration run on every push and pull request; the end-to-end suite runs on `main` and nightly. A test is meaningful only if it can fail: the timing-sensitive ones wait for the condition they assert (below) instead of racing it, and the security tests inspect live containers rather than the code that configures them.

The detail, by area:

```bash
make test        # unit + integration + real-Docker tests (no network, no API key)
make test-unit   # no Docker needed
make e2e         # needs `make up && make migrate`; recreates the worker with short lease timings
make lint        # ruff + mypy
```

**Continuous integration** (`.github/workflows/`): `ci.yml` runs on every push and pull request with these jobs: `lint` (ruff, format check, mypy including `benchmarks`), `unit` (no Docker: core tests, the bounded-buffer test and the benchmark-harness tests), `integration` (the full `pytest` run on a runner with Docker, after building the sandbox image), `web` (`npm ci`, typecheck, lint, test, build), `audit` (`pip-audit` on the locked Python dependencies and `npm audit --omit=dev --audit-level=high`) and `images` (builds the api, worker, web and sandbox images without pushing). `e2e.yml` runs the compose-stack suite (`make up && make migrate && make e2e`) on pushes to `main`, nightly and on demand. `make ci-local` runs the Docker-free checks locally.

The standard suite uses only the fake provider and makes no real model calls. Its one `live` test is skipped unless explicitly enabled.

**Phase 3 tests:**

- **Runtime unit tests** (fake provider, scripted workspace): every way a run can end, retries and `retry-after`, deadline vs. provider fault, cancellation during a model call, a backoff or a tool call, event order and save batching, budgets, truncation handling.
- **Sandbox session tests** (real Docker): every §15 spike case, including background children and `setsid` escapees, before and after the reaper; the pids limit with and without the reaper; the backstop; reaper failure.
- **Tool tests:** unit tests against a fake workspace, and integration tests in a real sandbox, including the fix-the-bug loop, an injection attempt, timeouts and state isolation.
- **Anthropic provider tests:** a stubbed client with real SDK types and exceptions (request shape per model, strict-compatible schemas, verbatim thinking replay, 16 error mappings), plus a full agent run through the provider.
- **Integration tests** (real Postgres, Redis and Docker): agent runs through the real executor, each outcome persisted, writer fencing, idempotency and retries, ownership lost mid-run, cancellation at the shutdown deadline, a command task alongside them.
- **API and migration tests:** agents and versions (idempotency, concurrency, every validation rule), run pairing rules, trajectory paging; the migrated schema matches the models exactly, S1 data survives `0002`, and the downgrade refuses while agent data exists.
- **E2E:** agent fix-the-bug and max-steps runs over real HTTP, alongside the S1 command-task E2E.

**Phase 4 tests** (details in [docs/architecture/evaluation.md](docs/architecture/evaluation.md#6-verification)):

- **Core:** unit and property tests for the grading domain.
- **Sandbox security suite** (real Docker): archive traversal, hidden-file isolation, no network, a read-only `/eval`, reaping, snapshot completeness, and the C2 grading attacks.
- **Runner integration tests:** every evaluation outcome, evaluator failures, duplicate evaluations, and the invariant that evaluation never changes the attempt.
- **API tests:** datasets, idempotent batches, results, summary and compare, plus the D11 response-model snapshot and route sweep.
- **E2E benchmark scenarios** (S1–S9) on the running stack.

**Phase 5 tests:**

- **Unit tests:** the state-machine tables, failure taxonomy and its CHECK lists, retry policy, backoff, lease timing and the keeper protocol.
- **Integration tests** (real Postgres, Redis and Docker): every transition and constraint, fencing of stale owners, lock behaviour (recovery never waits), each reconciler step, a worker killed mid-run and recovered by another, artifact save and sweep races, evaluation recovery, and the cancel, replay, run-listing and budget APIs.
- **E2E reliability scenarios** (`tests/e2e/test_reliability_e2e.py`) on the compose stack: worker SIGKILL mid-run and mid-grading, a transient model failure retried with backoff, run and batch cancellation, dead letter and replay, and duplicate dispatch.

**Phase 6 tests (metrics):**

- **Unit tests:** every metric moves on its event, labels collapse into closed sets, a broken registry never raises into the caller, and no label value looks like an id.
- **API tests:** `/metrics` exposition, route-template labels, `unmatched`, the endpoint excluded from its own numbers, and a 200 with both sources down.
- **Integration tests** (real Postgres, Redis and Docker): the scrape-time gauges against seeded rows and streams, a broken Redis or Postgres counted without failing the scrape, and the executor, reconciler and evaluation runner moving the run, model, tool and evaluation metrics.

**Phase 8 tests (authentication, scopes, rate limits):**

- **Unit tests:** key format and parsing, constant-time digest comparison, scope parsing, and log redaction of `ae_...` keys, bearer text and `Authorization` headers.
- **Integration tests** (real Postgres and Redis): the key lifecycle (create returns the plaintext once, only a digest is stored, revoke, expiry, `last_used_at` written at most once a minute), the CLI, migration `0010` (constraints, a downgrade that refuses while keys exist), a 401/403 matrix over **every** `/api/v1` route found in the app, a sweep that fails when a route has no scope dependency (or the OpenAPI schema lacks its security requirement), the key endpoints, authentication disabled, and the rate limiter (429 and headers, separate read and write budgets, per-key counters, window reset, atomicity under concurrency, Redis down allows the request and counts it).
- **Fixtures:** the integration `client` sends a fresh admin key; the e2e helpers and `benchmarks/run.py` use `AGENTEVAL_E2E_API_KEY` / `AGENTEVAL_BENCH_API_KEY`, or create a short-lived key through the CLI in the `api` container.
- **E2E:** `tests/e2e/test_auth_e2e.py` shows the running stack refuses keyless requests, a `read` key cannot write, and revocation takes effect at once.
- **Dashboard:** key storage, the bearer header, 401 returning to the key screen, Sign out, and the 403 message on cancel and replay.

**Security review tests** ([docs/security-review.md](docs/security-review.md)):

- **Sandbox, from the inside (real Docker):** `NoNewPrivs` and seccomp filter mode are on, no worker environment variable (including the model key and database URL) is visible even in PID 1's environment, no Docker socket, no name resolution for `postgres`, `redis` or `api`, every writable mount is `noexec,nosuid,nodev`, and a worker refuses an image outside its allowlist.
- **Hostile input:** an oversize body is a 413 before it is parsed (declared, chunked and through the real app, and counted in the metrics); a NUL character is a fixed-text 422 and the API keeps working; the dashboard's security headers are pinned.

**E2E configuration:**
- **API key.** The suite sends `AGENTEVAL_E2E_API_KEY` if set; otherwise it creates one per session with `docker compose exec -T api python -m agenteval_api.keys create` (admin scopes, expires in a day), so `make e2e` stays self-contained.
- **Short lease timings.** `make e2e` runs `docker compose -f docker-compose.yml -f docker-compose.e2e.yml up -d --no-deps --wait worker` first, so the worker uses a 15 s lease, 2 s heartbeat and 1 s reconcile tick (`docker-compose.e2e.yml`, values that pass startup validation). The crash scenarios then recover in seconds, and the older scenarios behave the same. Those timings are only for this suite: run `make up` (or `docker compose up -d worker`) afterwards to go back to the 30 s defaults. The crash scenarios call `docker compose kill -s SIGKILL worker` and `docker compose up -d worker` with both files, so the timings survive the restart.
- **Redis.** The duplicate-dispatch scenario also needs Redis, read from `AGENTEVAL_E2E_REDIS_URL` (default `redis://127.0.0.1:56379/0`).
- The E2E suite reads `AGENTEVAL_E2E_URL` (default `http://localhost:8000`).
- The invariant scenario also reads `AGENTEVAL_E2E_DATABASE_URL` (default `postgresql://agenteval:agenteval@127.0.0.1:55432/agenteval`), through a **SELECT-only** probe. It is skipped if the database is unreachable.
- A full `make e2e` takes a few minutes and needs a Docker host that isn't starved of memory.

**Timing-sensitive tests, and how they are made deterministic:** three tests that used to fail intermittently now wait for the condition they assert instead of racing it.
- `test_backstop_kills_the_container_if_timeout_never_fires` waits for the daemon to report the killed container as not running (`docker kill` only delivers the signal).
- `test_memory_limit_oom` is fixed in the code: after an exec exits 137 the sandbox waits (up to 3 s) for Docker to record `OOMKilled`, so a real OOM kill is no longer reported as `oom_killed=false`.
- `test_drain_deadline_cancels_agent_runs_and_records_why` starts its drain only once the agent is really in its first model call (the `run_started` event), not as soon as the attempt row exists while the container is still being created.

The S1 tests from the slice still run unchanged: state machine, idempotency, claim races, fencing, sandbox controls, the command-task E2E.

## 14. Benchmarks

The platform itself is benchmarked with a reproducible harness (`benchmarks/`, method in [ADR-011](docs/decisions/ADR-011-benchmarking.md)). Every number below is copied from the generated report, [docs/benchmarks/README.md](docs/benchmarks/README.md), which has the full method, latency percentiles, CPU and memory tables, and the limitations. Reproduce with `make bench-smoke` (seconds) or `make bench` (about an hour; it **wipes the stack's data**, see [benchmarks/README.md](benchmarks/README.md)).

Throughput, 2 workers x 2 slots, 3 repeats per cell (median, with the min-max range). `noop-command` runs `true` in a sandbox, so it measures dispatch, claim, container create/start/remove and persistence:

| Runs | Repeats | Throughput (runs/s) | Success rate | Retry rate | Submit phase (s, client) |
|---|---|---|---|---|---|
| 10 | 3 | 3.25 (3.22-3.33) | 1.000 (1.000-1.000) | 0.000 (0.000-0.000) | 0.17 (0.16-0.18) |
| 100 | 3 | 3.84 (2.46-4.27) | 1.000 (1.000-1.000) | 0.000 (0.000-0.000) | 1.30 (1.06-1.62) |
| 500 | 3 | 4.09 (3.71-4.11) | 1.000 (1.000-1.000) | 0.000 (0.000-0.000) | 6.62 (4.25-7.18) |
| 1000 | 3 | 3.98 (3.97-4.01) | 1.000 (1.000-1.000) | 0.000 (0.000-0.000) | 10.77 (10.06-13.01) |

`fake-agent` adds the scripted agent loop, trajectory writes and grading:

| Runs | Repeats | Throughput (runs/s) | Throughput incl. grading (runs/s) | Success rate | Retry rate | Submit phase (s, client) |
|---|---|---|---|---|---|---|
| 10 | 3 | 3.66 (3.02-3.70) | 3.63 (3.00-3.67) | 1.000 (1.000-1.000) | 0.000 (0.000-0.000) | 0.17 (0.16-0.22) |
| 100 | 3 | 2.87 (2.74-3.05) | 2.86 (2.74-3.05) | 1.000 (1.000-1.000) | 0.000 (0.000-0.000) | 1.47 (1.12-2.58) |
| 500 | 3 | 2.99 (2.95-3.04) | 2.99 (2.95-3.04) | 1.000 (1.000-1.000) | 0.000 (0.000-0.000) | 5.10 (5.09-5.57) |
| 1000 | 3 | 3.00 (2.91-3.06) | 3.00 (2.91-3.06) | 1.000 (1.000-1.000) | 0.000 (0.000-0.000) | 12.72 (11.81-13.07) |

Worker scaling, `noop-command`, 200 runs, 2 slots per worker:

| Workers | Throughput (runs/s) | Speedup | Efficiency | Exec p50 (s) |
|---|---|---|---|---|
| 1 | 3.11 | 1.00 | 100% | 0.685 |
| 2 | 4.03 | 1.30 | 65% | 1.046 |
| 5 | 4.25 | 1.37 | 27% | 2.321 |
| 10 | 4.90 | 1.58 | 16% | 3.722 |
| 25 | 4.41 | 1.42 | 6% | 10.988 |

How to read this honestly (the report's own caveats, abridged): one developer laptop, shared with unrelated containers, so the absolute numbers are indicative and the shape is the point; every run succeeded with no retries; submission is a burst, so queue latency is mostly the backlog; throughput flattens at about 4 to 5 runs per second because each sandbox takes longer the more of them run at once, and the data is consistent with, but does not prove, the Docker daemon being the limit; sandbox CPU and memory could not be measured. Nothing here is a claim about other hardware.

## 15. Local setup

Prerequisites: [uv](https://docs.astral.sh/uv/), and Docker with Compose v2 (Docker Desktop on macOS works).

```bash
cp .env.example .env     # optional; defaults work
make sync                # uv sync --all-packages (Python 3.12 is pinned; uv fetches it)
make up                  # builds the sandbox image, starts postgres, redis, api, worker
make migrate             # alembic upgrade head (inside the api container)
# Authentication is ON: create a key once (it is shown once; only its hash is stored)
docker compose exec api python -m agenteval_api.keys create --name me --scopes read,write,admin
export AGENTEVAL_API_KEY=ae_...        # paste the key it printed
make e2e                 # full-stack tests over real HTTP (command tasks, agent tasks, Phase 4 benchmark scenarios)
make logs / make down
```

- **Every `/api/v1` call needs the key** (`Authorization: Bearer <key>`, shown in the curl examples below as `-H "$AUTH"`), except `/health`, `/ready` and `/metrics`. `make e2e` and `make bench-smoke` use `AGENTEVAL_E2E_API_KEY` / `AGENTEVAL_BENCH_API_KEY` if set and otherwise create a short-lived key themselves with the command above. `AGENTEVAL_AUTH_ENABLED=false` switches checks off for local experiments (see [docs/api/auth.md](docs/api/auth.md)).
- Host ports default to **55432** (Postgres), **56379** (Redis) and **8000** (API). Postgres and Redis avoid the standard ports so they don't clash with local instances; all three can be changed in `.env`.
- The API reloads on code changes, and so does the worker, via `watchfiles` (in-flight runs drain first).
- Scale out with `docker compose up -d --scale worker=3`.

For a guided tour with data in the dashboard, run the [demo](#18-demo) (`scripts/demo.sh`).

### Try it: a command task

```bash
AUTH="Authorization: Bearer $AGENTEVAL_API_KEY"
curl -s -H "$AUTH" -X POST localhost:8000/api/v1/tasks -H 'content-type: application/json' -d '{
  "name": "hello", "image": "agenteval-sandbox:py312",
  "command": ["python", "main.py"],
  "files": {"main.py": "print(\"hi\")\nraise SystemExit(3)"},
  "timeout_s": 10
}'
# -> {"id": "<task_id>", ...}

curl -s -H "$AUTH" -X POST localhost:8000/api/v1/runs -H 'content-type: application/json' \
     -H 'Idempotency-Key: demo-1' -d '{"task_id": "<task_id>"}'
# -> 202 {"id": "<run_id>", "status": "QUEUED", ...}

curl -s -H "$AUTH" localhost:8000/api/v1/runs/<run_id>            # status COMPLETED, outcome NON_ZERO_EXIT
curl -s -H "$AUTH" localhost:8000/api/v1/runs/<run_id>/attempts   # exit_code 3, stdout "hi\n"
```

### Try it: an agent task (scripted fake model; offline, free)

```bash
AUTH="Authorization: Bearer $AGENTEVAL_API_KEY"
API=localhost:8000/api/v1
curl -s -H "$AUTH" -X POST $API/agents -H 'content-type: application/json' -d '{"name": "fixer"}'
# -> {"id": "<agent_id>", ...}

curl -s -H "$AUTH" -X POST $API/agents/<agent_id>/versions -H 'content-type: application/json' -d '{
  "model_provider": "fake", "model": "fake-model",
  "system_prompt": "You fix bugs.",
  "tools": ["read_file", "write_file", "run_tests"],
  "limits": {"max_steps": 10},
  "model_settings": {"script": [
    {"type": "turn", "tool_calls": [{"name": "run_tests"}]},
    {"type": "turn", "tool_calls": [{"name": "write_file",
       "input": {"path": "calc.py", "content": "def add(a, b):\n    return a + b\n"}}]},
    {"type": "turn", "tool_calls": [{"name": "run_tests"}]},
    {"type": "turn", "text": "Fixed add().", "expect_tool_results_contain": ["Tests passed."]}
  ]}
}'
# -> 201 {"id": "<version_id>", "version": 1, "config_hash": "...", ...}

curl -s -H "$AUTH" -X POST $API/tasks -H 'content-type: application/json' -d '{
  "name": "fix-add", "task_type": "agent", "image": "agenteval-sandbox:py312",
  "instructions": "The tests fail. Fix calc.py so they pass.",
  "test_command": ["python", "-m", "unittest", "-q"],
  "files": {"calc.py": "def add(a, b):\n    return a - b\n",
            "test_calc.py": "import unittest\nfrom calc import add\nclass T(unittest.TestCase):\n    def test(self):\n        self.assertEqual(add(2, 3), 5)\n"},
  "timeout_s": 120
}'
curl -s -H "$AUTH" -X POST $API/runs -H 'content-type: application/json' \
     -d '{"task_id": "<task_id>", "agent_version_id": "<version_id>"}'

curl -s -H "$AUTH" $API/runs/<run_id>                        # COMPLETED / AGENT_FINISHED
curl -s -H "$AUTH" $API/runs/<run_id>/attempts               # steps 4, tokens_in 400, tokens_out 80, cost_usd "0.000800"
curl -s -H "$AUTH" "$API/runs/<run_id>/attempts/1/events?limit=100"   # the trajectory, in seq order
```

### Dashboard

A Next.js dashboard (`apps/web`) reads the public REST API: KPIs and charts, agents and their versions, tasks, evaluations (detail and A vs B vs C comparison), runs (with the dead-letter view, trajectory, cancel and replay) and workers. It has no database access. It asks for an API key on first use (below).

```bash
make up && make migrate     # the API on http://localhost:8000
make web-up                 # the dashboard container on http://localhost:3000 (compose profile "web")
make web                    # or: a dev server with hot reload on :3000 (npm ci + npm run dev)
make web-check              # typecheck, lint, tests and production build
```

The dashboard service is opt-in (profile `web`), so `make up` and the e2e runs never pay for a Node build. With compose it is served on `127.0.0.1:${AGENTEVAL_WEB_HOST_PORT:-3000}`. The browser calls the API directly, so `NEXT_PUBLIC_API_URL` (build arg `AGENTEVAL_WEB_API_URL`, default `http://localhost:8000`) is baked into the bundle at build time, and the API's CORS list (`AGENTEVAL_CORS_ORIGINS`) must contain the dashboard's origin. **API key (ADR-012 D).** On first use the dashboard shows an "API key" screen. The key is kept in the tab's `sessionStorage` (gone when the tab closes), sent only as `Authorization: Bearer`, and forgotten by **Sign out** in the sidebar. A 401 (invalid, revoked or expired key) returns you to the key screen with a message; a 403 on cancel or replay says the key lacks the `write` scope (a `read` key can browse). Create a key with `docker compose exec api python -m agenteval_api.keys create --name dashboard --scopes read,write`. If the API runs with `AGENTEVAL_AUTH_ENABLED=false` enter any value. Keep both ports bound to localhost unless you put a TLS proxy in front. Details: [apps/web/README.md](apps/web/README.md).

### Deployment

`docker-compose.prod.yml` (used on top of `docker-compose.yml`) runs the stack for a single dedicated host: images by tag, no source mounts or reload, `restart: unless-stopped`, resource limits, secrets from an env file outside the repository (`.env.prod.example`), and a Caddy reverse proxy with automatic TLS as the **only** published service (ports 80 and 443). Postgres, Redis and the metrics ports stay on the internal network.

```bash
make prod-config     # sanity-check the production compose files (throwaway secrets, starts nothing)
```

First deploy, upgrades, backups and restore, rollback, the Docker-socket caveat and sizing notes: [docs/deployment.md](docs/deployment.md). Images are published to GHCR on version tags by `.github/workflows/release.yml`.

## 16. API documentation

The OpenAPI docs are at <http://localhost:8000/docs>. Errors are RFC 7807-style `application/problem+json`. **Every `/api/v1` route needs an API key** with the route's scope: `read` for `GET`, `write` for creating, cancelling, replaying and freezing, `admin` for `/keys`. Missing, invalid, revoked or expired keys get 401 (`WWW-Authenticate: Bearer`), a key without the scope gets 403, and a key over its per-minute budget gets 429 with `Retry-After` and `X-RateLimit-*`. See [docs/api/auth.md](docs/api/auth.md).

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/health`, `/ready` | open (no key). Liveness; readiness checks Postgres and Redis (503 if either is down). `/metrics` is open too |
| POST, GET | `/api/v1/keys` | `admin`. Create a key (201; the full `key` is returned once) / list keys (never secrets) |
| DELETE | `/api/v1/keys/{prefix}` | `admin`. Revoke; 204, idempotent; 404 unknown |
| POST | `/api/v1/agents` | 201; 409 if the name is taken |
| GET | `/api/v1/agents` | newest first, keyset-paged (`after`, `limit` 1–200); each agent has its `latest_version` (number, provider, model, tools); `{agents, next_after}` |
| GET | `/api/v1/agents/{id}` | |
| POST | `/api/v1/agents/{id}/versions` | 201 new version; **200** when an equivalent config already exists; 422 if it can't run |
| GET | `/api/v1/agents/{id}/versions`, `…/versions/{n}` | versions are immutable |
| POST | `/api/v1/tasks` | `task_type` `command` (default) or `agent`. 201; 422 on validation or an image not on the allowlist |
| GET | `/api/v1/tasks` | newest first, keyset-paged; filters `dataset_id`, `task_type`, `difficulty`; public fields only (`has_evaluation`, never the spec); `{tasks, next_after}` |
| GET | `/api/v1/tasks/{id}` | |
| POST | `/api/v1/runs` | body `{task_id, agent_version_id?, max_attempts?}` (budget 1–10, default `AGENTEVAL_RUN_MAX_ATTEMPTS` = 3, pinned on the run), optional `Idempotency-Key` header |
| GET | `/api/v1/runs` | newest first; filters `status`, `dead_letter` (the DLQ view), `evaluation_id`, `task_id`; keyset-paged by an opaque `after` cursor, `limit` 1–200; `{runs, next_after}` |
| GET | `/api/v1/runs/{id}` | includes `max_attempts`, `replay_count`, `dead_letter_reason`, `failure_reason`, `next_attempt_at` |
| POST | `/api/v1/runs/{id}/cancel` | 202 accepted (queued/retrying: cancelled now; claimed/running: flagged, the worker stops it) / 200 repeat / 409 already finished / 404 |
| POST | `/api/v1/runs/{id}/replay` | give a FAILED run `additional_attempts` (1–9, default 1; total cap 10) more attempts and queue it again; optional `Idempotency-Key`; 202 replayed / 200 repeat of the key / 409 not FAILED or key reused / 422 over the cap / 404 |
| GET | `/api/v1/runs/{id}/attempts` | exit code and output (command tasks); steps, tokens, cost and final answer (agent tasks) |
| GET | `/api/v1/runs/{id}/attempts/{n}/events` | the trajectory, paged by `after_seq` (default -1) and `limit` (1–1000, default 100); `next_after_seq` is null on the last page |
| GET | `/api/v1/runs/{id}/attempts/{n}/evaluation` | the attempt's **public** evaluation; 404 if none is recorded |
| POST | `/api/v1/datasets` | 201 DRAFT dataset; 409 if `(name, version)` exists |
| GET | `/api/v1/datasets` | newest first, keyset-paged; filter `status`; `task_count` per dataset; `{datasets, next_after}` |
| GET | `/api/v1/datasets/{id}` | includes `task_ids` |
| POST | `/api/v1/datasets/{id}/freeze` | DRAFT → FROZEN (idempotent); 409 if it has no tasks |
| POST | `/api/v1/evaluations` | a benchmark evaluation: agent version × frozen dataset × repetitions, with optional budgets `run_max_attempts` / `eval_max_attempts` (1–10, default 3); optional `Idempotency-Key`; 202 new / 200 replay / 409 / 503 |
| GET | `/api/v1/evaluations` | newest first, keyset-paged; filters `agent_version_id`, `dataset_id`; each batch with its progress; `{evaluations, next_after}` |
| GET | `/api/v1/evaluations/{id}` | the batch, scoring config and hash, derived progress |
| POST | `/api/v1/evaluations/{id}/cancel` | cancel the batch and its unfinished runs; finished runs and scores are untouched; 202 / 200 repeat / 404 |
| GET | `/api/v1/evaluations/{id}/results` | public per-run results, keyset-paged |
| GET | `/api/v1/evaluations/{id}/summary` | success rate + CI, failure categories, score means, latency, cost, per task |
| GET | `/api/v1/evaluations/compare?ids=a,b` | 2–5 batches on the same dataset and scoring config (else 422) |
| GET | `/api/v1/workers` | live workers first, keyset-paged; each has an `alive` flag (not stopped, heartbeat within 3 × lease TTL); `{workers, next_after}` |
| GET | `/api/v1/stats/overview` | `window_h` 1–720 (default 24): evaluation and run counts, success rate, run latency p50/p95, cost and tokens, active workers, queue depth |

The dashboard endpoints (the lists and `/stats/overview`) are defined in [docs/api/read.md](docs/api/read.md). CORS allows only the origins in `AGENTEVAL_CORS_ORIGINS` (default `http://localhost:3000`), for `GET` and `POST`, with the `Authorization`, `Content-Type` and `Idempotency-Key` request headers (no cookies).

The full reference for the Phase 4 endpoints is [docs/api/evaluations.md](docs/api/evaluations.md). `POST /tasks` also accepts `dataset_id`, `difficulty` and a **write-only** `evaluation` spec: responses only say `has_evaluation`. `RunRead` gains `evaluation_id` and `repetition`, and attempts gain `sandbox_image_id`.

**Request bodies are capped** at `AGENTEVAL_MAX_REQUEST_BYTES` (default 5 MiB): a larger body gets `413` before it is parsed. **A value Postgres cannot store** (for example a NUL character) is a `422` with fixed text.

**Validation errors (422) never echo the submitted input.** Errors inside an evaluation spec get a fixed message and a truncated location, so no hidden content comes back (ADR-007).

**`POST /api/v1/runs` responses:**

- **202:** a new run was created.
- **200:** a replay of an existing `Idempotency-Key`.
- **409:** the key was already used for a different task, agent version or `max_attempts`.
- **404:** unknown task or agent version.
- **422:** an agent task without an `agent_version_id`, or a command task with one.
- **503:** the run was persisted but dispatch failed. Retry with the same key and the run is re-dispatched.

**Task limits (validated):**

- `timeout_s` 1–300 for command tasks and 1–1800 for agent tasks; `memory_mb` 64–1024; `cpu_limit` 0.1–2.0.
- `command` and `test_command` take up to 64 args.
- `files` is `{relative_path: content}`, with no absolute paths or `..`, at most 100 files and at most 1 MB in total.
- Agent tasks need `instructions` and have no `command`.

**Agent version validation** (all at creation, so a bad config never reaches a worker):

- Tools must be from the built-in six.
- `shell` requires a non-empty `shell_allowlist` of bare program names, and an allowlist requires `shell`.
- The provider must be enabled and the model priced.
- The provider settings must be valid: a well-formed script for `fake`; for `anthropic`, the effort level, stored explicitly and defaulting to `high`.
- `limits` must be within the global caps (`AGENTEVAL_AGENT_MAX_STEPS_CAP` 100, `…_MAX_TOTAL_TOKENS_CAP` 2,000,000, `…_MAX_COST_USD_CAP` 5.00). Over-cap limits are **rejected, never clamped**, so a version runs with exactly its stored config.

**Agent limits** (per version; defaults in brackets): `max_steps` [30], `max_total_tokens` [500,000], `max_cost_usd` [1.00], `max_tokens_per_call` [16,000], `tool_timeout_s` [60], `model_call_timeout_s` [300].

More detail: [docs/api/auth.md](docs/api/auth.md) (keys, scopes, rate limits), [docs/api/evaluations.md](docs/api/evaluations.md) (datasets, evaluations, results, summaries, compare), [docs/api/read.md](docs/api/read.md) (the dashboard's list endpoints and `/stats/overview`), and the interactive OpenAPI docs at `/docs` on a running stack.

## 17. Screenshots

Captured from the running dashboard, with the data the [demo](#18-demo) creates (three scripted agents on one dataset), by `scripts/capture-screenshots.mjs`.

**Overview:** KPIs for the selected window and the run counts by status.

![Dashboard overview](docs/images/overview.png)

**Evaluation detail:** success rate with its confidence interval, progress, grading status and failure categories for one agent.

![Evaluation detail](docs/images/evaluation.png)

**Compare:** three agents on the same dataset, with deltas against the first column and per-task success.

![Comparing three evaluations](docs/images/compare.png)

**Run detail:** the run, its attempt, the public evaluation view and the full trajectory (tool calls with status, duration, tokens and cost).

![Run detail with trajectory](docs/images/run.png)

## 18. Demo

`scripts/demo.sh` walks through the core loop against a running stack: three scripted agents, one dataset of two tasks with hidden checks, one evaluation per agent, then summaries and a comparison. It uses the fake model provider, so it is offline, free and deterministic. It needs `curl` and `jq`.

```bash
make up && make migrate
docker compose exec -T api python -m agenteval_api.keys create --name demo --scopes read,write,admin
export AGENTEVAL_API_KEY=ae_...          # the key it printed (shown once)
./scripts/demo.sh                        # a few seconds on a warm stack
```

What it does, step by step:

1. **Create three agents** (each one agent plus one immutable version): `fixer` edits `calc.py` and runs the tests, `sloppy` edits it wrongly and never checks, `quitter` gives up.
2. **Create a dataset** with two agent tasks. One is graded by a hidden test file plus a protected-path (tamper) check; the other by a final-answer check. The dataset is then **frozen**, so its tasks can't change under a benchmark.
3. **Evaluate every agent** on the frozen dataset (the fixer with 2 repetitions) and wait for every run and grading to finish.
4. **Read the summaries and compare.** The scripted agents are deterministic, so the output is the same on every run (table below).
5. The script ends by printing dashboard links. With the dashboard running (`make web-up`, then open <http://localhost:3000> and paste the key), the evaluation and compare pages show the same numbers.

The summaries it prints (real output, trimmed):

| Agent | Graded | Passed | Success rate | 95% CI | Failure categories | Mean score |
| --- | --- | --- | --- | --- | --- | --- |
| fixer | 4 | 4 | 100% | 51% to 100% | `passed: 4` | 0.985 |
| sloppy | 2 | 1 | 50% | 9.5% to 90.5% | `passed: 1`, `tests_failed: 1` | 0.545 |
| quitter | 2 | 0 | 0% | 0% to 65.8% | `tests_failed: 1`, `wrong_answer: 1` | 0.100 |

The fixer's confidence interval is wide because four graded attempts say little, which is why the interval is reported. The sloppy agent passes the final-answer task by *claiming* success and fails the hidden test, which is exactly why the hidden test exists. The per-result rows show only the public projection: status, category, tags, per-position check results, score components and metrics; hidden test names and expected values never appear.

To regenerate the [screenshots](#17-screenshots) from that data:

```bash
EV=$(./scripts/demo.sh | sed -n 's/^EVALUATIONS=//p')
AGENTEVAL_EVALUATIONS=$EV node scripts/capture-screenshots.mjs     # needs chromium and Node 22+
```

The script exits non-zero if the scripted agents don't score as designed (fixer 4 of 4, quitter 0 of 2), so it doubles as a smoke test of a deployment.

## 19. Engineering tradeoffs

The reasoning behind each major choice is an Architecture Decision Record; the index is [docs/decisions/README.md](docs/decisions/README.md). The short version:

| Decision | Chosen | Instead of | Cost accepted |
| --- | --- | --- | --- |
| Source of truth | Postgres, guarded `UPDATE`s, CHECK constraints (ADR-002) | Redis state, an ORM unit of work | more SQL; correctness is testable in one place |
| Dispatch | Redis Streams, reconciler republishes (ADR-001) | Kafka, Celery, a plain list | at-least-once delivery, handled by the conditional claim |
| Isolation | one Docker container per attempt (ADR-003) | in-process execution, a VM per task | shared kernel; the worker needs the Docker socket |
| Process model | modular monolith, API and worker from one package (ADR-004) | microservices | one deploy unit, one codebase to reason about |
| Agent loop | hand-written, provider-neutral (ADR-005) | an agent framework | more code to own, fully understood |
| Evaluation | separate record, fresh hardened container, hidden specs write-only (ADR-006, ADR-007) | grading inside the agent's sandbox, returning the spec | operators can't read back a spec they wrote |
| Artifacts | files on a volume, metadata in Postgres (ADR-008) | blobs in Postgres, an object store | single host |
| Reliability | leases with epoch fencing, at-least-once execution, exactly-once results (ADR-009) | exactly-once execution claims | a recovered attempt re-runs the agent and pays for it again |
| Observability | Prometheus, OpenTelemetry, labels from closed sets (ADR-010) | free-form labels and logged payloads | no prompts or outputs in traces |
| Benchmarks | open-loop burst on one host, reported with its limits (ADR-011) | a tuned, paced demo | numbers are indicative, not general |
| Auth | opaque API keys with scopes, fail-open rate limits (ADR-012) | OAuth, JWTs | no users, no per-object ownership |

Limitations that follow from those choices, and the deviations from the original plan that implementation forced, are listed below.

### Known limitations

- **Reliability scope (Phase 5):**
  - **At-least-once execution.** A recovered or retried attempt re-runs the agent from the start (model calls are paid for again). Only the recorded result is exactly-once.
  - **Single host.** Container kills, the orphan sweeps and the artifact volume assume one Docker daemon, and the artifact store is a local named volume shared by workers on that host. Several hosts would need an object store behind `ArtifactStore` and per-host sweeps.
  - **Recovery needs a live worker,** because the reconciler runs inside the workers. Recovery takes about one lease TTL (30 s by default); a cancel of a running run takes up to one heartbeat (10 s) plus the runtime's next check.
  - **Failure handling is tested, and the happy path is benchmarked, but crashes under load are not.** [The benchmarks](#14-benchmarks) measure throughput and scaling with no injected faults; the crash scenarios are functional tests.
  - **A batch created without `Idempotency-Key`** whose dispatch fails isn't re-dispatched by the client, but the reconciler republishes its `QUEUED` runs.
- **Authentication is API keys only (Phase 8).** No users, no OAuth, no per-object ownership. TLS is terminated by the production reverse proxy (Caddy), not by the API.
- **Metrics are scraped and traces are an opt-in export** (see [Observability](#observability)); beyond them, observability is structured JSON logs (with `run_id`, `task_id`, `attempt_no`, `worker_id`), the trajectory, and the run, attempt, evaluation and `workers` rows.
- **Agent runs:**
  - Budgets are checked between model calls (the worst-case overshoot is one call).
  - Model calls are non-streaming.
  - Costs are list-price estimates (no batch or volume discounts, 5-minute cache writes).
  - A tool that leaves a process holding stdout takes about 2 s longer (Docker's stream drain).
- **File mtimes (decided in Phase 4, C8):** agent-task files and tool writes use real mtimes. S1 command-task files deliberately keep `tar`'s mtime 0, so S1 execution is unchanged; a command task using `make`, or anything else that compares timestamps, could still misbehave.
- **Evaluation (Phase 4):**
  - **No rescoring endpoint and no re-running of checks** (deferred; the score storage already supports rescoring).
  - **Mutable image tags.** Evaluation containers use the task's image *tag*. The agent's resolved image ID is recorded, but grading isn't verified against it.
  - **pytest checks fail closed** if the image has no pytest (recorded as `tests_failed`).
  - **Indirect disclosure.** Public per-check scores and positions reveal how many hidden tests and checks exist (not model-visible).
  - **Specs are write-only,** even for their author.
  - **Summaries are computed in memory** over at most 1000 runs per batch.
  - **No LLM-as-judge** (deferred).

### Deviations from the approved plan (found during implementation)

S1:

1. **File copy uses a tar streamed over `exec` stdin, not `put_archive`.** Docker rejects `put_archive` when the rootfs is read-only, and keeping the read-only rootfs won out.
2. **The worker's Redis client sets `socket_timeout` longer than its `XREADGROUP BLOCK`.** redis-py 8 defaults it to 5 s, which broke 5 s blocking reads.
3. **`watchfiles` is an explicit worker dependency.** The plan assumed it came with `uvicorn[standard]`, which only the API installs.
4. **`runs.error_message` column.** A run that fails before any attempt exists (`CLAIMED → FAILED`) still records why.
5. **The orphan sweep is deadline-based** (an `agenteval.deadline` label), so it is safe with several workers sharing one Docker daemon.

Phase 3 (details in ADR-005):

6. **A post-tool process reaper** was added after the §15 spike showed `timeout` alone misses background children and `setsid` escapees.
7. **`Workspace.abort()`** was added so cancellation can interrupt a tool call that's in progress.
8. **Tool commands set `PYTHONDONTWRITEBYTECODE=1`, and tool writes use real mtimes.** A same-size edit within the same second otherwise ran stale bytecode.
9. **Tool schemas are sent to Anthropic as a strict-compatible copy.** Unsupported constraints are removed and described in words; the full schema is still enforced.
10. **Anthropic effort is stored explicitly** (default `high`), so a version's hash says exactly what is sent. Haiku 4.5 takes no effort or thinking parameters.
11. **A latent S1 consumer bug was fixed:** shutdown didn't start the drain while every concurrency slot was busy. The drain is now phased within the same 330 s total.
12. **Over-cap agent limits are rejected (422), not clamped.**

Phase 4 (details in ADR-006 and ADR-007):

13. **Hidden evaluation data is never readable through the API.** The first design allowed it until auth existed; it was amended to strict secrecy before implementation.
14. **Check kinds were split** into `python_tests`, `io`, `command` (trusted environment only), `final_answer` and `file`, after the spike found in-process grading attacks (C2–C7).
15. **Any tar warning fails a snapshot export.** GNU tar skips unix sockets with exit 0, which would be a silent loss.
16. **Rescoring was deferred**, though it was in the original API design.
17. **Task request and response schemas moved to `schemas/task_api.py`** to break an import cycle with the evaluation spec.

## 20. Future work

Things deliberately left out, roughly in the order that would matter:

- **Stronger isolation:** gVisor or Firecracker for the sandboxes, a Docker socket proxy (or rootless Docker) for the worker, and a worker that does not need to be root-equivalent. See the [security review](docs/security-review.md).
- **Multi-host operation:** an object-store backend behind `ArtifactStore`, per-host container sweeps and kills, and the reconciler's single-host assumptions lifted.
- **Rescoring and re-checking:** an API to re-run the checks against a stored snapshot (the score storage already supports rescoring), and a re-gradable `ERROR` state.
- **LLM-as-judge** for tasks without objective checks, with its limitations documented and never as the only signal.
- **More model providers** behind the existing `ModelProvider` interface (OpenAI, Gemini), and batch or volume pricing in the cost estimates.
- **Users and ownership:** OAuth or OIDC, per-user audit, per-object ownership, a least-privilege split of database roles.
- **Live updates** in the dashboard (server-sent events instead of polling) and richer failure drill-downs.
- **Better benchmarks:** paced (closed-loop) submission, sampling the Docker daemon and the sandboxes themselves, and fault injection under load.
- **Streaming model calls** and budgets enforced within a call rather than between calls.
