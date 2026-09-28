"""End-to-end test of the vertical slice against the running compose stack.

    make up && make migrate && make e2e

Everything goes over real HTTP to the api container; the worker container
executes the run in a real sibling sandbox container.
"""

import os
import subprocess
import time
import uuid

import httpx
import pytest

pytestmark = pytest.mark.e2e

BASE_URL = os.environ.get("AGENTEVAL_E2E_URL", "http://localhost:8000")
TERMINAL = {"COMPLETED", "FAILED"}


@pytest.fixture(scope="module")
def http() -> httpx.Client:
    with httpx.Client(base_url=BASE_URL, timeout=10) as c:
        try:
            ready = c.get("/ready")
        except httpx.ConnectError:
            pytest.skip(f"API not reachable at {BASE_URL}; run `make up && make migrate`")
        assert ready.status_code == 200, ready.text
        yield c


def wait_terminal(http: httpx.Client, run_id: str, timeout_s: float = 60) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        run = http.get(f"/api/v1/runs/{run_id}").json()
        if run["status"] in TERMINAL:
            return run
        time.sleep(0.25)
    raise AssertionError(f"run {run_id} not terminal after {timeout_s}s: {run}")


def leftover_containers(run_id: str) -> list[str]:
    out = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label=agenteval.run_id={run_id}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.split()


def create_task(http: httpx.Client, **overrides: object) -> str:
    body = {
        "name": "e2e-exit-three",
        "image": "agenteval-sandbox:py312",
        "command": ["python", "main.py"],
        "files": {"main.py": "print('value=42')\nraise SystemExit(3)\n"},
        "timeout_s": 20,
        **overrides,
    }
    resp = http.post("/api/v1/tasks", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_submit_execute_persist_retrieve(http: httpx.Client) -> None:
    task_id = create_task(http)
    key = f"e2e-{uuid.uuid4()}"
    resp = http.post("/api/v1/runs", json={"task_id": task_id}, headers={"Idempotency-Key": key})
    assert resp.status_code == 202, resp.text
    run_id = resp.json()["id"]

    run = wait_terminal(http, run_id)
    assert run["status"] == "COMPLETED"
    assert run["outcome"] == "NON_ZERO_EXIT"

    attempts = http.get(f"/api/v1/runs/{run_id}/attempts").json()
    assert len(attempts) == 1
    assert attempts[0]["exit_code"] == 3
    assert attempts[0]["stdout"] == "value=42\n"
    assert leftover_containers(run_id) == []

    # Replaying the same key after completion returns the same run, no new work.
    replay = http.post("/api/v1/runs", json={"task_id": task_id}, headers={"Idempotency-Key": key})
    assert replay.status_code == 200
    assert replay.json()["id"] == run_id
    assert len(http.get(f"/api/v1/runs/{run_id}/attempts").json()) == 1


def test_timeout_through_full_stack(http: httpx.Client) -> None:
    task_id = create_task(http, command=["sleep", "999"], files={}, timeout_s=2)
    run_id = http.post("/api/v1/runs", json={"task_id": task_id}).json()["id"]
    run = wait_terminal(http, run_id)
    assert (run["status"], run["outcome"]) == ("COMPLETED", "TIMEOUT")
    assert leftover_containers(run_id) == []
