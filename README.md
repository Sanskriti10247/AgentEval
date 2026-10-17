# AgentEval

A platform for running, observing, evaluating and benchmarking AI agents inside controlled execution environments.

> **Status: Phase 5 complete (reliability and recovery).** Two kinds of task run through the same pipeline:
>
> - **Command tasks** (the S1 slice) run one command in a locked-down Docker sandbox.
> - **Agent tasks** give instructions to an agent: a hand-written loop that calls a model and uses six sandboxed tools, within step, token, cost and time limits. Every model call and tool call is recorded as a trajectory.
>
> **Objective grading (Phase 4).** An agent task can carry a hidden evaluation spec, which is graded after the attempt in a fresh, hardened evaluation container. Each attempt gets a failure category and a transparent score. **Benchmark evaluations** run an agent version over a frozen dataset, then summarise and compare the results.
>
> **Reliability (Phase 5).** Runs have budgeted, retried attempts. Workers hold fenced leases renewed by a separate heartbeat process, and a reconciler in every worker recovers crashed workers, lost messages and stuck evaluations. Runs and batches can be cancelled, failed runs are dead-lettered (`FAILED` plus a `dead_letter_reason`) and can be replayed, and workspace snapshots live on a volume. See [Failure handling](#failure-handling) and [ADR-009](docs/decisions/ADR-009-reliability-and-recovery.md).
>
> The default model provider is a deterministic scripted fake. Real Anthropic models are **opt-in**.
>
> **Not built yet:**
>
> - **Phase 6 (in progress):** Prometheus metrics are built (below); tracing, the dashboard read API and the web UI are not.
> - **Phase 7:** performance and throughput benchmarks of the platform itself (distinct from Phase 4's benchmark evaluations, which measure agents). No performance numbers are claimed anywhere in this repository yet.
> - **Phase 8:** API-key authentication, scopes, rate limiting and the dashboard's key handling are built ([ADR-012](docs/decisions/ADR-012-production-polish.md), [docs/api/auth.md](docs/api/auth.md)); CI/CD, the production deployment and the security review are not.
> - **Deferred:** rescoring through the API, and LLM-as-judge.

## Architecture

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

### Evaluation

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

## Run status vs execution outcome

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

**`COMPLETED` never means the task was solved.** It means the result is attributable to the task or the agent. Whether an agent actually solved its task is decided by the [evaluation](#evaluation), recorded separately from the run.

`FAILED` outcomes are platform faults, so the task was never fairly tested; they are the only ones that are retried (and only when the failure reason is retryable). A `COMPLETED` outcome is never retried.

The mapping lives in one pure function, `states.status_for()`, and the database enforces it with CHECK constraints.

Legal transitions (the full table with transition codes is in [ADR-009](docs/decisions/ADR-009-reliability-and-recovery.md)):

```text
QUEUED ─► CLAIMED ─► RUNNING ─► COMPLETED
  ▲          │          │ ├────► FAILED ──replay──► QUEUED
  │          │          │ └────► RETRYING ─(backoff)─► QUEUED
  └──────────┘          └──────► CANCELLED   (also from QUEUED, CLAIMED, RETRYING)
 (claim lease expired)
```

`CLAIMED → QUEUED` happens when a claim's lease expires before an attempt starts (at most 3 times, then `FAILED` with `claim_abandoned`). `CLAIMED → FAILED` also happens when the run or task is inconsistent at claim time.

## Failure handling

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

## Observability

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

### Tracing

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

## Local setup

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

### Try it: a benchmark evaluation (scripted fake model; offline, free; needs `jq`)

Two agents are evaluated on a two-task frozen dataset, then compared. Rerunning needs a new dataset `version`, because `(name, version)` is unique.

```bash
AUTH="Authorization: Bearer $AGENTEVAL_API_KEY"
API=localhost:8000/api/v1
post() { curl -sf -X POST "$API$1" -H "$AUTH" -H 'content-type: application/json' "${@:2}"; }

# Two scripted agents (the fake provider: deterministic, offline, free).
version() {  # $1 agent name, $2 tools, $3 script
  local agent; agent=$(post /agents -d "{\"name\": \"$1-$RANDOM\"}" | jq -r .id)
  post "/agents/$agent/versions" -d "{\"model_provider\": \"fake\", \"model\": \"fake-model\",
    \"tools\": $2, \"limits\": {\"max_steps\": 10}, \"model_settings\": {\"script\": $3}}" | jq -r .id
}
FIXER=$(version fixer '["write_file", "run_tests"]' '[
  {"type": "turn", "tool_calls": [{"name": "write_file",
     "input": {"path": "calc.py", "content": "def add(a, b):\n    return a + b\n"}}]},
  {"type": "turn", "tool_calls": [{"name": "run_tests"}]},
  {"type": "turn", "text": "Fixed add()."}]')
QUITTER=$(version quitter '["write_file"]' '[{"type": "turn", "text": "I could not fix it."}]')

# A dataset of two agent tasks: hidden tests + a protected file, and a final-answer check.
DS=$(post /datasets -d "{\"name\": \"calc-demo\", \"version\": \"1\"}" | jq -r .id)
task() {  # $1 name, $2 evaluation spec
  post /tasks -d "{\"name\": \"$1\", \"task_type\": \"agent\", \"image\": \"agenteval-sandbox:py312\",
    \"instructions\": \"The tests fail. Fix calc.py so they pass.\",
    \"test_command\": [\"python\", \"-m\", \"unittest\", \"-q\"],
    \"files\": {\"calc.py\": \"def add(a, b):\n    return a - b\n\",
              \"test_calc.py\": \"import unittest\nfrom calc import add\nclass T(unittest.TestCase):\n    def test(self):\n        self.assertEqual(add(2, 3), 5)\n\"},
    \"timeout_s\": 120, \"dataset_id\": \"$DS\", \"evaluation\": $2}" | jq '{id, has_evaluation}'
}
task hidden-tests '{
  "checks": [{"kind": "python_tests", "name": "hidden", "pattern": "test_h*.py", "expected_tests": 1}],
  "hidden_files": {"test_hidden.py": "import unittest\nfrom calc import add\nclass H(unittest.TestCase):\n    def test(self):\n        self.assertEqual(add(-1, 1), 0)\n"},
  "protected_paths": ["test_calc.py"]}'
task final-answer '{"checks": [{"kind": "final_answer", "name": "answer", "match": "contains", "expected": "Fixed"}]}'
post "/datasets/$DS/freeze" | jq '{status, task_count}'

# Evaluate both agents (one batch each), then wait for grading to finish.
evaluate() {  # $1 version, $2 repetitions
  post /evaluations -H "Idempotency-Key: demo-$1" \
    -d "{\"name\": \"calc-demo\", \"dataset_id\": \"$DS\", \"agent_version_id\": \"$1\", \"repetitions\": $2}" | jq -r .id
}
E1=$(evaluate "$FIXER" 2); E2=$(evaluate "$QUITTER" 1)
for E in $E1 $E2; do
  until curl -sf -H "$AUTH" "$API/evaluations/$E" | jq -e .progress.complete >/dev/null; do sleep 1; done
done

curl -sf -H "$AUTH" "$API/evaluations/$E1/summary" | jq '{graded, passed, success_rate, ci95, failure_categories, mean_score}'
curl -sf -H "$AUTH" "$API/evaluations/$E2/summary" | jq '{graded, passed, success_rate, failure_categories}'
curl -sf -H "$AUTH" "$API/evaluations/$E1/results?limit=1" | jq '.results[0].evaluation'
curl -sf -H "$AUTH" "$API/evaluations/compare?ids=$E1,$E2" | jq '[.per_task[] | {task_id, delta_vs_first}]'
```

What it prints (real output, trimmed):

- **The fixer** gets `graded 4, passed 4, success_rate 1.0`, `ci95 [0.51, 1.0]`, mean `overall` `0.98497`.
- **The quitter** gets `graded 2, passed 0`, with `failure_categories {"tests_failed": 1, "wrong_answer": 1}`.
- **Each result row** carries only the public projection: status, category, tags, per-position check results, score components and metrics. Hidden tests, check names and expected values never appear.

## Dashboard

A Next.js dashboard (`apps/web`) reads the public REST API: KPIs and charts, agents and their versions, tasks, evaluations (detail and A vs B vs C comparison), runs (with the dead-letter view, trajectory, cancel and replay) and workers. It has no database access. It asks for an API key on first use (below).

```bash
make up && make migrate     # the API on http://localhost:8000
make web-up                 # the dashboard container on http://localhost:3000 (compose profile "web")
make web                    # or: a dev server with hot reload on :3000 (npm ci + npm run dev)
make web-check              # typecheck, lint, tests and production build
```

The dashboard service is opt-in (profile `web`), so `make up` and the e2e runs never pay for a Node build. With compose it is served on `127.0.0.1:${AGENTEVAL_WEB_HOST_PORT:-3000}`. The browser calls the API directly, so `NEXT_PUBLIC_API_URL` (build arg `AGENTEVAL_WEB_API_URL`, default `http://localhost:8000`) is baked into the bundle at build time, and the API's CORS list (`AGENTEVAL_CORS_ORIGINS`) must contain the dashboard's origin. **API key (ADR-012 D).** On first use the dashboard shows an "API key" screen. The key is kept in the tab's `sessionStorage` (gone when the tab closes), sent only as `Authorization: Bearer`, and forgotten by **Sign out** in the sidebar. A 401 (invalid, revoked or expired key) returns you to the key screen with a message; a 403 on cancel or replay says the key lacks the `write` scope (a `read` key can browse). Create a key with `docker compose exec api python -m agenteval_api.keys create --name dashboard --scopes read,write`. If the API runs with `AGENTEVAL_AUTH_ENABLED=false` enter any value. Keep both ports bound to localhost unless you put a TLS proxy in front. Details: [apps/web/README.md](apps/web/README.md).

## API

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

## Tools

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

## Real models (opt-in)

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

## Repository layout

```text
packages/core/agenteval_core/
  config, db, states (state machine), hashing, auth (API-key format and hashing),
  logging (with secret redaction)
  models/, schemas/, repositories/, services/, queue/
  reliability/  state machines, failure taxonomy, retry policy and backoff, lease timing,
                lease keeper process
  artifacts/    ArtifactStore (snapshot files on a volume; metadata row in Postgres)
  agent/        runtime loop, limits, context, events, prompts, pricing, retry policy
  providers/    ModelProvider protocol, FakeProvider, Anthropic settings (no SDK here)
  tools/        Tool contract, ToolSet/registry, the six tools, Workspace protocol
  evaluation/   pure grading domain: spec, checks (plan/judge), matchers, parsers,
                integrity, metrics, classify, scoring, stats, public (the D11 projection)
packages/core/migrations/       Alembic: 0001 (tasks, runs, attempts), 0002 (agent runtime),
                                0003 (datasets, evaluations, artifacts, attempt evaluations, scores),
                                0004-0009 (reliability: leases, failure reasons, budgets, worker stop
                                reasons, evaluation leases, replays), 0010 (api_keys)
apps/api/agenteval_api/         FastAPI app, deps, auth (scope guard), ratelimit, keys (operator CLI), error mapping
                                (sanitised 422s), routes/
apps/worker/agenteval_worker/   main (signals, drain), consumer, executor, agent_runner,
                                evaluation_runner, reconciler, lease_manager, trajectory writer,
                                sandbox/ (session, evaluation, workspace), providers/
infrastructure/docker/          api, worker and sandbox Dockerfiles
tests/integration/              testcontainers Postgres 16 + Redis 7 (real migrations)
tests/e2e/                      against the running compose stack (incl. Phase 4 scenarios)
docs/architecture/, docs/api/   evaluation architecture and API reference (incl. auth.md)
docs/decisions/                 ADR-001 … ADR-012
docker-compose.yml              the dev stack (CPU weights, artifact volume)
docker-compose.e2e.yml          short lease timings, applied by `make e2e`
```

## Testing

```bash
make test        # unit + integration + real-Docker tests (no network, no API key)
make test-unit   # no Docker needed
make e2e         # needs `make up && make migrate`; recreates the worker with short lease timings
make lint        # ruff + mypy
```

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

## Security model

This is a **research and portfolio sandbox, not a hardened multi-tenant security boundary**.

- **Sandbox controls:**
  - no network (`network_mode=none`)
  - memory limit with no swap, CPU quota, `pids_limit=128`
  - read-only rootfs, with size-capped tmpfs at `/workspace` and `/tmp`
  - uid 1000, all capabilities dropped, `no-new-privileges`, tini as PID 1
  - wall-clock limits, output caps, and the container is always removed
- **Agent tools** add a per-call timeout, the post-call reaper, argv-only execution and an allowlist for `shell`.
  - **The allowlist and path checks are policy, not the isolation boundary.** The container is the boundary: an agent allowed `sh` can do anything the container permits.
- **Prompt injection through task files and tool output is expected.** Its reach is limited to the version's tools inside the container, and a runaway agent is stopped by its step, token, cost and time limits.
- **Model credentials** live only in the worker's environment. They are never passed into sandbox containers, never in prompts, and the API image doesn't install the SDK. Structured logs redact `sk-ant-…` values, `ae_…` API keys, `Bearer …` text and credential-named fields (`api_key`, `authorization`, `x-api-key`, …).
- **Image allowlist:** tasks may only use images listed in `AGENTEVAL_IMAGE_ALLOWLIST`. The worker never pulls images.
- **The worker mounts `/var/run/docker.sock`, which is host-root equivalent.**
  - The worker is therefore a trusted component and runs as root in its container.
  - The API has no socket access, runs as a non-root user, and its image installs neither the Docker SDK nor the Anthropic SDK.
- **Sandboxes share the host kernel,** so a kernel exploit is not mitigated beyond Docker defaults. gVisor or Firecracker would be the next step.
- **Evaluation container** (ADR-006): the same controls, plus `noexec,nosuid,nodev` on every writable mount, and hidden files in a root-owned, read-only `/eval`. Snapshots are validated header by header on the host and are only ever unpacked inside a sandbox.
  - **Residual risk:** candidate code runs inside the test process, so it can read `/eval` while it's being graded, and could cheat in-process (C7). `io` checks are the robust choice when integrity matters most.
- **Evaluation secrecy** (ADR-007): hidden files, check names, expected values and evaluator configuration never appear in API responses or model-visible data. Specs are write-only, every response goes through one public projection, and 422s are sanitised.
  - Evaluation log events carry only ids, statuses and categories. The one exception is an *unexpected* evaluator exception, which is logged with its traceback (operator-only).
- **Authentication and authorization (ADR-012 A, B).** Every `/api/v1` request needs an API key (`ae_<prefix>_<secret>`, 256 bits of randomness; only its SHA-256 is stored, compared in constant time). Keys have `read`, `write` and `admin` scopes, can expire and can be revoked; a test fails if any route lacks a scope. The first key is minted by an operator CLI inside the API container (no anonymous endpoint). `/health`, `/ready` and `/metrics` are open: `/metrics` is meant for the internal network only. The platform is single-tenant (scopes, not per-object ownership).
- **Rate limiting (ADR-012 C).** A per-key, per-minute budget for reads (600) and writes (120) in Redis, 429 with `Retry-After`. It **fails open** if Redis is down (logged and counted), because Redis is never the authority.
- **Limits of this model.** Keys are bearer credentials: anyone holding one has its scopes, so use TLS (the dashboard and API are plain HTTP on localhost by default) and keep keys out of code. There is no per-user audit beyond the key name, no key rotation automation, and the dashboard keeps its key in `sessionStorage`, which any script running on the dashboard's origin can read. `AGENTEVAL_AUTH_ENABLED=false` removes all of this and is for local experiments only. Compose still binds every port to `127.0.0.1`.

## Known limitations (by design; fixed in the phase noted)

- **Reliability scope (Phase 5):**
  - **At-least-once execution.** A recovered or retried attempt re-runs the agent from the start (model calls are paid for again). Only the recorded result is exactly-once.
  - **Single host.** Container kills, the orphan sweeps and the artifact volume assume one Docker daemon, and the artifact store is a local named volume shared by workers on that host. Several hosts would need an object store behind `ArtifactStore` and per-host sweeps.
  - **Recovery needs a live worker,** because the reconciler runs inside the workers. Recovery takes about one lease TTL (30 s by default); a cancel of a running run takes up to one heartbeat (10 s) plus the runtime's next check.
  - **Failure handling is tested, not load-tested.** Behaviour under sustained load, many workers and throughput are Phase 7 and nothing about them is claimed.
  - **A batch created without `Idempotency-Key`** whose dispatch fails isn't re-dispatched by the client, but the reconciler republishes its `QUEUED` runs.
- **Authentication is API keys only (Phase 8).** No users, no OAuth, no per-object ownership, no TLS termination in the stack (the production reverse proxy is a later Phase 8 step).
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

## Deviations from the approved plan (found during implementation)

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
