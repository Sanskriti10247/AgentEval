"""Render docs/benchmarks/README.md (and the scaling chart) from the result JSONs.

    uv run python -m benchmarks.report

Every number comes from a file in benchmarks/results/. A cell's value is the
median over its repeats, with the min-max spread in parentheses. If
docs/benchmarks/observations.md exists it is included verbatim under
"Observations" (hand-written, and only about numbers in the tables above it).
"""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
RESULTS = Path(__file__).resolve().parent / "results"
DOCS = ROOT / "docs" / "benchmarks"
ROLES = ("api", "worker", "postgres", "redis", "sandbox")

Cell = tuple[str, str, int, int, int]  # label, workload, runs, workers, concurrency


def load_results(directory: Path = RESULTS) -> list[dict[str, Any]]:
    return [json.loads(p.read_text()) for p in sorted(directory.glob("*.json"))]


def cell_key(r: dict[str, Any]) -> Cell:
    return (r["label"], r["workload"], r["runs"], r["workers"], r["concurrency"])


def group(results: Sequence[dict[str, Any]]) -> dict[Cell, list[dict[str, Any]]]:
    cells: dict[Cell, list[dict[str, Any]]] = defaultdict(list)
    for r in results:
        cells[cell_key(r)].append(r)
    return dict(cells)


def pick(r: dict[str, Any], path: str) -> float | None:
    """Dotted lookup into a result; None when any part is missing."""
    cur: Any = r
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur or cur[part] is None:
            return None
        cur = cur[part]
    return float(cur)


def stat(rs: Sequence[dict[str, Any]], path: str) -> tuple[float, float, float, int] | None:
    """(median, min, max, n) over the repeats that have the value."""
    vals = [v for v in (pick(r, path) for r in rs) if v is not None]
    if not vals:
        return None
    return statistics.median(vals), min(vals), max(vals), len(vals)


def fmt(s: tuple[float, float, float, int] | None, digits: int = 2, scale: float = 1.0) -> str:
    if s is None:
        return "n/a"
    med, lo, hi, n = s
    text = f"{med * scale:.{digits}f}"
    if n > 1:
        text += f" ({lo * scale:.{digits}f}-{hi * scale:.{digits}f})"
    return text


def col(
    path: str, digits: int = 2, scale: float = 1.0
) -> Callable[[Sequence[dict[str, Any]]], str]:
    return lambda rs: fmt(stat(rs, path), digits, scale)


def table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(out)


LAT = "summary.latency_s"
PCT = lambda k, p: col(f"{LAT}.{k}.{p}", 3)  # noqa: E731  (seconds, 3 decimals)


def ok_cells(cells: dict[Cell, list[dict[str, Any]]], label: str, workload: str | None = None):
    for key in sorted(cells, key=lambda k: (k[1], k[3], k[2])):
        if key[0] != label or (workload and key[1] != workload):
            continue
        rs = [r for r in cells[key] if r.get("status") in ("ok", "incomplete")]
        if rs:
            yield key, rs


