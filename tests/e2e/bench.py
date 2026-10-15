"""The Phase 4 E2E benchmark: tasks, agent scripts, the expected outcome
matrix, and HTTP helpers for the running compose stack.

Every agent is a deterministic FakeProvider script, identical for every task
in a batch (so scripts never depend on a task's output); variety comes from
the tasks' evaluation specs. MARKER is planted in every piece of hidden
evaluator data and must never come back from the API (D11).

Expected categories follow Step 3's classify() precedence:
platform > evaluation error > tampering > passed > limits (max_steps) >
refusal / no answer > the first failing required check's category.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import asyncpg
import httpx

BASE_URL = os.environ.get("AGENTEVAL_E2E_URL", "http://localhost:8000")
DATABASE_URL = os.environ.get(
    "AGENTEVAL_E2E_DATABASE_URL", "postgresql://agenteval:agenteval@127.0.0.1:55432/agenteval"
)
API = "/api/v1"
IMAGE = "agenteval-sandbox:py312"
MARKER = "HIDDEN_MARKER_e2e_5f1c"
COST_PER_CALL = Decimal("0.000200")  # FakeProvider: 100 in + 20 out tokens at $1 / $5 per M
WAIT_S = 300

# Benchmark policy weights for the custom-scoring batch (S3): task success only.
ONLY_SUCCESS = {
    "version": "v1",
    "weights": {"task_success": "1", "test_score": "0", "reliability": "0", "efficiency": "0"},
}

BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
VISIBLE_TESTS = (
    "import unittest\nfrom calc import add\n\n"
    "class T(unittest.TestCase):\n"
    "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n"
)
REPORT = "add fixed\n"

# -- tasks ---------------------------------------------------------------------------------

HIDDEN_TESTS = (
    f"# {MARKER}\nimport unittest\nfrom calc import add\n\n"
    "class Hidden(unittest.TestCase):\n"
    f"    def test_pos(self):\n        self.assertEqual(add(2, 3), 5, '{MARKER}')\n"
    f"    def test_zero(self):\n        self.assertEqual(add(-1, 1), 0, '{MARKER}')\n"
)
IMPOSSIBLE_TESTS = (
    f"# {MARKER}\nimport unittest\nfrom calc import add\n\n"
    "class Hidden(unittest.TestCase):\n"
    f"    def test_six(self):\n        self.assertEqual(add(2, 3), 6, '{MARKER}')\n"
)

SPECS: dict[str, dict[str, Any] | None] = {
    # C2/C3 hidden tests + integrity of the visible test file.
    "T1": {
        "checks": [{"kind": "python_tests", "name": f"hidden_{MARKER}", "pattern": "test_h*.py",
                    "expected_tests": 2}],
        "hidden_files": {"test_hidden.py": HIDDEN_TESTS},
        "protected_paths": ["test_calc.py"],
    },
    # C4: stdout compared on the host; C6: a trusted-environment command check.
    "T2": {
        "checks": [
            {"kind": "io", "name": f"io_{MARKER}", "stdin": "2\n", "expected_stdout": "5",
             "argv": ["python", "-c",
                      f"from calc import add; print(add(int(input()), 3))  # {MARKER}"]},
            {"kind": "command", "name": f"cmd_{MARKER}", "trusted_environment": True,
             "argv": ["python", "-c",
                      f"import calc, sys; sys.exit(calc.add(1, 1) != 2)  # {MARKER}"]},
        ],
    },
    # A6: final-answer only - no snapshot, no evaluation container.
    "T3": {
        "checks": [{"kind": "final_answer", "name": f"answer_{MARKER}", "match": "contains",
                    "expected": "Fixed add"}],
    },
    # A file the agent must leave behind, read from the pristine snapshot.
    "T4": {
        "checks": [{"kind": "file", "name": f"report_{MARKER}", "path": "report.txt",
                    "mode": "equals", "expected": REPORT}],
    },
    # Fails whatever the agent does.
    "T5": {
        "checks": [{"kind": "python_tests", "name": f"impossible_{MARKER}",
                    "pattern": "test_h*.py", "expected_tests": 1}],
        "hidden_files": {"test_hidden.py": IMPOSSIBLE_TESTS},
    },
    # R7: no spec - ungraded, outside the success-rate denominator.
    "T6": None,
}  # fmt: skip
BENCH_TASKS = ["T1", "T2", "T3", "T4", "T5", "T6"]
# Tasks whose spec needs the workspace (A6): snapshot + evaluation container.
NEEDS_WORKSPACE = {"T1", "T2", "T4", "T5"}


def task_body(label: str, dataset_id: str) -> dict[str, Any]:
    return {
        "name": f"{label}-{uuid.uuid4().hex[:6]}",
        "task_type": "agent",
        "image": IMAGE,
        "instructions": "The tests in test_calc.py fail. Fix calc.py so they pass.",
        "test_command": ["python", "-m", "unittest", "-q"],
        "files": {"calc.py": BUGGY, "test_calc.py": VISIBLE_TESTS},
        "timeout_s": 120,
        "difficulty": "easy",
        "dataset_id": dataset_id,
        "evaluation": SPECS[label],
    }


# -- agents ----------------------------------------------------------------------------------


def turn(text: str = "", *calls: tuple[str, dict[str, Any]]) -> dict[str, Any]:
    step: dict[str, Any] = {"type": "turn", "text": text}
    if calls:
        step["tool_calls"] = [{"name": n, "input": i} for n, i in calls]
    return step


def write(path: str, content: str) -> tuple[str, dict[str, Any]]:
    return ("write_file", {"path": path, "content": content})


AGENTS: dict[str, dict[str, Any]] = {
    "fixer": {
        "tools": ["read_file", "write_file", "run_tests"],
        "script": [
            turn("Fixing add() and writing the report.", write("calc.py", FIXED),
                 write("report.txt", REPORT)),
            turn("Checking.", ("run_tests", {})),
            turn("Fixed add()."),
        ],
    },
    "quitter": {"tools": ["read_file"], "script": [turn("I could not fix it.")]},
    "cheater": {
        "tools": ["write_file"],
        "script": [
            turn("", write("calc.py", FIXED), write("test_calc.py", "import unittest\n")),
            turn("Fixed add()."),
        ],
    },
    "looper": {
        "tools": ["list_dir"],
        "limits": {"max_steps": 2},
        "script": [turn("", ("list_dir", {})) for _ in range(5)],
    },
    "model-error": {
        "tools": ["read_file"],
        "script": [{"type": "error", "kind": "authentication", "message": "scripted 401"}],
    },
    "hoarder": {
        "tools": ["shell"],
        "shell_allowlist": ["python"],
        "script": [
            turn("", ("shell", {"argv": ["python", "-c",
                 "import os; open('blob.bin', 'wb').write(os.urandom(17 * 1024 * 1024))"]})),
            turn("Fixed add()."),
        ],
    },
}  # fmt: skip
STEPS = {"fixer": 3, "quitter": 1, "cheater": 2, "looper": 2, "model-error": 0, "hoarder": 2}

# category per (agent, task); None = no category (ungraded, no spec)
EXPECTED: dict[str, dict[str, str | None]] = {
    "fixer": {"T1": "passed", "T2": "passed", "T3": "passed", "T4": "passed",
              "T5": "tests_failed", "T6": None},
    "quitter": {"T1": "tests_failed", "T2": "tests_failed", "T3": "wrong_answer",
                "T4": "missing_output", "T5": "tests_failed", "T6": None},
    "cheater": {"T1": "tampering", "T2": "passed", "T3": "passed", "T4": "missing_output",
                "T5": "tests_failed", "T6": None},
    # MAX_STEPS beats every task-result category; a no-spec task keeps it too.
    "looper": {"T1": "max_steps", "T2": "max_steps", "T3": "max_steps", "T4": "max_steps",
               "T5": "max_steps", "T6": "max_steps"},
}  # fmt: skip


# -- HTTP helpers ------------------------------------------------------------------------------


def ok(resp: httpx.Response, *codes: int) -> Any:
    assert resp.status_code in codes, f"{resp.request.method} {resp.request.url}: {resp.text}"
    return resp.json()


def create_dataset(http: httpx.Client, labels: list[str]) -> tuple[str, dict[str, str]]:
    """A frozen dataset; returns (dataset id, label -> task id)."""
    ds = ok(
        http.post(f"{API}/datasets", json={"name": f"calc-bench-{uuid.uuid4().hex[:8]}",
                                           "version": "1"}),
        201,
    )  # fmt: skip
    tasks = {label: ok(http.post(f"{API}/tasks", json=task_body(label, ds["id"])), 201)["id"]
             for label in labels}  # fmt: skip
    ok(http.post(f"{API}/datasets/{ds['id']}/freeze"), 200)
    return ds["id"], tasks


def create_agent(http: httpx.Client, name: str) -> str:
    cfg = AGENTS[name]
    agent = ok(http.post(f"{API}/agents", json={"name": f"e2e-{name}-{uuid.uuid4().hex[:6]}"}), 201)
    version = ok(
        http.post(
            f"{API}/agents/{agent['id']}/versions",
            json={
                "model_provider": "fake",
                "model": "fake-model",
                "system_prompt": "You fix bugs in small Python projects.",
                "tools": cfg["tools"],
                "shell_allowlist": cfg.get("shell_allowlist", []),
                "limits": cfg.get("limits", {"max_steps": 10}),
                "model_settings": {"script": cfg["script"]},
            },
        ),
        201,
    )
    return str(version["id"])


def start_evaluation(
    http: httpx.Client,
    dataset_id: str,
    version_id: str,
    *,
    repetitions: int = 1,
    scoring_config: dict[str, Any] | None = None,
    key: str | None = None,
    codes: tuple[int, ...] = (202,),
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "calc-bench",
        "dataset_id": dataset_id,
        "agent_version_id": version_id,
        "repetitions": repetitions,
    }
    if scoring_config is not None:
        body["scoring_config"] = scoring_config
    headers = {"Idempotency-Key": key} if key else {}
    return dict(ok(http.post(f"{API}/evaluations", json=body, headers=headers), *codes))


def wait_complete(http: httpx.Client, evaluation_id: str, timeout_s: float = WAIT_S) -> dict:
    deadline = time.monotonic() + timeout_s
    body: dict[str, Any] = {}
    while time.monotonic() < deadline:
        body = ok(http.get(f"{API}/evaluations/{evaluation_id}"), 200)
        if body["progress"]["complete"]:
            return body
        time.sleep(0.5)
    raise AssertionError(f"evaluation {evaluation_id} incomplete after {timeout_s}s: {body}")


def all_results(http: httpx.Client, evaluation_id: str, page: int = 5) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    after: str | None = None
    while True:
        params: dict[str, Any] = {"limit": page}
        if after:
            params["after"] = after
        body = ok(http.get(f"{API}/evaluations/{evaluation_id}/results", params=params), 200)
        rows += body["results"]
        after = body["next_after"]
        if after is None:
            return rows


def all_events(http: httpx.Client, run_id: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    after = -1
    while True:
        page = ok(
            http.get(f"{API}/runs/{run_id}/attempts/1/events",
                     params={"after_seq": after, "limit": 50}),
            200,
        )  # fmt: skip
        events += page["events"]
        if page["next_after_seq"] is None:
            return events
        after = page["next_after_seq"]


def leftover_containers(run_ids: set[str]) -> set[str]:
    """Run ids that still have a labelled container (agent or evaluation)."""
    out = subprocess.run(
        ["docker", "ps", "-a", "--filter", "label=agenteval.run_id",
         "--format", '{{.Label "agenteval.run_id"}}'],
        capture_output=True, text=True, check=True,
    ).stdout.split()  # fmt: skip
    return run_ids & set(out)


def image_id(image: str = IMAGE) -> str:
    return subprocess.run(
        ["docker", "image", "inspect", image, "--format", "{{.Id}}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()  # fmt: skip


# -- a finished batch ----------------------------------------------------------------------------


@dataclass
class Batch:
    agent: str
    evaluation_id: str
    tasks: dict[str, str]  # label -> task id
    repetitions: int
    rows: list[dict[str, Any]] = field(default_factory=list)

    def label_of(self, task_id: str) -> str:
        return next(label for label, tid in self.tasks.items() if tid == task_id)

    def by_label(self) -> dict[tuple[str, int], dict[str, Any]]:
        return {(self.label_of(r["task_id"]), r["repetition"]): r for r in self.rows}


# -- read-only database probe (E1) --------------------------------------------------------


class ReadOnlyDB:
    """SELECT-only access to the stack's Postgres, for invariants the public
    API deliberately doesn't expose (D11)."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def select(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        if not sql.lstrip().upper().startswith(("SELECT", "WITH")):
            raise ValueError("the E2E database probe only runs SELECT queries")
        return asyncio.run(self._fetch(sql, *args))

    async def _fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        conn = await asyncpg.connect(
            self._dsn, server_settings={"default_transaction_read_only": "on"}
        )
        try:
            async with conn.transaction(readonly=True):
                return [dict(r) for r in await conn.fetch(sql, *args)]
        finally:
            await conn.close()
