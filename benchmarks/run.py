"""Drive one benchmark scenario against the running compose stack (ADR-011).

    make up && make migrate
    AGENTEVAL_BENCH_ALLOW_RESET=1 uv run python -m benchmarks.run \\
        --workload noop-command --runs 100 --workers 2 --concurrency 2 --repeat 3 --label throughput

For each repeat: scale the worker service to `--workers` (and wait until exactly
that many are alive in GET /api/v1/workers), empty the data tables, create the
workload's task, submit `--runs` runs concurrently over HTTP, wait until all are
terminal (and, for graded workloads, graded), then read every timing back from
Postgres and write one result JSON to `--out-dir`. Runs use max_attempts=1 so a
retry can never hide a failure.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import gzip
import json
import os
import platform
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncpg
import httpx

from benchmarks import metrics, reset
from benchmarks.sampler import Sampler, compose_project, host_mem_available_mib

ROOT = Path(__file__).resolve().parent.parent
RESULTS = Path(__file__).resolve().parent / "results"
API = "/api/v1"
IMAGE = "agenteval-sandbox:py312"
SCHEMA_VERSION = 1
RAW_GZ_THRESHOLD = 1000  # per-run arrays longer than this go to a .rows.json.gz sidecar
SUBMIT_PARALLELISM = 32

WORKLOADS: dict[str, dict[str, Any]] = {
    "noop-command": {"expected_outcome": "SUCCESS", "graded": False},
    "fake-agent": {"expected_outcome": "AGENT_FINISHED", "graded": True},
}

FAKE_SCRIPT = [
    {"type": "turn", "text": "Listing the workspace.",
     "tool_calls": [{"name": "list_dir", "input": {}}]},
    {"type": "turn", "text": "Reading the notes.",
     "tool_calls": [{"name": "read_file", "input": {"path": "notes.txt"}}]},
    {"type": "turn", "text": "The answer is 42."},
]  # fmt: skip


@dataclass
class Scenario:
    workload: str
    runs: int
    workers: int
    concurrency: int
    label: str
    base_url: str = "http://localhost:8000"
    out_dir: Path = RESULTS
    min_available_mb: int | None = None


# -- environment -----------------------------------------------------------------------------


def sh(*cmd: str, env: dict[str, str] | None = None, check: bool = True) -> str:
    res = subprocess.run(
        cmd, capture_output=True, text=True, cwd=ROOT, check=check,
        env={**os.environ, **(env or {})},
    )  # fmt: skip
    return res.stdout.strip()


def git_info() -> dict[str, Any]:
    return {
        "sha": sh("git", "rev-parse", "HEAD"),
        "branch": sh("git", "rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(sh("git", "status", "--porcelain")),
    }


def machine_info(project: str) -> dict[str, Any]:
    mem: dict[str, int] = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, _, rest = line.partition(":")
            if key in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
                mem[key] = int(rest.split()[0]) // 1024
    cpu_model = ""
    with open("/proc/cpuinfo") as f:
        for line in f:
            if line.startswith("model name"):
                cpu_model = line.split(":", 1)[1].strip()
                break
    names = sh(
        "docker", "ps", "--format",
        '{{.Names}}|{{.Label "com.docker.compose.project"}}|{{.Label "agenteval.run_id"}}',
    ).splitlines()  # fmt: skip
    others = sorted(
        n.split("|")[0] for n in names if n.split("|")[1] != project and not n.split("|")[2]
    )
    return {
        "nproc": int(sh("nproc")),
        "cpu_model": cpu_model,
        "mem_total_mib": mem.get("MemTotal"),
        "mem_available_mib": mem.get("MemAvailable"),
        "swap_total_mib": mem.get("SwapTotal"),
        "swap_free_mib": mem.get("SwapFree"),
        "kernel": platform.release(),
        "docker_server_version": sh("docker", "info", "--format", "{{.ServerVersion}}"),
        "load_avg": os.getloadavg(),
        "other_containers_running": others,
        "compose_project": project,
    }


def worker_settings(project: str) -> dict[str, str]:
    """AGENTEVAL_* environment of a running worker container, as the platform sees it."""
    ids = sh(
        "docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={project}",
        "--filter", "label=com.docker.compose.service=worker",
    ).split()  # fmt: skip
    if not ids:
        return {}
    env = json.loads(sh("docker", "inspect", "-f", "{{json .Config.Env}}", ids[0]))
    return dict(sorted(e.split("=", 1) for e in env if e.startswith("AGENTEVAL_")))


# -- workers ---------------------------------------------------------------------------------


async def alive_workers(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    after: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 200}
        if after:
            params["after"] = after
        body = (await client.get(f"{API}/workers", params=params)).json()
        out += [w for w in body["workers"] if w["alive"]]
        after = body["next_after"]
        if after is None:
            return out


async def ensure_workers(
    client: httpx.AsyncClient, workers: int, concurrency: int, timeout_s: float = 300
) -> None:
    """Scale the compose worker service and wait for exactly `workers` live workers,
    all registered with `concurrency` slots."""
    env = {"AGENTEVAL_WORKER_CONCURRENCY": str(concurrency)}
    await asyncio.to_thread(
        sh, "docker", "compose", "up", "-d", "--no-deps", "--scale", f"worker={workers}", "worker",
        env=env,
    )  # fmt: skip
    deadline = time.monotonic() + timeout_s
    live: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        live = await alive_workers(client)
        if len(live) == workers and all(w["concurrency"] == concurrency for w in live):
            await asyncio.sleep(3)  # let the consumers finish joining the group
            return
        await asyncio.sleep(1)
    raise RuntimeError(
        f"wanted {workers} live workers x{concurrency}, saw "
        f"{[(w['id'], w['concurrency']) for w in live]}"
    )


# -- workload setup + submission -------------------------------------------------------------


async def create_workload(client: httpx.AsyncClient, workload: str) -> dict[str, str | None]:
    """Create the workload's task (and agent version); returns their ids."""
    tag = uuid.uuid4().hex[:8]
    if workload == "noop-command":
        body: dict[str, Any] = {
            "name": f"bench-noop-{tag}", "task_type": "command", "image": IMAGE,
            "command": ["true"], "timeout_s": 30, "memory_mb": 128,
        }  # fmt: skip
        version_id = None
    else:
        agent = await client.post(f"{API}/agents", json={"name": f"bench-fake-{tag}"})
        agent.raise_for_status()
        version = await client.post(
            f"{API}/agents/{agent.json()['id']}/versions",
            json={
                "model_provider": "fake", "model": "fake-model",
                "system_prompt": "You answer questions about the workspace.",
                "tools": ["list_dir", "read_file"], "shell_allowlist": [],
                "limits": {"max_steps": 10}, "model_settings": {"script": FAKE_SCRIPT},
            },
        )  # fmt: skip
        version.raise_for_status()
        version_id = version.json()["id"]
        body = {
            "name": f"bench-agent-{tag}", "task_type": "agent", "image": IMAGE,
            "instructions": "Read notes.txt and report the answer.",
            "files": {"notes.txt": "the answer is 42\n"},
            "timeout_s": 30, "memory_mb": 128,
            "evaluation": {"checks": [{"kind": "final_answer", "name": "answer",
                                       "match": "contains", "expected": "42"}]},
        }  # fmt: skip
    task = await client.post(f"{API}/tasks", json=body)
    if task.status_code != 201:
        raise RuntimeError(f"task creation failed: {task.status_code} {task.text}")
    return {"task_id": task.json()["id"], "agent_version_id": version_id}