def perf_tables(cells: dict[Cell, list[dict[str, Any]]], label: str, workload: str, x: int) -> str:
    """Throughput + latency + resource tables for one workload; x is the varying column
    (2 = runs, 3 = workers)."""
    items = list(ok_cells(cells, label, workload))
    if not items:
        return "_no results_\n"
    name = "Runs" if x == 2 else "Workers"
    graded = workload == "fake-agent"
    rows1, rows2, rows3, rows4 = [], [], [], []
    for key, rs in items:
        n = str(len(rs))
        bad = sum(1 for r in rs if r["status"] != "ok")
        label_x = str(key[x]) + (f" ({bad} incomplete)" if bad else "")
        rows1.append([label_x, n, col("summary.throughput_rps")(rs)]
                     + ([col("summary.graded_throughput_rps")(rs)] if graded else [])
                     + [col("summary.success_rate", 3)(rs), col("summary.retry_rate", 3)(rs),
                        col("wall.submit_s", 2)(rs)])  # fmt: skip
        rows2.append([label_x]
                     + [f"{PCT(k, 'p50')(rs)} / {PCT(k, 'p95')(rs)}"
                        for k in ("queue", "start", "exec", "e2e")]
                     + ([f"{PCT('grade', 'p50')(rs)} / {PCT('grade', 'p95')(rs)}"] if graded else []))  # fmt: skip
        rows3.append([label_x] + [
            f"{col(f'resources.{r}.cpu_mean', 1)(rs)} / {col(f'resources.{r}.cpu_peak', 1)(rs)}"
            for r in ROLES])  # fmt: skip
        rows4.append([label_x] + [
            f"{col(f'resources.{r}.mem_mean_mib', 0)(rs)} / {col(f'resources.{r}.mem_peak_mib', 0)(rs)}"
            for r in ROLES] + [col("resources.host.mem_available_min_mib", 0)(rs),
                               col("resources.samples", 0)(rs), col("resources.errors", 0)(rs),
                               col("resources.interval_s", 1)(rs),
                               col("resources.sandbox.present", 0)(rs)])  # fmt: skip
    h1 = [name, "Repeats", "Throughput (runs/s)"] + (["Throughput incl. grading (runs/s)"] if graded else []) \
        + ["Success rate", "Retry rate", "Submit phase (s, client)"]  # fmt: skip
    h2 = [name] + [f"{k} p50 / p95 (s)" for k in ("Queue", "Start", "Exec", "End-to-end")] \
        + (["Grading p50 / p95 (s)"] if graded else [])  # fmt: skip
    roles = [f"{r}" for r in ROLES]
    return "\n\n".join([
        "**Throughput and rates**\n\n" + table(h1, rows1),
        "**Latency** (queue = claim - create, start = claim to attempt running, exec = attempt "
        "duration, end-to-end = create to terminal)\n\n" + table(h2, rows2),
        "**CPU %, mean / peak** (Docker CPU %: 100 = one core; roles are summed over replicas)\n\n"
        + table([name] + roles, rows3),
        "**Memory MiB, mean / peak** (roles summed over replicas), host MemAvailable minimum, and "
        "sampler samples\n\n"
        + table([name] + roles + ["Host MemAvailable min (MiB)", "Samples", "Failed samples",
                                    "Mean interval (s)", "Samples with a sandbox"], rows4),
    ]) + "\n"  # fmt: skip


def scaling_svg(points: list[tuple[int, float, float, float]]) -> str:
    """Throughput vs workers: (workers, median, min, max), with the ideal-linear line from
    the 1-worker median. Plain SVG, no dependencies."""
    w, h, left, bottom, top, right = 640, 380, 64, 52, 24, 24
    xmax = max(p[0] for p in points)
    first = next((p for p in points if p[0] == 1), None)
    ideal = [(p[0], first[1] * p[0]) for p in points] if first else []
    ymax = max([p[3] for p in points] + [v for _, v in ideal]) * 1.1

    def sx(x: float) -> float:
        return left + (w - left - right) * x / xmax

    def sy(y: float) -> float:
        return h - bottom - (h - bottom - top) * y / ymax

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
           'font-family="sans-serif" font-size="12">',
           '<rect width="100%" height="100%" fill="white"/>',
           f'<line x1="{left}" y1="{h - bottom}" x2="{w - right}" y2="{h - bottom}" stroke="#333"/>',
           f'<line x1="{left}" y1="{top}" x2="{left}" y2="{h - bottom}" stroke="#333"/>']  # fmt: skip
    for i in range(5):
        v = ymax * i / 4
        out.append(
            f'<line x1="{left}" y1="{sy(v):.1f}" x2="{w - right}" y2="{sy(v):.1f}" stroke="#ddd"/>'
        )
        out.append(f'<text x="{left - 6}" y="{sy(v) + 4:.1f}" text-anchor="end">{v:.1f}</text>')
    for p in points:
        out.append(
            f'<text x="{sx(p[0]):.1f}" y="{h - bottom + 18}" text-anchor="middle">{p[0]}</text>'
        )
    if ideal:
        pts = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in ideal)
        out.append(f'<polyline points="{pts}" fill="none" stroke="#999" stroke-dasharray="5 4"/>')
    pts = " ".join(f"{sx(p[0]):.1f},{sy(p[1]):.1f}" for p in points)
    out.append(f'<polyline points="{pts}" fill="none" stroke="#1f6feb" stroke-width="2"/>')
    for x, med, lo, hi in points:
        out.append(
            f'<line x1="{sx(x):.1f}" y1="{sy(lo):.1f}" x2="{sx(x):.1f}" y2="{sy(hi):.1f}" stroke="#1f6feb"/>'
        )
        out.append(f'<circle cx="{sx(x):.1f}" cy="{sy(med):.1f}" r="4" fill="#1f6feb"/>')
    out.append(
        f'<text x="{(left + w - right) / 2}" y="{h - 12}" text-anchor="middle">workers (2 slots each)</text>'
    )
    out.append(
        f'<text x="16" y="{(top + h - bottom) / 2}" text-anchor="middle" '
        f'transform="rotate(-90 16 {(top + h - bottom) / 2})">throughput (runs/s)</text>'
    )
    out.append(
        f'<text x="{w - right}" y="{top + 8}" text-anchor="end" fill="#666">dashed: linear from 1 worker; bars: min-max of repeats</text>'
    )
    out.append("</svg>")
    return "\n".join(out) + "\n"


