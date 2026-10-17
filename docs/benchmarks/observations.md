Each statement below is read off the tables above (arithmetic on table values is shown). Where the
data does not settle a question, it says so.

**Every run in every cell succeeded.** Success rate is 1.000 and retry rate 0.000 in all 39 result
files (`max_attempts` = 1, so a retry could not have hidden anything); there are no skipped cells.

**Where the time goes: queueing behind a burst, not the platform's own steps.**
- Start latency (claim to attempt running) is about 0.01 s at p50 in every cell (0.009 s noop,
  0.010 s fake-agent at 1000 runs). Grading of the `fake-agent` runs is 0.050 s at p50 at 1000 runs.
  Exec latency is about one second (1.036 s noop, 1.277 s fake-agent at 1000 runs, 2 x 2 slots).
- The harness submits every run in a burst (1000 runs accepted in 10.77 s, client side, noop) while
  the platform finishes about 4 per second, so queue latency is the backlog: queue p50 is 14.3 s,
  59.9 s and 118.0 s for 100, 500 and 1000 noop runs, and 15.7 s, 78.8 s and 163.0 s for
  `fake-agent`. At 1000 runs queue p50 is 118.0 s of an end-to-end p50 of 119.1 s (noop) and 163.0 s
  of 164.4 s (fake-agent). These are open-loop (burst) latencies; they would be far smaller with
  paced submission.

**Throughput is flat once there is a backlog.** With 2 workers x 2 slots: noop 3.84, 4.09 and 3.98
runs/s at 100, 500 and 1000 runs; fake-agent 2.87, 2.99 and 3.00. The 10-run cells are lower for
noop (3.25) because the run is short relative to the ramp. The noop 100-run cell has a wide spread
(2.46-4.27 runs/s); the host load average at the start of its three repeats was 2.8, 3.9 and 5.2,
but the data does not establish that as the cause.
- `fake-agent` is slower than `noop-command` (3.00 vs 3.98 runs/s at 1000 runs). Its exec latency is
  longer (1.277 s vs 1.036 s) and grading adds only 0.050 s per run. With 4 slots, 4 / 1.036 s =
  3.86 runs/s and 4 / 1.277 s = 3.13 runs/s, close to the measured 3.98 and 3.00: the slots are
  busy all the time and throughput is slots divided by exec latency.

**Worker scaling stops being linear immediately and flattens at about 4-5 runs/s** (200 noop runs,
2 slots per worker):

| Workers | Throughput (runs/s) | Speedup | Efficiency | Exec p50 (s) |
|---|---|---|---|---|
| 1 | 3.11 | 1.00 | 100% | 0.685 |
| 2 | 4.03 | 1.30 | 65% | 1.046 |
| 5 | 4.25 | 1.37 | 27% | 2.321 |
| 10 | 4.90 | 1.58 | 16% | 3.722 |
| 25 | 4.41 | 1.42 | 6% | 10.988 |

- Adding workers beyond 2 buys little (+5% from 2 to 5, +15% from 5 to 10, from medians), and 25
  workers is not better than 10: the throughput ranges overlap (4.64-5.33 at 10, 4.15-4.89 at 25).
- The *Slots / exec p50* column tracks the measured throughput in every row (2.92, 3.82, 4.31, 5.37,
  4.55 against 3.11, 4.03, 4.25, 4.90, 4.41): all slots stay busy, but each sandbox takes longer
  the more of them run at once (exec p50 grows from 0.685 s to 10.988 s). Total container
  throughput is capped, not the number of workers.
- At the point where linearity ends (2 workers) the measured processes are nowhere near saturated:
  mean CPU (percent of one core) is 9.5 for all workers together, 5.5 for Postgres and 4.6 for the
  API, and the host's lowest MemAvailable is 4830 MiB. At 10 workers: workers 20.2, Postgres 12.4,
  API 7.2, MemAvailable minimum 3552 MiB. At 25 workers: workers 45.0, Postgres 14.2, API 5.0,
  MemAvailable minimum 3413 MiB (2540 MiB in the worst repeat).
- What is shared and saturated is therefore not visible in the sampled containers. The data is
  consistent with the Docker daemon's container create/start/remove path (or the host CPU: the
  12-thread laptop's load average at scenario start was 4.5-9.0 for the 1-10 worker cells) being
  the limit, but the daemon was not sampled, so this is a hypothesis, not a measured finding.

**Resource footprint** (medians; summed over replicas): worker memory is 137, 278, 732 and 1486 MiB
at 1, 2, 5 and 10 workers (about 150 MiB per worker), 1532 MiB mean and 1745 MiB peak at 25 workers
(sampled while the workers were still starting up). Postgres memory grows from 57 MiB (1 worker)
to 150 MiB (10) and 234 MiB (25); the API stays at 81-86 MiB in the scaling cells. The API's CPU is idle on average
(3-8% of one core) but peaks at 94-98% of one core during the submit burst of the 500- and 1000-run
cells (single uvicorn process); Postgres peaks at 38-52% of a core and Redis peaks at no more than 11%.
Sandbox CPU and memory could not be measured (see Limitations).

**The 25-worker cell ran.** With 10 workers up the host's lowest MemAvailable was 3546 MiB, above
the 2048 MiB floor, and 25 workers measured 2540 MiB at worst, so the cell was run; it was not
skipped.
