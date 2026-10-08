# AgentEval

A platform for running, observing, evaluating and benchmarking AI agents inside controlled execution environments.

> **Status: Phase 3 complete (agent runtime).** Two kinds of task run through the same pipeline:
>
> - **Command tasks** (the S1 slice) run one command in a locked-down Docker sandbox.
> - **Agent tasks** give instructions to an agent: a hand-written loop that calls a model and uses six sandboxed tools, within step, token, cost and time limits. Every model call and tool call is recorded as a trajectory.
>
> The default model provider is a deterministic scripted fake. Real Anthropic models are **opt-in**.
>
> **Not built yet:** evaluation and scoring (Phase 4); attempt retries, leases, the cancel API and dead-letter handling (Phase 5); metrics and tracing (Phase 6); benchmarks (Phase 7); auth and the web UI (Phase 8).

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
client ──GET /api/v1/runs/{id}[/attempts[/{n}/events]]──► api ──► PostgreSQL
```

- **Modular monolith + workers.** A single domain package, `agenteval_core`, is used by two process types, `api` and `worker` (ADR-004).
- **Postgres is authoritative.** Redis only delivers run ids, and every state change is a guarded `UPDATE` (ADR-002).
- **Redis Streams** handle dispatch. Duplicate messages are harmless because the claim is conditional (ADR-001).
- **One Docker container per attempt**, with no network, resource limits, a read-only rootfs and a non-root user (ADR-003).
- **The agent runtime is hand-written,** not a framework. It is provider-neutral and lives in core (ADR-005).

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

**`COMPLETED` never means the task was solved.** It means the result is attributable to the task or the agent. Whether an agent actually solved its task is Phase 4's evaluator.

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
make e2e                 # full-stack tests over real HTTP (command and agent tasks)
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
  config, db, states (state machine), logging (with secret redaction)
  models/, schemas/, repositories/, services/, queue/
  agent/        runtime loop, limits, context, events, prompts, pricing, retry policy
  providers/    ModelProvider protocol, FakeProvider, Anthropic settings (no SDK here)
  tools/        Tool contract, ToolSet/registry, the six tools, Workspace protocol
packages/core/migrations/       Alembic: 0001 (tasks, runs, attempts), 0002 (agent runtime)
apps/api/agenteval_api/         FastAPI app, deps, error mapping, routes/
apps/worker/agenteval_worker/   main (signals, drain), consumer, executor, agent_runner,
                                trajectory writer, sandbox/ (session, workspace), providers/
infrastructure/docker/          api, worker and sandbox Dockerfiles
tests/integration/              testcontainers Postgres 16 + Redis 7 (real migrations)
tests/e2e/                      against the running compose stack
docs/decisions/                 ADR-001 … ADR-005
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
- **No authentication yet (Phase 8).** Compose binds every port to `127.0.0.1` only.

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
- **Open question:** S1 command-task files are written with a 1970 mtime (`tar`'s default). It affects no current test, but a command task using `make`, or anything else that compares timestamps, could misbehave. Tool writes in agent tasks use real mtimes. This was left unchanged to keep S1 execution identical, pending a decision.

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