def scaling_section(cells: dict[Cell, list[dict[str, Any]]]) -> tuple[str, str | None]:
    items = list(ok_cells(cells, "scaling", "noop-command"))
    if not items:
        return "_no results_\n", None
    base = next((stat(rs, "summary.throughput_rps") for k, rs in items if k[3] == 1), None)
    rows = []
    points = []
    for key, rs in items:
        s = stat(rs, "summary.throughput_rps")
        speed = eff = "n/a"
        if s and base:
            speed = f"{s[0] / base[0]:.2f}"
            eff = f"{s[0] / base[0] / key[3] * 100:.0f}%"
        if s:
            points.append((key[3], s[0], s[1], s[2]))
        exec_p50 = stat(rs, f"{LAT}.exec.p50")
        little = f"{key[3] * key[4] / exec_p50[0]:.2f}" if exec_p50 else "n/a"
        rows.append([str(key[3]), str(len(rs)), fmt(s), speed, eff, little,
                     f"{PCT('queue', 'p50')(rs)} / {PCT('queue', 'p95')(rs)}",
                     f"{PCT('exec', 'p50')(rs)} / {PCT('exec', 'p95')(rs)}",
                     f"{PCT('e2e', 'p50')(rs)} / {PCT('e2e', 'p95')(rs)}",
                     col("summary.success_rate", 3)(rs),
                     col("resources.worker.cpu_mean", 1)(rs), col("resources.postgres.cpu_mean", 1)(rs),
                     col("resources.api.cpu_mean", 1)(rs),
                     col("resources.host.mem_available_min_mib", 0)(rs)])  # fmt: skip
    head = ["Workers", "Repeats", "Throughput (runs/s)", "Speedup vs 1", "Efficiency",
            "Slots / exec p50 (runs/s)",
            "Queue p50 / p95 (s)", "Exec p50 / p95 (s)", "End-to-end p50 / p95 (s)", "Success",
            "Worker CPU % mean", "Postgres CPU % mean", "API CPU % mean",
            "Host MemAvailable min (MiB)"]  # fmt: skip
    text = (
        table(head, rows) + "\n\nSpeedup is the cell's median throughput over the 1-worker median; "
        "efficiency is speedup divided by the worker count. "
        "*Slots / exec p50* is workers x slots divided by the median exec latency: the throughput "
        "that would result if every slot were always busy running a sandbox of the median "
        "duration.\n"
    )
    return text, scaling_svg(points) if points else None


