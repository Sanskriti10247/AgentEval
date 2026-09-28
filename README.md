# AgentEval

A platform for running, observing, evaluating and benchmarking AI agents inside controlled execution environments.

> **Status: vertical slice (Phase 1 and Phase 2).** A task can be submitted, dispatched through Redis, executed in a locked-down Docker sandbox by a worker, persisted in Postgres and retrieved over HTTP.
>
> These parts are designed but **not built yet**: the agent runtime (Phase 3), evaluation (Phase 4), retries and recovery (Phase 5), observability (Phase 6), benchmarks (Phase 7), and auth and the web UI (Phase 8).

## Architecture (slice)

```text
client ──HTTP──► api (FastAPI) ──1. INSERT run(QUEUED), COMMIT──► PostgreSQL  (source of truth)
                     │
                     └─2. XADD runs.dispatch {run_id}──► Redis Stream (delivery only)
                                                              │ XREADGROUP (group "workers")
                                                              ▼
                               worker (asyncio, N replicas, semaphore-bounded)
                                 3. UPDATE runs … WHERE status='QUEUED'   (conditional claim)
                                 4. attempt row + RUNNING, COMMIT, then XACK
                                 5. sandbox ──docker.sock──► ephemeral sibling container
                                 6. persist attempt + terminal status (one transaction)
client ──GET /api/v1/runs/{id}──► api ──► PostgreSQL
```

- **Modular monolith + workers.** A single domain package, `agenteval_core`, is used by two process types, `api` and `worker` (ADR-004).
- **Postgres is authoritative.** Redis only delivers run ids, and every state change is a guarded `UPDATE` (ADR-002).
- **Redis Streams** handle dispatch. Duplicate messages are harmless because the claim is conditional (ADR-001).
- **One Docker container per attempt**, with no network, resource limits, a read-only rootfs and a non-root user (ADR-003).

## Run status vs execution outcome

These are two separate columns, because they answer two separate questions.

| `runs.status` (platform lifecycle) | Meaning |
|---|---|
| `QUEUED` | persisted, waiting for a worker |
| `CLAIMED` | exactly one worker owns it (its conditional `UPDATE` won) |
| `RUNNING` | an attempt is executing |
| `COMPLETED` | the platform produced a **trustworthy outcome** (the task may still have failed) |
| `FAILED` | the platform **could not** produce a trustworthy outcome |

| `outcome` (what the command did) | Status it produces |
|---|---|
| `SUCCESS`: exit 0 within the time limit | `COMPLETED` |
| `NON_ZERO_EXIT`: exit ≠ 0, including an OOM kill (`oom_killed=true`, exit 137) | `COMPLETED` |
| `TIMEOUT`: wall-clock limit hit, container killed | `COMPLETED` |
| `SANDBOX_ERROR`: Docker create/start/exec failed, or the image is missing | `FAILED` |
| `INTERNAL_ERROR`: an unexpected worker exception | `FAILED` |

**`COMPLETED` never means the task succeeded.** `TIMEOUT` and `NON_ZERO_EXIT` are results about the task, and in Phase 4 they become evaluation signals. Only `FAILED` outcomes are platform faults, and those are the only ones that will be retryable in Phase 5.

The mapping lives in one pure function, `states.status_for()`, and the database enforces it with CHECK constraints.

Legal transitions: `QUEUED → CLAIMED → RUNNING → COMPLETED | FAILED`, plus `CLAIMED → FAILED` when an attempt can't be started.

## Local setup

