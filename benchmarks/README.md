# Platform benchmarks

Harness for the Phase 7 measurements (method: [ADR-011](../docs/decisions/ADR-011-benchmarking.md),
results: [docs/benchmarks](../docs/benchmarks/README.md)). It drives the running compose stack over
HTTP and reads every timing back from Postgres.

**It wipes data.** Each scenario truncates all data tables (runs, tasks, agents, evaluations, ...;
never the schema or the `workers` registry) and rescales the compose `worker` service. Use a stack
you do not mind emptying. `benchmarks/reset.py` refuses to run unless `AGENTEVAL_BENCH_ALLOW_RESET=1`;
the make targets set it for you. The machine should be otherwise idle: no `pytest`, observability
profile off (`make observability-down`).

## Reproduce

```bash
make up && make migrate
make bench-smoke            # 5 noop runs on 1 worker; seconds
make bench                  # the whole matrix, then renders docs/benchmarks/README.md
```

`make bench` runs, per ADR-011 (3 repeats per cell; each repeat starts from empty tables):

| Suite | Workload | Runs | Workers x slots |
|---|---|---|---|
| throughput | `noop-command`, `fake-agent` | 10, 100, 500, 1000 | 2 x 2 |
| scaling | `noop-command` | 200 | 1, 2, 5, 10 x 2 (25 x 2 only if memory allows) |

A single scenario:

```bash
AGENTEVAL_BENCH_ALLOW_RESET=1 uv run python -m benchmarks.run \
    --workload fake-agent --runs 100 --workers 2 --concurrency 2 --repeat 3 --label throughput
```

`benchmarks.matrix` also takes `--repeats N`, `--big-repeats N` (the 1000-run cells) and
`--only throughput|scaling`.

## Files

| File | Role |
|---|---|
| `run.py` | CLI: scale workers, reset, create the task, submit concurrently, wait, write one result JSON per repeat |
| `matrix.py` | The full matrix, including the 25-worker memory gate |
| `reset.py` | `TRUNCATE` of the data tables, guarded by `AGENTEVAL_BENCH_ALLOW_RESET=1` |
| `sampler.py` | `docker stats --no-stream` loop: per-role mean/peak CPU % and memory |
| `metrics.py` | Timing queries and the percentile / throughput maths |
| `report.py` | Renders `docs/benchmarks/README.md` and the scaling SVG from `results/*.json` |
| `results/` | One JSON per scenario repeat: git SHA, settings, machine info, raw per-run timings (columnar), summary |

The database is reached through `AGENTEVAL_DATABASE_URL` (default: the host port 55432 from
`.env.example`), the API through `--base-url` / `AGENTEVAL_E2E_URL` (default `http://localhost:8000`).

## Tests

Pure unit tests (percentiles, throughput, sampler parsing, report rendering); no Docker needed:

```bash
uv run pytest benchmarks/tests
```

They are not in the default `testpaths` of the platform suite.

## Notes on the numbers

- Absolute numbers depend on the machine and on what else runs on it; every result file records the
  CPU, memory, Docker version, load and the names of the other containers running on the host.
- `docker stats` takes about two seconds per call, so resource samples arrive about every 2 s.
  Sandboxes live about a second and `docker stats` reported 0 for every one it saw, so the sandbox
  rows are zeros and carry no information.
- A result with `"status": "incomplete"` timed out waiting for runs; `"skipped"` records why a cell
  was not run. Neither is ever filled in with estimates.