async def submit_all(
    client: httpx.AsyncClient, ids: dict[str, str | None], runs: int
) -> dict[str, Any]:
    """POST `runs` runs concurrently. Client-side numbers describe only the submit phase."""
    sem = asyncio.Semaphore(SUBMIT_PARALLELISM)
    body: dict[str, Any] = {"task_id": ids["task_id"], "max_attempts": 1}
    if ids["agent_version_id"]:
        body["agent_version_id"] = ids["agent_version_id"]
    latencies: list[float] = []
    failures: list[str] = []

    async def one() -> None:
        async with sem:
            t0 = time.perf_counter()
            try:
                resp = await client.post(f"{API}/runs", json=body)
            except httpx.HTTPError as exc:
                failures.append(type(exc).__name__)
                return
            if resp.status_code != 202:
                failures.append(f"http_{resp.status_code}")
                return
            latencies.append(time.perf_counter() - t0)

    t0 = time.perf_counter()
    await asyncio.gather(*(one() for _ in range(runs)))
    return {
        "submit_s": time.perf_counter() - t0,
        "submitted": len(latencies),
        "submit_failures": failures,
        "submit_latency_s": metrics.describe(latencies),
    }


async def wait_done(
    conn: asyncpg.Connection, task_id: str, runs: int, graded: bool, timeout_s: float
) -> dict[str, Any]:
    """Until every run is terminal and, if graded, no grading is open and every finished
    attempt has an evaluation row. Returns the last progress snapshot (done=False on timeout)."""
    deadline = time.monotonic() + timeout_s
    while True:
        p = await metrics.fetch_progress(conn, task_id)
        done = p["total"] == runs and p["terminal"] == runs
        if done and graded:
            done = p["grading_open"] == 0 and p["evaluations"] == p["finished_attempts"]
        if done or time.monotonic() > deadline:
            return {**p, "done": done}
        await asyncio.sleep(0.5)


# -- one scenario ----------------------------------------------------------------------------


def write_result(scn: Scenario, repeat: int, result: dict[str, Any]) -> Path:
    scn.out_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    name = (
        f"{stamp}-{scn.label}-{scn.workload}-n{scn.runs}-w{scn.workers}c{scn.concurrency}-r{repeat}"
    )
    rows = result.get("rows")
    if rows and len(rows["created_s"]) > RAW_GZ_THRESHOLD:
        side = scn.out_dir / f"{name}.rows.json.gz"
        side.write_bytes(gzip.compress(json.dumps(rows, separators=(",", ":")).encode()))
        result["rows"] = {"file": side.name}
    path = scn.out_dir / f"{name}.json"
    path.write_text(json.dumps(result, indent=1, default=str) + "\n")
    return path


