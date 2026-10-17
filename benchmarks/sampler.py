"""Resource sampler: `docker stats --no-stream` in a background thread (ADR-011).

Each sample lists the compose project's api / worker / postgres / redis
containers plus any sandbox container (label `agenteval.run_id`), runs one
`docker stats --no-stream`, and sums CPU% and memory per role (several worker
replicas, or several concurrent sandboxes, add up). Every `docker stats` call
itself takes about a second, so the real cadence is reported (`samples`,
`interval_s`), not assumed. Sandbox containers that live shorter than a sample
are invisible to it: `present` counts the samples in which a role had at least
one container, and `mean` is taken over ALL samples (0 when absent).

CPU% is Docker's: 100 = one full core. Memory is the container's usage in MiB.
The host's MemAvailable and 1-minute load average are sampled alongside, to
show what else was going on.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from typing import Any

ROLES = ("api", "worker", "postgres", "redis", "sandbox")
_UNITS = {"B": 1 / 1048576, "KIB": 1 / 1024, "KB": 1 / 1024, "MIB": 1.0, "MB": 1.0,
          "GIB": 1024.0, "GB": 1024.0}  # fmt: skip


def parse_mem_mib(text: str) -> float:
    """'12.5MiB / 15.3GiB' -> 12.5 (the usage half, in MiB)."""
    m = re.match(r"\s*([\d.]+)\s*([A-Za-z]+)", text.split("/")[0])
    if not m or m.group(2).upper() not in _UNITS:
        raise ValueError(f"unparseable memory usage: {text!r}")
    return float(m.group(1)) * _UNITS[m.group(2).upper()]


def parse_cpu(text: str) -> float:
    return float(text.strip().rstrip("%") or 0)


def host_mem_available_mib() -> float:
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024
    raise RuntimeError("MemAvailable not found")


def host_load1() -> float:
    with open("/proc/loadavg") as f:
        return float(f.read().split()[0])


def summarize_samples(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Mean/peak CPU% and memory per role over the samples (pure)."""
    out: dict[str, Any] = {"samples": len(samples)}
    for role in ROLES:
        cpu = [s["roles"].get(role, {}).get("cpu", 0.0) for s in samples]
        mem = [s["roles"].get(role, {}).get("mem_mib", 0.0) for s in samples]
        n = [s["roles"].get(role, {}).get("containers", 0) for s in samples]
        out[role] = {
            "cpu_mean": sum(cpu) / len(cpu) if cpu else None,
            "cpu_peak": max(cpu) if cpu else None,
            "mem_mean_mib": sum(mem) / len(mem) if mem else None,
            "mem_peak_mib": max(mem) if mem else None,
            "containers_max": max(n) if n else 0,
            "present": sum(1 for x in n if x > 0),
        }
    avail = [s["host_mem_available_mib"] for s in samples]
    load = [s["host_load1"] for s in samples]
    out["host"] = {
        "mem_available_min_mib": min(avail) if avail else None,
        "mem_available_mean_mib": sum(avail) / len(avail) if avail else None,
        "load1_max": max(load) if load else None,
        "load1_mean": sum(load) / len(load) if load else None,
    }
    if len(samples) > 1:
        out["interval_s"] = (samples[-1]["t"] - samples[0]["t"]) / (len(samples) - 1)
    return out


def _docker(*args: str, timeout: float = 30) -> str:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=True, timeout=timeout
    ).stdout


def compose_project() -> str:
    """The compose project of the running api container (not the directory name guess)."""
    cid = subprocess.run(
        ["docker", "compose", "ps", "-q", "api"], capture_output=True, text=True, check=True
    ).stdout.split()[0]
    return _docker(
        "inspect", "-f", '{{index .Config.Labels "com.docker.compose.project"}}', cid
    ).strip()


class Sampler:
    def __init__(self, project: str, period_s: float = 1.0) -> None:
        self.project = project
        self.period_s = period_s
        self.samples: list[dict[str, Any]] = []
        self.errors = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        self._thread.join(timeout=60)
        result = summarize_samples(self.samples)
        result["errors"] = self.errors
        return result

    def _containers(self) -> dict[str, str]:
        """container id -> role."""
        fmt = '{{.ID}}|{{.Label "com.docker.compose.service"}}|{{.Label "agenteval.run_id"}}'
        roles: dict[str, str] = {}
        for flt in (f"label=com.docker.compose.project={self.project}", "label=agenteval.run_id"):
            for line in _docker("ps", "--filter", flt, "--format", fmt).splitlines():
                cid, service, run_id = line.split("|")
                if run_id:
                    roles[cid] = "sandbox"
                elif service in ROLES:
                    roles[cid] = service
        return roles

    def _sample(self) -> None:
        t = time.time()
        roles = self._containers()
        agg: dict[str, dict[str, float]] = {}
        if roles:
            out = _docker("stats", "--no-stream", "--format", "{{json .}}", *roles, timeout=60)
            for line in out.splitlines():
                d = json.loads(line)
                role = next((r for cid, r in roles.items() if cid.startswith(d["ID"])), None)
                if role is None:
                    continue
                a = agg.setdefault(role, {"cpu": 0.0, "mem_mib": 0.0, "containers": 0})
                a["cpu"] += parse_cpu(d["CPUPerc"])
                a["mem_mib"] += parse_mem_mib(d["MemUsage"])
                a["containers"] += 1
        self.samples.append(
            {
                "t": t,
                "roles": agg,
                "host_mem_available_mib": host_mem_available_mib(),
                "host_load1": host_load1(),
            }  # fmt: skip
        )

    def _loop(self) -> None:
        while not self._stop.is_set():
            began = time.monotonic()
            try:
                self._sample()
            except (subprocess.SubprocessError, ValueError, KeyError, OSError):
                # A sandbox container vanished between `ps` and `stats`: skip this sample.
                self.errors += 1
            self._stop.wait(max(0.0, self.period_s - (time.monotonic() - began)))