def skipped_section(results: Sequence[dict[str, Any]]) -> str:
    skipped = [r for r in results if r.get("status") == "skipped"]
    if not skipped:
        return "None.\n"
    rows = [[r["label"], r["workload"], str(r["runs"]), f"{r['workers']} x {r['concurrency']}", r["reason"]]
            for r in skipped]  # fmt: skip
    return table(["Suite", "Workload", "Runs", "Workers x slots", "Reason"], rows) + "\n"


def machine_section(results: Sequence[dict[str, Any]]) -> str:
    ran = [r for r in results if "machine" in r]
    if not ran:
        return "_no results_\n"
    m = ran[0]["machine"]
    avail = [r["machine"]["mem_available_mib"] for r in ran]
    others = sorted({c for r in ran for c in r["machine"]["other_containers_running"]})
    shas = sorted({(r["git"]["sha"][:10], r["git"]["dirty"]) for r in ran})
    started = sorted(r["started_at"] for r in ran)
    load = [r["machine"]["load_avg"][0] for r in ran]
    lines = [
        f"- CPU: {m['cpu_model']}, `nproc` = {m['nproc']}",
        f"- RAM: {m['mem_total_mib']} MiB total; MemAvailable at scenario start ranged "
        f"{min(avail)}-{max(avail)} MiB; swap {m['swap_total_mib']} MiB",
        f"- Kernel {m['kernel']}; Docker server {m['docker_server_version']}",
        f"- Host 1-minute load average at scenario start: {min(load):.2f}-{max(load):.2f}",
        "- Code: "
        + ", ".join(
            f"`{s}`" + (" (working tree had uncommitted changes)" if d else "") for s, d in shas
        ),
        f"- Run window: {started[0]} to {started[-1]} (UTC)",
        "- Stack: the repository's `docker-compose.yml` as is (api with `--reload`, worker under "
        "`watchfiles`, Postgres 16, Redis 7), observability profile off; "
        "worker settings per scenario are stored in each result file",
        "- **Other containers running on the host during the benchmarks (unrelated stacks, not "
        "controlled by us):** " + (", ".join(f"`{c}`" for c in others) or "none"),
    ]
    return "\n".join(lines) + "\n"


METHOD = """\
Defined in [ADR-011](../decisions/ADR-011-benchmarking.md); reproduce with
[`benchmarks/README.md`](../../benchmarks/README.md).

- A *scenario* = one workload x run count x worker setup. The harness scales the compose `worker`
  service to the requested count, waits until exactly that many are alive in `GET /api/v1/workers`,
  empties the data tables, creates one task, submits all runs concurrently over HTTP
  (`max_attempts` = 1, so a retry can never hide a failure), waits until every run is terminal (and,
  for `fake-agent`, graded), then reads the timings from Postgres.
- Workloads: `noop-command` runs `true` in `agenteval-sandbox:py312` (128 MB, 30 s limit) and so
  exercises dispatch, claim, container create/start/remove and persistence. `fake-agent` runs the
  scripted fake model (list_dir, read_file, final answer; no network, no cost) with a final-answer
  evaluation, and so adds the agent loop, trajectory writes and grading.
- Latencies come from server-side timestamps in Postgres: queue = `claimed_at - created_at`,
  start = `started_at - claimed_at`, exec = attempt `finished_at - started_at`, end-to-end =
  `finished_at - created_at`, grading = evaluation `finished_at - attempt finished_at`.
  Percentiles use linear interpolation between closest ranks. Throughput = finished runs /
  (last `finished_at` - first `created_at`).
- Success = run COMPLETED with the workload's expected outcome (and, for `fake-agent`, a SUCCEEDED
  evaluation with `task_success`). Retry rate = runs with more than one attempt.
- Resource use: `docker stats --no-stream` of the api, worker, postgres and redis containers and of
  any sandbox container, summed per role. One `docker stats` call takes about two seconds, so the
  cadence is about 2 s, not 1 s (the actual mean interval and sample count are stored per result).
- Each cell has its repeats listed in its table's *Repeats* column; values are the median over the
  repeats with the min-max range in parentheses.
"""