async def run_scenario(scn: Scenario, repeat: int, repeats: int) -> Path:
    spec = WORKLOADS[scn.workload]
    project = await asyncio.to_thread(compose_project)
    base: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "label": scn.label,
        "workload": scn.workload,
        "runs": scn.runs,
        "workers": scn.workers,
        "concurrency": scn.concurrency,
        "repeat": repeat,
        "repeats": repeats,
        "git": await asyncio.to_thread(git_info),
        "started_at": dt.datetime.now(dt.UTC).isoformat(),
    }
    async with httpx.AsyncClient(
        base_url=scn.base_url, timeout=60,
        limits=httpx.Limits(max_connections=SUBMIT_PARALLELISM * 2),
    ) as client:  # fmt: skip
        ready = await client.get("/ready")
        ready.raise_for_status()
        await ensure_workers(client, scn.workers, scn.concurrency)
        base["machine"] = await asyncio.to_thread(machine_info, project)
        base["settings"] = {
            "worker_env": await asyncio.to_thread(worker_settings, project),
            "submit_parallelism": SUBMIT_PARALLELISM,
            "max_attempts": 1,
        }
        avail = host_mem_available_mib()
        if scn.min_available_mb is not None and avail < scn.min_available_mb:
            base.update(
                status="skipped",
                reason=(
                    f"host MemAvailable {avail:.0f} MiB with {scn.workers} workers up is below "
                    f"the {scn.min_available_mb} MiB floor"
                ),
                mem_available_mib_measured=avail,
            )
            return write_result(scn, repeat, base)

        conn = await asyncpg.connect(reset.database_url())
        try:
            await reset.reset(conn)
            ids = await create_workload(client, scn.workload)
            sampler = Sampler(project)
            sampler.start()
            t0 = time.perf_counter()
            try:
                submit = await submit_all(client, ids, scn.runs)
                progress = await wait_done(
                    conn, str(ids["task_id"]), scn.runs, spec["graded"], 120 + 3 * scn.runs
                )
                drain_s = time.perf_counter() - t0
            finally:
                resources = await asyncio.to_thread(sampler.stop)
            rows = await metrics.fetch_rows(conn, str(ids["task_id"]))
        finally:
            await conn.close()

    created = [r["created"] for r in rows if r["created"] is not None]
    origin = min(created) if created else None
    summary = metrics.summarize(
        rows, expected_outcome=spec["expected_outcome"], graded=spec["graded"]
    )
    base.update(
        status="ok" if progress["done"] and submit["submitted"] == scn.runs else "incomplete",
        expected_outcome=spec["expected_outcome"],
        graded=spec["graded"],
        wall={"submit_s": submit["submit_s"], "total_s": drain_s},
        submit=submit,
        progress=progress,
        summary=summary,
        resources=resources,
        rows=metrics.to_columns(rows, origin),
        finished_at=dt.datetime.now(dt.UTC).isoformat(),
    )
    return write_result(scn, repeat, base)


def describe_result(path: Path) -> str:
    r = json.loads(path.read_text())
    if r.get("status") == "skipped":
        return f"{path.name}: SKIPPED ({r['reason']})"
    s = r["summary"]
    tp = s["throughput_rps"]
    return (
        f"{path.name}: {r['status']} {s['succeeded']}/{s['runs']} ok, throughput {tp:.2f} runs/s"
        if tp
        else f"{path.name}: {r['status']} (no throughput)"
    )


async def run_repeats(scn: Scenario, repeats: int) -> list[Path]:
    paths = []
    for i in range(1, repeats + 1):
        path = await run_scenario(scn, i, repeats)
        print(describe_result(path), flush=True)
        paths.append(path)
        if json.loads(path.read_text()).get("status") == "skipped":
            break  # the same gate would skip every repeat
    return paths


def parse_args(argv: list[str] | None = None) -> tuple[Scenario, int]:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--workload", choices=sorted(WORKLOADS), required=True)
    p.add_argument("--runs", type=int, required=True)
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--concurrency", type=int, default=2)
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--label", default="adhoc")
    p.add_argument(
        "--base-url", default=os.environ.get("AGENTEVAL_E2E_URL", "http://localhost:8000")
    )
    p.add_argument("--out-dir", type=Path, default=RESULTS)
    p.add_argument(
        "--min-available-mb",
        type=int,
        default=None,
        help="skip (and record why) if the host has less MemAvailable once the workers are up",
    )
    a = p.parse_args(argv)
    scn = Scenario(a.workload, a.runs, a.workers, a.concurrency, a.label, a.base_url, a.out_dir,
                   a.min_available_mb)  # fmt: skip
    return scn, a.repeat


def main(argv: list[str] | None = None) -> None:
    reset.check_allowed()
    scn, repeats = parse_args(argv)
    asyncio.run(run_repeats(scn, repeats))


if __name__ == "__main__":
    main()
