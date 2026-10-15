# AgentEval

A platform for running, observing, evaluating and benchmarking AI agents inside controlled execution environments.

> **Status: Phase 4 complete (evaluation and benchmark evaluations).** Two kinds of task run through the same pipeline:
>
> - **Command tasks** (the S1 slice) run one command in a locked-down Docker sandbox.
> - **Agent tasks** give instructions to an agent: a hand-written loop that calls a model and uses six sandboxed tools, within step, token, cost and time limits. Every model call and tool call is recorded as a trajectory.
>
> Phase 4 adds **objective grading**. An agent task can carry a hidden evaluation spec, which is graded after the attempt in a fresh, hardened evaluation container. Each attempt gets a failure category and a transparent score. **Benchmark evaluations** run an agent version over a frozen dataset, then summarise and compare the results.
>
> The default model provider is a deterministic scripted fake. Real Anthropic models are **opt-in**.
>
> **Not built yet:**
>
> - **Phase 5:** attempt retries, leases and heartbeats, recovery of stuck evaluations, the cancel API, dead-letter handling.
> - **Phase 6:** metrics and tracing.
> - **Phase 7:** performance and throughput benchmarks of the platform itself (distinct from Phase 4's benchmark evaluations, which measure agents).
> - **Phase 8:** authentication, rate limiting, the web UI.
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
- **Redis Streams** handle dispatch. Duplicate messages are harmless because the claim is conditional (ADR-001).
- **One Docker container per attempt**, with no network, resource limits, a read-only rootfs and a non-root user (ADR-003).
- **The agent runtime is hand-written,** not a framework. It is provider-neutral and lives in core (ADR-005).
- **Evaluation is a separate record, written after the attempt is durable,** and graded in a fresh hardened container. It never changes the run's outcome (ADR-006).
- **Hidden evaluation data never leaves the evaluator.** Specs are write-only, and every API response goes through one public projection (ADR-007).

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
| `COMPLETED` | the platform produced a **trustworthy outcome** (the task may still have failed) |
| `FAILED` | the platform **could not** produce a trustworthy outcome |

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

`FAILED` outcomes are platform faults, so the task was never fairly tested; they are the only ones Phase 5 will make retryable.

The mapping lives in one pure function, `states.status_for()`, and the database enforces it with CHECK constraints.

Legal transitions: `QUEUED → CLAIMED → RUNNING → COMPLETED | FAILED`, plus `CLAIMED → FAILED` when an attempt can't be started.

## Local setup

Prerequisites: [uv](https://docs.astral.sh/uv/), and Docker with Compose v2 (Docker Desktop on macOS works).

```bash
cp .env.example .env     # optional; defaults work
make sync                # uv sync --all-packages (Python 3.12 is pinned; uv fetches it)
make up                  # builds the sandbox image, starts postgres, redis, api, worker
make migrate             # alembic upgrade head (inside the api container)
make e2e                 # full-stack tests over real HTTP (command tasks, agent tasks, Phase 4 benchmark scenarios)
make logs / make down
```

- Host ports default to **55432** (Postgres), **56379** (Redis) and **8000** (API). Postgres and Redis avoid the standard ports so they don't clash with local instances; all three can be changed in `.env`.
- The API reloads on code changes, and so does the worker, via `watchfiles` (in-flight runs drain first).
- Scale out with `docker compose up -d --scale worker=3`.

### Try it: a command task

```bash
curl -s -X POST localhost:8000/api/v1/tasks -H 'content-type: application/json' -d '{
  "name": "hello", "image": "agenteval-sandbox:py312",
  "command": ["python", "main.py"],
  "files": {"main.py": "print(\"hi\")\nraise SystemExit(3)"},
  "timeout_s": 10
}'
# -> {"id": "<task_id>", ...}

curl -s -X POST localhost:8000/api/v1/runs -H 'content-type: application/json' \
     -H 'Idempotency-Key: demo-1' -d '{"task_id": "<task_id>"}'
# -> 202 {"id": "<run_id>", "status": "QUEUED", ...}

curl -s localhost:8000/api/v1/runs/<run_id>            # status COMPLETED, outcome NON_ZERO_EXIT
curl -s localhost:8000/api/v1/runs/<run_id>/attempts   # exit_code 3, stdout "hi\n"
```

### Try it: an agent task (scripted fake model; offline, free)

```bash
API=localhost:8000/api/v1
curl -s -X POST $API/agents -H 'content-type: application/json' -d '{"name": "fixer"}'
# -> {"id": "<agent_id>", ...}

curl -s -X POST $API/agents/<agent_id>/versions -H 'content-type: application/json' -d '{
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

curl -s -X POST $API/tasks -H 'content-type: application/json' -d '{
  "name": "fix-add", "task_type": "agent", "image": "agenteval-sandbox:py312",
  "instructions": "The tests fail. Fix calc.py so they pass.",
  "test_command": ["python", "-m", "unittest", "-q"],
  "files": {"calc.py": "def add(a, b):\n    return a - b\n",
            "test_calc.py": "import unittest\nfrom calc import add\nclass T(unittest.TestCase):\n    def test(self):\n        self.assertEqual(add(2, 3), 5)\n"},
  "timeout_s": 120
}'
curl -s -X POST $API/runs -H 'content-type: application/json' \
     -d '{"task_id": "<task_id>", "agent_version_id": "<version_id>"}'

curl -s $API/runs/<run_id>                        # COMPLETED / AGENT_FINISHED
curl -s $API/runs/<run_id>/attempts               # steps 4, tokens_in 400, tokens_out 80, cost_usd "0.000800"
curl -s "$API/runs/<run_id>/attempts/1/events?limit=100"   # the trajectory, in seq order
```

### Try it: a benchmark evaluation (scripted fake model; offline, free; needs `jq`)

Two agents are evaluated on a two-task frozen dataset, then compared. Rerunning needs a new dataset `version`, because `(name, version)` is unique.

```bash
API=localhost:8000/api/v1
post() { curl -sf -X POST "$API$1" -H 'content-type: application/json' "${@:2}"; }

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
  until curl -sf "$API/evaluations/$E" | jq -e .progress.complete >/dev/null; do sleep 1; done
done

curl -sf "$API/evaluations/$E1/summary" | jq '{graded, passed, success_rate, ci95, failure_categories, mean_score}'
curl -sf "$API/evaluations/$E2/summary" | jq '{graded, passed, success_rate, failure_categories}'
curl -sf "$API/evaluations/$E1/results?limit=1" | jq '.results[0].evaluation'
curl -sf "$API/evaluations/compare?ids=$E1,$E2" | jq '[.per_task[] | {task_id, delta_vs_first}]'
```

What it prints (real output, trimmed):

- **The fixer** gets `graded 4, passed 4, success_rate 1.0`, `ci95 [0.51, 1.0]`, mean `overall` `0.98497`.
- **The quitter** gets `graded 2, passed 0`, with `failure_categories {"tests_failed": 1, "wrong_answer": 1}`.
- **Each result row** carries only the public projection: status, category, tags, per-position check results, score components and metrics. Hidden tests, check names and expected values never appear.

## API

The OpenAPI docs are at <http://localhost:8000/docs>. Errors are RFC 7807-style `application/problem+json`.

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/health`, `/ready` | liveness; readiness checks Postgres and Redis (503 if either is down) |
| POST | `/api/v1/agents` | 201; 409 if the name is taken |
| GET | `/api/v1/agents/{id}` | |
| POST | `/api/v1/agents/{id}/versions` | 201 new version; **200** when an equivalent config already exists; 422 if it can't run |
| GET | `/api/v1/agents/{id}/versions`, `…/versions/{n}` | versions are immutable |
| POST | `/api/v1/tasks` | `task_type` `command` (default) or `agent`. 201; 422 on validation or an image not on the allowlist |
| GET | `/api/v1/tasks/{id}` | |
| POST | `/api/v1/runs` | body `{task_id, agent_version_id?}`, optional `Idempotency-Key` header |
| GET | `/api/v1/runs/{id}` | |
| GET | `/api/v1/runs/{id}/attempts` | exit code and output (command tasks); steps, tokens, cost and final answer (agent tasks) |
| GET | `/api/v1/runs/{id}/attempts/{n}/events` | the trajectory, paged by `after_seq` (default -1) and `limit` (1–1000, default 100); `next_after_seq` is null on the last page |
| GET | `/api/v1/runs/{id}/attempts/{n}/evaluation` | the attempt's **public** evaluation; 404 if none is recorded |
| POST | `/api/v1/datasets` | 201 DRAFT dataset; 409 if `(name, version)` exists |
| GET | `/api/v1/datasets/{id}` | includes `task_ids` |
| POST | `/api/v1/datasets/{id}/freeze` | DRAFT → FROZEN (idempotent); 409 if it has no tasks |
| POST | `/api/v1/evaluations` | a benchmark evaluation: agent version × frozen dataset × repetitions; optional `Idempotency-Key`; 202 new / 200 replay / 409 / 503 |
| GET | `/api/v1/evaluations/{id}` | the batch, scoring config and hash, derived progress |
| GET | `/api/v1/evaluations/{id}/results` | public per-run results, keyset-paged |
| GET | `/api/v1/evaluations/{id}/summary` | success rate + CI, failure categories, score means, latency, cost, per task |
| GET | `/api/v1/evaluations/compare?ids=a,b` | 2–5 batches on the same dataset and scoring config (else 422) |

The full reference for the Phase 4 endpoints is [docs/api/evaluations.md](docs/api/evaluations.md). `POST /tasks` also accepts `dataset_id`, `difficulty` and a **write-only** `evaluation` spec: responses only say `has_evaluation`. `RunRead` gains `evaluation_id` and `repetition`, and attempts gain `sandbox_image_id`.

**Validation errors (422) never echo the submitted input.** Errors inside an evaluation spec get a fixed message and a truncated location, so no hidden content comes back (ADR-007).

**`POST /api/v1/runs` responses:**

- **202:** a new run was created.
- **200:** a replay of an existing `Idempotency-Key`.
- **409:** the key was already used for a different task or agent version.
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
  config, db, states (state machine), hashing, logging (with secret redaction)
  models/, schemas/, repositories/, services/, queue/
  agent/        runtime loop, limits, context, events, prompts, pricing, retry policy
  providers/    ModelProvider protocol, FakeProvider, Anthropic settings (no SDK here)
  tools/        Tool contract, ToolSet/registry, the six tools, Workspace protocol
  evaluation/   pure grading domain: spec, checks (plan/judge), matchers, parsers,
                integrity, metrics, classify, scoring, stats, public (the D11 projection)
packages/core/migrations/       Alembic: 0001 (tasks, runs, attempts), 0002 (agent runtime),
                                0003 (datasets, evaluations, artifacts, attempt evaluations, scores)
apps/api/agenteval_api/         FastAPI app, deps, error mapping (sanitised 422s), routes/
apps/worker/agenteval_worker/   main (signals, drain), consumer, executor, agent_runner,
                                evaluation_runner, trajectory writer,
                                sandbox/ (session, evaluation, workspace), providers/
infrastructure/docker/          api, worker and sandbox Dockerfiles
tests/integration/              testcontainers Postgres 16 + Redis 7 (real migrations)
tests/e2e/                      against the running compose stack (incl. Phase 4 scenarios)
docs/architecture/, docs/api/   evaluation architecture and API reference
docs/decisions/                 ADR-001 … ADR-007
```

## Testing

```bash
make test        # unit + integration + real-Docker tests (no network, no API key)
make test-unit   # no Docker needed
make e2e         # needs `make up && make migrate`
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

**E2E configuration:**
- The E2E suite reads `AGENTEVAL_E2E_URL` (default `http://localhost:8000`).
- The invariant scenario also reads `AGENTEVAL_E2E_DATABASE_URL` (default `postgresql://agenteval:agenteval@127.0.0.1:55432/agenteval`), through a **SELECT-only** probe. It is skipped if the database is unreachable.
- A full `make e2e` takes a few minutes and needs a Docker host that isn't starved of memory.

**Known flaky test:** `tests/integration/test_agent_executor.py::test_drain_deadline_cancels_agent_runs_and_records_why` is timing-sensitive. Its 1 s cancel grace races Docker teardown on a busy host, so it fails intermittently; it fails at the Phase 3 code too. It has been left unchanged so far.

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
- **Model credentials** live only in the worker's environment. They are never passed into sandbox containers, never in prompts, and the API image doesn't install the SDK. Structured logs redact `sk-ant-…` values and credential-named fields (`api_key`, `authorization`, `x-api-key`, …).
- **Image allowlist:** tasks may only use images listed in `AGENTEVAL_IMAGE_ALLOWLIST`. The worker never pulls images.
- **The worker mounts `/var/run/docker.sock`, which is host-root equivalent.**
  - The worker is therefore a trusted component and runs as root in its container.
  - The API has no socket access, runs as a non-root user, and its image installs neither the Docker SDK nor the Anthropic SDK.
- **Sandboxes share the host kernel,** so a kernel exploit is not mitigated beyond Docker defaults. gVisor or Firecracker would be the next step.
- **Evaluation container** (ADR-006): the same controls, plus `noexec,nosuid,nodev` on every writable mount, and hidden files in a root-owned, read-only `/eval`. Snapshots are validated header by header on the host and are only ever unpacked inside a sandbox.
  - **Residual risk:** candidate code runs inside the test process, so it can read `/eval` while it's being graded, and could cheat in-process (C7). `io` checks are the robust choice when integrity matters most.
- **Evaluation secrecy** (ADR-007): hidden files, check names, expected values and evaluator configuration never appear in API responses or model-visible data. Specs are write-only, every response goes through one public projection, and 422s are sanitised.
  - Evaluation log events carry only ids, statuses and categories. The one exception is an *unexpected* evaluator exception, which is logged with its traceback (operator-only).
- **No authentication or rate limiting yet (Phase 8).** Compose binds every port to `127.0.0.1` only.

## Known limitations (by design; fixed in the phase noted)

- **Worker crash mid-run:** the run stays `RUNNING` (its trajectory so far is kept). There are no leases or heartbeats yet (Phase 5).
- **No automatic re-dispatch** if `XADD` fails. The client must retry with the same `Idempotency-Key` (a reconciler comes in Phase 5).
- **A worker crash between reading a message and acking it** leaves the message in the stream's pending list, with no reclaim yet (Phase 5).
- **Retries:** model calls are retried within a step (Phase 3), but **attempts** are never retried, and there is no cancel API or dead-letter handling (Phase 5). Phase 3's only cancellation trigger is worker shutdown.
- **If Postgres is unavailable when the result is persisted,** the result is logged but lost, and the run stays `RUNNING` (Phase 5).
- **Agent runs:**
  - Budgets are checked between model calls (the worst-case overshoot is one call).
  - Model calls are non-streaming.
  - Costs are list-price estimates (no batch or volume discounts, 5-minute cache writes).
  - A tool that leaves a process holding stdout takes about 2 s longer (Docker's stream drain).
- **Observability:** structured JSON logs (with `run_id`, `task_id`, `attempt_no`, `worker_id`) and the trajectory. No metrics or tracing yet (Phase 6).
- **File mtimes (decided in Phase 4, C8):** agent-task files and tool writes use real mtimes. S1 command-task files deliberately keep `tar`'s mtime 0, so S1 execution is unchanged; a command task using `make`, or anything else that compares timestamps, could still misbehave.
- **Evaluation (Phase 4):**
  - **No rescoring endpoint and no re-running of checks** (deferred; the score storage already supports rescoring).
  - **No recovery yet (Phase 5).** A worker crash between an attempt's commit and its evaluation record leaves no evaluation. A crash during grading leaves it PENDING or RUNNING, and that batch never reports `complete`. Shutdown during grading marks it ERROR on a best-effort basis.
  - **Mutable image tags.** Evaluation containers use the task's image *tag*. The agent's resolved image ID is recorded, but grading isn't verified against it.
  - **pytest checks fail closed** if the image has no pytest (recorded as `tests_failed`).
  - **Indirect disclosure.** Public per-check scores and positions reveal how many hidden tests and checks exist (not model-visible).
  - **Specs are write-only,** even for their author.
  - **Summaries are computed in memory** over at most 1000 runs per batch.
  - **A batch created without `Idempotency-Key`** whose dispatch fails can't be re-dispatched.
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
