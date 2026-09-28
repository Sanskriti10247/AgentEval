"""Agent run end to end against the running compose stack, over real HTTP.

    make up && make migrate && make e2e

FakeProvider only: no network, no API key, deterministic.
"""

import os
import subprocess
import time
import uuid
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.e2e

BASE_URL = os.environ.get("AGENTEVAL_E2E_URL", "http://localhost:8000")
API = "/api/v1"

BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
TESTS = (
    "import unittest\nfrom calc import add\n\n"
    "class T(unittest.TestCase):\n"
    "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n"
)
SCRIPT = [
    {"type": "turn", "text": "Running the tests.", "tool_calls": [{"name": "run_tests"}]},
    {
        "type": "turn",
        "text": "add() subtracts.",
        "tool_calls": [{"name": "write_file", "input": {"path": "calc.py", "content": FIXED}}],
        "expect_tool_results_contain": ["Tests failed"],
    },
    {"type": "turn", "tool_calls": [{"name": "run_tests"}]},
    {"type": "turn", "text": "Fixed add().", "expect_tool_results_contain": ["Tests passed."]},
]


@pytest.fixture(scope="module")
def http() -> httpx.Client:
    with httpx.Client(base_url=BASE_URL, timeout=10) as c:
        try:
            ready = c.get("/ready")
        except httpx.ConnectError:
            pytest.skip(f"API not reachable at {BASE_URL}; run `make up && make migrate`")
        assert ready.status_code == 200, ready.text
        yield c


def ok(resp: httpx.Response, *codes: int) -> dict[str, Any]:
    assert resp.status_code in codes, resp.text
    return resp.json()


def wait_terminal(http: httpx.Client, run_id: str, timeout_s: float = 90) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        run = ok(http.get(f"{API}/runs/{run_id}"), 200)
        if run["status"] in ("COMPLETED", "FAILED"):
            return run
        time.sleep(0.25)
    raise AssertionError(f"run {run_id} not terminal after {timeout_s}s")


def all_events(http: httpx.Client, run_id: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    after = -1
    while True:
        page = ok(
            http.get(
                f"{API}/runs/{run_id}/attempts/1/events", params={"after_seq": after, "limit": 3}
            ),
            200,
        )
        events += page["events"]
        if page["next_after_seq"] is None:
            return events
        after = page["next_after_seq"]


def test_agent_fixes_a_bug_through_the_full_stack(http: httpx.Client) -> None:
    agent = ok(http.post(f"{API}/agents", json={"name": f"e2e-{uuid.uuid4().hex[:8]}"}), 201)
    version = ok(
        http.post(
            f"{API}/agents/{agent['id']}/versions",
            json={
                "model_provider": "fake",
                "model": "fake-model",
                "system_prompt": "You fix bugs in small Python projects.",
                "tools": ["read_file", "write_file", "run_tests"],
                "limits": {"max_steps": 10},
                "model_settings": {"script": SCRIPT},
            },
        ),
        201,
    )
    task = ok(
        http.post(
            f"{API}/tasks",
            json={
                "name": "e2e-fix-add",
                "task_type": "agent",
                "image": "agenteval-sandbox:py312",
                "instructions": "The tests fail. Fix calc.py so they pass.",
                "test_command": ["python", "-m", "unittest", "-q"],
                "files": {"calc.py": BUGGY, "test_calc.py": TESTS},
                "timeout_s": 120,
            },
        ),
        201,
    )
    run = ok(
        http.post(f"{API}/runs", json={"task_id": task["id"], "agent_version_id": version["id"]}),
        202,
    )

    done = wait_terminal(http, run["id"])
    assert (done["status"], done["outcome"], done["error_message"]) == (
        "COMPLETED",
        "AGENT_FINISHED",
        None,
    )
    [attempt] = ok(http.get(f"{API}/runs/{run['id']}/attempts"), 200)
    assert (attempt["steps"], attempt["tokens_in"], attempt["tokens_out"]) == (4, 400, 80)
    assert (attempt["cost_usd"], attempt["final_answer"]) == ("0.000800", "Fixed add().")

    events = all_events(http, run["id"])  # paged 3 at a time
    assert [e["seq"] for e in events] == list(range(9))
    assert [e["event_type"] for e in events][0] == "run_started"
    assert events[-1]["output"]["outcome"] == "AGENT_FINISHED"
    run_tests = [e["output"]["content"] for e in events if e["tool"] == "run_tests"]
    assert run_tests[0].startswith("Tests failed") and run_tests[1].startswith("Tests passed.")

    leftovers = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label=agenteval.run_id={run['id']}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert leftovers == []


def test_agent_hitting_max_steps_through_the_full_stack(http: httpx.Client) -> None:
    agent = ok(http.post(f"{API}/agents", json={"name": f"e2e-{uuid.uuid4().hex[:8]}"}), 201)
    loop = [{"type": "turn", "tool_calls": [{"name": "list_dir"}]} for _ in range(5)]
    version = ok(
        http.post(
            f"{API}/agents/{agent['id']}/versions",
            json={
                "model_provider": "fake",
                "model": "fake-model",
                "tools": ["list_dir"],
                "limits": {"max_steps": 2},
                "model_settings": {"script": loop},
            },
        ),
        201,
    )
    task = ok(
        http.post(
            f"{API}/tasks",
            json={
                "name": "e2e-loop",
                "task_type": "agent",
                "image": "agenteval-sandbox:py312",
                "instructions": "Look around.",
                "timeout_s": 60,
            },
        ),
        201,
    )
    run = ok(
        http.post(f"{API}/runs", json={"task_id": task["id"], "agent_version_id": version["id"]}),
        202,
    )
    done = wait_terminal(http, run["id"])
    assert (done["status"], done["outcome"]) == ("COMPLETED", "MAX_STEPS")