LIMITATIONS = """\
- One developer laptop, shared with the unrelated containers listed above and with whatever else the
  desktop was doing: absolute numbers are indicative, the shape of the curves is the point. The
  repeats' spread shows run-to-run noise but not noise between sessions.
- Everything runs on one Docker host, so workers, sandboxes, Postgres, Redis and the API compete for
  the same 12 logical CPUs and for the same Docker daemon. Worker "scaling" here is scaling on a
  fixed machine, not across machines.
- The api container runs `uvicorn --reload` and the workers run under `watchfiles`, as in
  `docker-compose.yml` (a development stack); a production deployment would differ.
- `docker stats` is sampled about every 2 s, and a sandbox container of these workloads lives
  about a second. The sampler did list sandbox containers in many samples (*Samples with a sandbox*,
  up to `containers_max` at once in the result files), but `docker stats` reported 0 CPU and 0
  memory for every one of them. The *sandbox* columns are therefore all zeros and say nothing about
  what sandboxes really use; only api, worker, postgres and redis are measured. The Docker daemon
  and containerd (which do the container create/start/remove work) are not sampled either.
- Docker CPU % is relative to one core and is measured over the daemon's own sampling window.
- Submission is a burst: all runs are created within the submit phase (reported per cell), so
  "queue latency" includes time waiting behind that burst, which grows with the run count.
- Workloads are tiny and deterministic (`true`; a three-turn scripted model), so they measure
  platform overhead, not the cost of real tasks or real model calls.
- Host load was not constant: the host's 1-minute load average at scenario start is listed above and
  drifted upward during the session (see the per-result `machine.load_avg`), and the 25-worker
  cells started while the 25 workers were still settling. Cells run later in the session are not
  strictly comparable with earlier ones.
- The sampler's `docker ps` + `docker stats` pair fails when a container disappears in between and
  slows down under load: *Failed samples* and *Mean interval* are listed beside every resource
  table, and the agent workloads' cells have far fewer samples than their duration would suggest.
"""


def render(
    results: list[dict[str, Any]], observations: str | None = None
) -> tuple[str, str | None]:
    cells = group(results)
    scaling_text, svg = scaling_section(cells)
    parts = [
        "# Platform benchmarks (Phase 7)",
        "Generated by `uv run python -m benchmarks.report` from the result files in "
        "`benchmarks/results/`. Every number below comes from those files; nothing is estimated.",
        "## Machine and environment\n\n" + machine_section(results),
        "## Method\n\n" + METHOD,
    ]
    for workload in ("noop-command", "fake-agent"):
        parts.append(f"## Throughput: `{workload}` (2 workers x 2 slots)\n\n"
                     + perf_tables(cells, "throughput", workload, 2))  # fmt: skip
    parts.append("## Worker scaling: `noop-command`, 200 runs, 2 slots per worker\n\n" + scaling_text
                 + ("\n![Throughput vs workers](worker-scaling.svg)\n" if svg else "")
                 + "\n" + perf_tables(cells, "scaling", "noop-command", 3))  # fmt: skip
    parts.append("## Skipped cells\n\n" + skipped_section(results))
    if observations:
        parts.append("## Observations\n\n" + observations.strip() + "\n")
    parts.append("## Limitations\n\n" + LIMITATIONS)
    return "\n".join(parts), svg


def main() -> None:
    results = load_results()
    obs_path = DOCS / "observations.md"
    observations = obs_path.read_text() if obs_path.exists() else None
    text, svg = render(results, observations)
    DOCS.mkdir(parents=True, exist_ok=True)
    (DOCS / "README.md").write_text(text)
    if svg:
        (DOCS / "worker-scaling.svg").write_text(svg)
    print(f"wrote {DOCS / 'README.md'} from {len(results)} result files")


if __name__ == "__main__":
    main()
