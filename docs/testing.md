# Testing

The README has the overview of the test layers and the `make` targets. This page covers what each phase added, how CI is split, how the end-to-end suite is configured, and how the timing-sensitive tests were made deterministic.

The standard suite uses only the fake model provider and makes no real model calls. Its one `live` test, `apps/worker/tests/test_anthropic_live.py`, is skipped unless both `AGENTEVAL_LIVE_ANTHROPIC=1` and `ANTHROPIC_API_KEY` are set and it is selected with `-m live`; it makes one small real request, which costs money.

## Continuous integration

`.github/workflows/ci.yml` runs on every push and pull request:

| Job | What it runs |
| --- | --- |
| `lint` | ruff, the format check, and mypy (including `benchmarks`) |
| `unit` | no Docker: the core tests, the bounded-buffer test and the benchmark-harness tests |
| `integration` | the full `pytest` run on a runner with Docker, after building the sandbox image |
| `web` | `npm ci`, typecheck, lint, test, build |
| `audit` | `pip-audit` on the locked Python dependencies and `npm audit --omit=dev --audit-level=high` |
| `images` | builds the api, worker, web and sandbox images without pushing them |

`e2e.yml` runs the compose-stack suite (`make up && make migrate && make e2e`) on pushes to `main`, nightly, and on demand. `release.yml` publishes images on version tags. `make ci-local` runs the Docker-free checks locally.

## Coverage by area

The original vertical slice's tests still run unchanged: the state machine, idempotency, claim races, fencing, the sandbox controls and the command-task E2E.

### Agent runtime (Phase 3)

- Runtime unit tests, with the fake provider and a scripted workspace: every way a run can end, retries and `retry-after`, a deadline versus a provider fault, cancellation during a model call, a backoff or a tool call, event order and save batching, budgets, and truncation handling.
- Sandbox session tests in real Docker: every case from the §15 spike, including background children and `setsid` escapees, before and after the reaper; the pids limit with and without the reaper; the backstop; and reaper failure.
- Tool tests: unit tests against a fake workspace, and integration tests in a real sandbox, including the fix-the-bug loop, an injection attempt, timeouts and state isolation.
- Anthropic provider tests against a stubbed client with real SDK types and exceptions: the request shape per model, strict-compatible schemas, verbatim thinking replay and 16 error mappings, plus a full agent run through the provider.
- Integration tests with real Postgres, Redis and Docker: agent runs through the real executor, each outcome persisted, writer fencing, idempotency and retries, ownership lost mid-run, cancellation at the shutdown deadline, and a command task alongside them.
- API and migration tests: agents and versions (idempotency, concurrency, every validation rule), run pairing rules and trajectory paging. The migrated schema matches the models exactly, S1 data survives `0002`, and the downgrade refuses while agent data exists.
- E2E: agent fix-the-bug and max-steps runs over real HTTP, alongside the command-task E2E.

### Evaluation (Phase 4)

