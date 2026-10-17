"""An API key for the scripts that call a running stack (ADR-012 A).

The platform has authentication ON by default, so `make e2e` and the benchmark driver
need a key. Resolution order:

1. the environment variable the caller names (`AGENTEVAL_E2E_API_KEY`,
   `AGENTEVAL_BENCH_API_KEY`), if set and non-empty;
2. otherwise a fresh short-lived key (admin scopes, expires in 1 day) minted once per
   process with the operator CLI inside the API container:
   `docker compose exec -T api python -m agenteval_api.keys create ...`.
"""

from __future__ import annotations

import functools
import os
import re
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEY_RE = re.compile(r"ae_[0-9a-f]{8}_[A-Za-z0-9_-]{43}")


@functools.cache
def api_key(env_var: str, label: str) -> str:
    given = os.environ.get(env_var, "").strip()
    if given:
        return given
    res = subprocess.run(
        [
            "docker", "compose", "exec", "-T", "api", "python", "-m", "agenteval_api.keys",
            "create", "--name", f"{label}-{int(time.time())}", "--scopes", "read,write,admin",
            "--expires-days", "1",
        ],
        cwd=ROOT, capture_output=True, text=True,
    )  # fmt: skip
    found = KEY_RE.findall(res.stdout)
    if res.returncode != 0 or not found:
        raise RuntimeError(
            f"no API key: set {env_var}, or run the stack (make up && make migrate) so one can "
            f"be created with `docker compose exec api python -m agenteval_api.keys create`. "
            f"CLI said: {res.stderr.strip() or res.stdout.strip()}"
        )
    return found[0]


def auth_headers(env_var: str, label: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key(env_var, label)}"}
