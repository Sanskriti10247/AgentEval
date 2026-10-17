# ADR-011: Platform benchmarks — method, harness and reporting

**Status:** Accepted (Phase 7).

Related: ADR-004 (modular monolith + workers), ADR-009 (reliability), ADR-010 (metrics).

## Context

Phase 4's "benchmark evaluations" measure *agents*. Phase 7 measures the *platform*: how many runs per second the API → Redis → worker → sandbox → Postgres path sustains, where time goes, and how throughput scales with workers. The project spec asks for throughput at 10/100/500/1000 tasks, worker scaling at 1/2/5/10/25 workers, and p50/p95, queue latency, execution latency, success rate, retry rate, CPU and memory — with reproducible scripts and never a fabricated number.

## Decision

### Harness: `benchmarks/` (Python, no new runtime dependency)

- `benchmarks/run.py` drives one **scenario** against the running compose stack over the public HTTP API, then reads timings back from Postgres (the source of truth) — never from client-side guesses:
  - **queue latency** = `claimed_at − created_at` of the first claim;
  - **start latency** = `started_at − claimed_at`;
  - **execution latency** = attempt `finished_at − started_at`;
  - **end-to-end latency** = run `finished_at − created_at`;
  - **throughput** = finished runs / (last `finished_at` − first `created_at`);
  - success rate (COMPLETED with the expected outcome / total), retry rate (runs with `attempt_count > 1`), failures by reason.
- Resource usage: a sampler polls `docker stats --no-stream` every second for the api, worker(s), postgres, redis and sandbox containers (CPU %, memory), reporting mean and peak per role.
- Worker count is set with `docker compose up -d --scale worker=N` and confirmed through `GET /workers` before the scenario starts. Each run starts from an empty database (`TRUNCATE` via a dedicated `benchmarks/reset.py`, refused unless `AGENTEVAL_BENCH_ALLOW_RESET=1`).
- Output: one JSON result file per scenario (`benchmarks/results/<timestamp>-<scenario>.json`, including the git SHA, settings, machine info and raw per-run timings) and `benchmarks/report.py` rendering a Markdown table set into `docs/benchmarks/`.

### Workloads

- **`noop-command`**: a command task running `true` in the sandbox image — isolates the platform path (dispatch, claim, container create/start/remove, persist).
- **`fake-agent`**: an agent task with the scripted fake provider (a few tool calls, deterministic, no network, no cost) plus a final-answer evaluation — exercises the agent loop, trajectory writes, artifact save and grading.
- No live model calls, ever (cost and nondeterminism).

### Matrix

- Throughput: 10, 100, 500, 1000 runs at a fixed worker setup (2 workers × concurrency 2), both workloads.
- Worker scaling: 1, 2, 5, 10 workers (concurrency 2) with 200 `noop-command` runs. 25 workers is attempted only if the host has the headroom (each sandbox reserves its task memory); if not, it is reported as skipped with the reason, not extrapolated.
- Each cell is run 3 times; the report shows the median and the spread.

### Reporting rules

- Every number in `docs/benchmarks/` comes from a result file in the same commit, produced by the committed scripts, on a machine described in the report (CPU, RAM, Docker version, other load).
- Bottlenecks are stated only when the data shows them (e.g. container start time dominating execution latency).

## Consequences

- Benchmarks run on a developer laptop shared with other containers: absolute numbers are indicative, trends are the point. The report says so.
- The harness lives outside the test suite (not run in CI); a tiny smoke scenario (5 runs) is included in `make bench-smoke` to keep it from rotting.

## Alternatives considered

| Option | Why not |
|---|---|
| Locust / k6 | They measure request latency; our latency of interest is asynchronous (queue → finish) and lives in Postgres timestamps |
| Prometheus queries as the source | Fine for live views; per-run timestamps in Postgres are exact and reproducible offline |
| Live model workloads | Cost, rate limits and nondeterminism make results unrepeatable |