The full list, including the S1–S9 benchmark scenarios, is in [architecture/evaluation.md](architecture/evaluation.md#6-verification). It comprises unit and property tests for the grading domain; a sandbox security suite in real Docker (archive traversal, hidden-file isolation, no network, a read-only `/eval`, reaping, snapshot completeness, and the C2 grading attacks); runner integration tests for every evaluation outcome, evaluator failures, duplicate evaluations and the invariant that evaluation never changes the attempt; and API tests for datasets, idempotent batches, results, summary and compare, plus the D11 response-model snapshot and route sweep.

### Reliability (Phase 5)

- Unit tests for the state-machine tables, the failure taxonomy and its CHECK lists, the retry policy, backoff, lease timing and the keeper protocol.
- Integration tests with real Postgres, Redis and Docker: every transition and constraint, fencing of stale owners, lock behaviour (recovery never waits), each reconciler step, a worker killed mid-run and recovered by another, artifact save and sweep races, evaluation recovery, and the cancel, replay, run-listing and budget APIs.
- E2E scenarios on the compose stack (`tests/e2e/test_reliability_e2e.py`): worker SIGKILL mid-run and mid-grading, a transient model failure retried with backoff, run and batch cancellation, dead letter and replay, and duplicate dispatch.

### Metrics (Phase 6)

- Unit tests: every metric moves on its event, labels collapse into closed sets, a broken registry never raises into the caller, and no label value looks like an id.
- API tests: the `/metrics` exposition, route-template labels, `unmatched`, the endpoint excluded from its own numbers, and a 200 with both sources down.
- Integration tests with real Postgres, Redis and Docker: the scrape-time gauges against seeded rows and streams, a broken Redis or Postgres counted without failing the scrape, and the executor, reconciler and evaluation runner moving the run, model, tool and evaluation metrics.

### Authentication, scopes and rate limits (Phase 8)

- Unit tests: key format and parsing, constant-time digest comparison, scope parsing, and log redaction of `ae_...` keys, bearer text and `Authorization` headers.
- Integration tests with real Postgres and Redis: the key lifecycle (create returns the plaintext once, only a digest is stored, revoke, expiry, `last_used_at` written at most once a minute), the CLI, and migration `0010` (constraints, and a downgrade that refuses while keys exist). A 401/403 matrix runs over every `/api/v1` route found in the app, and a sweep fails when a route has no scope dependency or the OpenAPI schema lacks its security requirement. The key endpoints, authentication disabled, and the rate limiter are covered too: 429 and its headers, separate read and write budgets, per-key counters, window reset, atomicity under concurrency, and Redis being down (the request is allowed and counted).
- Fixtures: the integration `client` sends a fresh admin key. The e2e helpers and `benchmarks/run.py` use `AGENTEVAL_E2E_API_KEY` / `AGENTEVAL_BENCH_API_KEY`, or create a short-lived key through the CLI in the `api` container.
- E2E (`tests/e2e/test_auth_e2e.py`): the running stack refuses keyless requests, a `read` key cannot write, and revocation takes effect at once.
- Dashboard: key storage, the bearer header, a 401 returning to the key screen, Sign out, and the 403 message on cancel and replay.

### Security review

These came with the [security review](security-review.md):

- The sandbox, inspected from the inside in real Docker: `NoNewPrivs` and seccomp filter mode are on; no worker environment variable (including the model key and database URL) is visible, even in PID 1's environment; there is no Docker socket; `postgres`, `redis` and `api` do not resolve; every writable mount is `noexec,nosuid,nodev`; and a worker refuses an image outside its allowlist.
- Hostile input: an oversize body is a 413 before it is parsed (with a declared length, chunked, and through the real app, and counted in the metrics); a NUL character is a fixed-text 422 and the API keeps working; and the dashboard's security headers are pinned.

## End-to-end configuration

`make e2e` needs a running stack (`make up && make migrate`). A full run takes a few minutes and needs a Docker host that isn't short of memory.

The suite sends `AGENTEVAL_E2E_API_KEY` if it is set. Otherwise it creates one key per session with `docker compose exec -T api python -m agenteval_api.keys create` (admin scopes, expiring in a day), so `make e2e` stays self-contained.

Before the tests, `make e2e` runs `docker compose -f docker-compose.yml -f docker-compose.e2e.yml up -d --no-deps --wait worker`, which gives the worker a 15 s lease, a 2 s heartbeat and a 1 s reconcile tick (values that still pass startup validation). The crash scenarios then recover in seconds, and the older scenarios behave the same. These timings are for this suite only; run `make up` (or `docker compose up -d worker`) afterwards to go back to the 30 s defaults. The crash scenarios call `docker compose kill -s SIGKILL worker` and `docker compose up -d worker` with both files, so the timings survive the restart.

| Variable | Default | Used by |
| --- | --- | --- |
| `AGENTEVAL_E2E_URL` | `http://localhost:8000` | every scenario |
| `AGENTEVAL_E2E_REDIS_URL` | `redis://127.0.0.1:56379/0` | the duplicate-dispatch scenario |
| `AGENTEVAL_E2E_DATABASE_URL` | `postgresql://agenteval:agenteval@127.0.0.1:55432/agenteval` | the invariant scenario, through a SELECT-only probe; skipped if the database is unreachable |

## Timing-sensitive tests

Three tests used to fail intermittently. Each now waits for the condition it asserts instead of racing it:

- `test_backstop_kills_the_container_if_timeout_never_fires` waits for the daemon to report the killed container as not running, because `docker kill` only delivers the signal.
- `test_memory_limit_oom` was fixed in the code: after an exec exits 137, the sandbox waits up to 3 s for Docker to record `OOMKilled`, so a real OOM kill is no longer reported as `oom_killed=false`.
- `test_drain_deadline_cancels_agent_runs_and_records_why` starts its drain only once the agent is really in its first model call (the `run_started` event), not as soon as the attempt row exists while the container is still being created.