Prerequisites: [uv](https://docs.astral.sh/uv/), and Docker with Compose v2 (Docker Desktop on macOS works).

```bash
cp .env.example .env     # optional; defaults work
make sync                # uv sync --all-packages (Python 3.12 is pinned; uv fetches it)
make up                  # builds the sandbox image, starts postgres, redis, api, worker
make migrate             # alembic upgrade head (inside the api container)
make e2e                 # full-stack test over real HTTP
make logs / make down
```

- Host ports default to **55432** (Postgres), **56379** (Redis) and **8000** (API). Postgres and Redis avoid the standard ports so they don't clash with local instances; all three can be changed in `.env`.
- The API reloads on code changes, and so does the worker, via `watchfiles` (in-flight runs drain first).
- Scale out with `docker compose up -d --scale worker=3`.

### Try it

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

## API (slice)

The OpenAPI docs are at <http://localhost:8000/docs>. Errors are RFC 7807-style `application/problem+json`.

| Method | Path | Notes |
|---|---|---|
| GET | `/health` | liveness |
| GET | `/ready` | checks Postgres and Redis; 503 if either is down |
| POST | `/api/v1/tasks` | 201. 422 if the image is not on the allowlist or validation fails |
| GET | `/api/v1/tasks/{id}` | |
| POST | `/api/v1/runs` | body `{task_id}`, optional `Idempotency-Key` header. See the response codes below |
| GET | `/api/v1/runs/{id}` | |
| GET | `/api/v1/runs/{id}/attempts` | exit code, stdout and stderr (≤ 64 KB each, with truncation flags), duration, OOM flag |

`POST /api/v1/runs` response codes:
- **202:** a new run was created.
- **200:** a replay of an existing `Idempotency-Key`.
- **409:** the key was already used for a different task.
- **404:** unknown task.
- **503:** the run was persisted but dispatch failed. Retry with the same key and the run is re-dispatched.

Task limits (validated): `timeout_s` 1–300, `memory_mb` 64–1024, `cpu_limit` 0.1–2.0, `command` up to 64 args. `files` is `{relative_path: content}`, with no absolute paths or `..`, at most 100 files, and at most 1 MB in total.

## Repository layout

```text
packages/core/agenteval_core/   config, db, states (state machine), logging,
                                models/, schemas/, repositories/, services/, queue/
packages/core/migrations/       Alembic (one revision: tasks, runs, run_attempts)
apps/api/agenteval_api/         FastAPI app, deps, error mapping, routes/
apps/worker/agenteval_worker/   main (signals, drain), consumer, executor, sandbox/
infrastructure/docker/          api, worker and sandbox Dockerfiles
tests/integration/              testcontainers Postgres 16 + Redis 7 (real migrations)
tests/e2e/                      against the running compose stack
docs/decisions/                 ADR-001 … ADR-004
```

## Testing

```bash
make test        # unit + integration + real-Docker sandbox tests
make test-unit   # no Docker needed
make e2e         # needs `make up && make migrate`
make lint        # ruff + mypy
```

- **Unit tests:**
  - the state machine and `status_for`
  - task schema limits and path validation
  - output truncation (including multibyte cuts and NUL bytes)
  - tar building
- **Integration tests** (real Postgres and Redis):
  - submit → `QUEUED` plus a stream message
  - idempotency, including 8 concurrent duplicate submits producing 1 run
  - re-dispatch on replay
  - 409 on key reuse
  - 503 on dispatch failure, then recovery through a retry
  - 10 concurrent claims producing exactly 1 winner
  - a duplicate message executing once
  - a stale message being skipped
  - fencing of a worker that lost ownership
  - DB CHECK constraints rejecting inconsistent status and outcome
  - the consumer loop respecting its concurrency bound
  - the executor against a real sandbox
  - regression: a blocking read longer than redis-py's 5 s default socket timeout
- **Sandbox tests** (real Docker):
  - success, non-zero exit and timeout (≈ 2 s, container removed)
  - output produced before a timeout is kept
  - no network (zero routes), OOM kill detected, missing image → `SANDBOX_ERROR`
  - read-only rootfs, uid 1000 with no capabilities, pids limit, output truncation
  - the orphan sweep removes only expired containers
- **E2E:** create a task over HTTP → run it → `COMPLETED`/`NON_ZERO_EXIT`, `exit_code=3`, the expected stdout, one attempt and no leftover containers. It also covers a timeout run through the full stack.

## Security model

This is a **research and portfolio sandbox, not a hardened multi-tenant security boundary**.

- **Sandbox controls:**
  - no network (`network_mode=none`)
  - memory limit with no swap, CPU quota, `pids_limit=128`
  - read-only rootfs, with size-capped tmpfs at `/workspace` and `/tmp`
  - uid 1000, all capabilities dropped, `no-new-privileges`, tini as PID 1
  - a wall-clock timeout enforced by killing the container
  - output capped at 64 KB per stream
  - the container is always removed
- **Image allowlist:** tasks may only use images listed in `AGENTEVAL_IMAGE_ALLOWLIST`. The worker never pulls images.
- **The worker mounts `/var/run/docker.sock`, which is host-root equivalent.** The worker is therefore a trusted component and runs as root in its container. The API has no socket access, and its image doesn't install the Docker SDK. The API runs as a non-root user.
- Sandboxes share the host kernel, so a kernel exploit is not mitigated beyond Docker defaults. gVisor or Firecracker would be the next step.
- **No authentication yet (Phase 8).** Compose binds every port to `127.0.0.1` only.

## Known limitations of the slice (by design; fixed in the phase noted)

- **Worker crash mid-run:** the run stays `RUNNING`. There are no leases or heartbeats yet (Phase 5).
- **No automatic re-dispatch** if `XADD` fails. The client must retry with the same `Idempotency-Key` (a reconciler comes in Phase 5).
- **A worker crash between reading a message and acking it** leaves the message in the stream's pending list, with no reclaim yet (Phase 5).
- **No retries, backoff, cancellation or dead-letter handling** (Phase 5).
- **If Postgres is unavailable when the result is persisted,** the result is logged but lost, and the run stays `RUNNING` (Phase 5).
- **Logs only:** structured JSON with `run_id`, `task_id`, `attempt_no` and `worker_id`. No metrics or tracing yet (Phase 6).

## Deviations from the approved plan (found during implementation)

1. **File copy uses a tar streamed over `exec` stdin, not `put_archive`.** Docker rejects `put_archive` when the rootfs is read-only, and keeping the read-only rootfs won out.
2. **The worker's Redis client sets `socket_timeout` longer than its `XREADGROUP BLOCK`.** redis-py 8 defaults it to 5 s, which broke 5 s blocking reads.
3. **`watchfiles` is an explicit worker dependency.** The plan assumed it came with `uvicorn[standard]`, which only the API installs.
4. **`runs.error_message` column.** A run that fails before any attempt exists (`CLAIMED → FAILED`) still records why.
5. **The orphan sweep is deadline-based** (an `agenteval.deadline` label), so it is safe with several workers sharing one Docker daemon.
