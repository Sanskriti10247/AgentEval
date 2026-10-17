"""The full ADR-011 matrix. Wipes the benchmark data tables between scenarios.

    AGENTEVAL_BENCH_ALLOW_RESET=1 uv run python -m benchmarks.matrix            # make bench
    ... --repeats 3 --big-repeats 1                                              # shorter

Throughput: 10/100/500/1000 runs x {noop-command, fake-agent} on 2 workers x 2 slots.
Worker scaling: 1/2/5/10 workers x 2 slots, 200 noop-command runs. 25 workers is
attempted only when the 10-worker cells left at least FLOOR_MB of host memory
available AND the host still has FLOOR_MB once 25 workers are up; otherwise a
"skipped" result records the measurements that justified skipping.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from benchmarks import reset
from benchmarks.run import (
    RESULTS,
    Scenario,
    describe_result,
    git_info,
    run_repeats,
    write_result,
)

FLOOR_MB = 2048
THROUGHPUT_RUNS = (10, 100, 500, 1000)
SCALING_WORKERS = (1, 2, 5, 10)
SCALING_RUNS = 200
BIG_RUNS = 1000


def min_available_mb(paths: list[Path]) -> float | None:
    """Lowest host MemAvailable the sampler saw across the given result files."""
    lows = []
    for p in paths:
        r = json.loads(p.read_text())
        low = r.get("resources", {}).get("host", {}).get("mem_available_min_mib")
        if low is not None:
            lows.append(low)
    return min(lows) if lows else None


async def main_async(args: argparse.Namespace) -> None:
    out = Path(args.out_dir)
    started = time.monotonic()

    if args.only in ("all", "throughput"):
        for workload in ("noop-command", "fake-agent"):
            for runs in THROUGHPUT_RUNS:
                repeats = args.big_repeats if runs == BIG_RUNS else args.repeats
                scn = Scenario(workload, runs, 2, 2, "throughput", out_dir=out)
                await run_repeats(scn, repeats)
                print(f"-- elapsed {time.monotonic() - started:.0f}s", flush=True)

    if args.only in ("all", "scaling"):
        ten: list[Path] = []
        for workers in SCALING_WORKERS:
            scn = Scenario("noop-command", SCALING_RUNS, workers, 2, "scaling", out_dir=out)
            paths = await run_repeats(scn, args.repeats)
            if workers == 10:
                ten = paths
            print(f"-- elapsed {time.monotonic() - started:.0f}s", flush=True)

        scn = Scenario("noop-command", SCALING_RUNS, 25, 2, "scaling", out_dir=out,
                       min_available_mb=FLOOR_MB)  # fmt: skip
        low = min_available_mb(ten)
        if low is not None and low < FLOOR_MB:
            result = {
                "schema_version": 1, "label": "scaling", "workload": "noop-command",
                "runs": SCALING_RUNS, "workers": 25, "concurrency": 2, "repeat": 1, "repeats": 3,
                "git": git_info(), "status": "skipped",
                "reason": (f"with 10 workers running, host MemAvailable fell to {low:.0f} MiB "
                           f"(floor {FLOOR_MB} MiB); 25 workers would need more"),
                "mem_available_min_mib_with_10_workers": low,
            }  # fmt: skip
            print(describe_result(write_result(scn, 1, result)))
        else:
            await run_repeats(scn, args.repeats)

    print(f"matrix done in {time.monotonic() - started:.0f}s")


def main() -> None:
    reset.check_allowed()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--big-repeats", type=int, default=3, help="repeats for the 1000-run cells")
    p.add_argument("--only", choices=("all", "throughput", "scaling"), default="all")
    p.add_argument("--out-dir", default=str(RESULTS))
    asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    main()
