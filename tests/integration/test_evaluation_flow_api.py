"""The whole Phase 4 flow through the API and a real worker: dataset + spec
tasks -> freeze -> two evaluation batches -> the executor runs every run in
Docker (FakeProvider) and Step 5 grades it -> progress, summary, results and
compare report the exact outcome. No network, no API key."""

from typing import Any

import httpx
import pytest

from agenteval_core.artifacts import LocalArtifactStore
from agenteval_core.models import AgentVersion
from agenteval_core.queue.streams import RunQueue
from tests.integration.conftest import SANDBOX_IMAGE
from tests.integration.eval_seed import SM, agent_version
from tests.integration.test_agent_executor import BUGGY, FIX_THE_BUG, TESTS, handle_next
from tests.integration.test_evaluation_runner import GIVE_UP, HIDDEN_SPEC, MARKER, build

pytestmark = pytest.mark.docker

UNSATISFIABLE = {  # add(2, 3) == 6: fails whatever the agent does
    "checks": [{"kind": "python_tests", "name": "hidden", "pattern": "test_h*.py",
                "expected_tests": 1}],
    "hidden_files": {"test_hidden.py": (
        f"# {MARKER}\nimport unittest\nfrom calc import add\n\n"
        "class H(unittest.TestCase):\n    def test_six(self):\n"
        "        self.assertEqual(add(2, 3), 6)\n")},
}  # fmt: skip


async def make_dataset(client: httpx.AsyncClient) -> tuple[str, dict[str, str]]:
    ds = (await client.post("/api/v1/datasets", json={"name": "calc", "version": "1"})).json()
    tasks = {}
    for label, spec in (
        ("fixable", HIDDEN_SPEC),
        ("ungraded", None),
        ("impossible", UNSATISFIABLE),
    ):
        body: dict[str, Any] = {
            "name": label, "task_type": "agent", "image": SANDBOX_IMAGE,
            "instructions": "The tests in test_calc.py fail. Fix calc.py so they pass.",
            "test_command": ["python", "-m", "unittest", "-q"],
            "files": {"calc.py": BUGGY, "test_calc.py": TESTS}, "timeout_s": 120,
            "dataset_id": ds["id"], "evaluation": spec,
        }  # fmt: skip
        resp = await client.post("/api/v1/tasks", json=body)
        assert resp.status_code == 201, resp.text
        tasks[label] = resp.json()["id"]
    assert (await client.post(f"/api/v1/datasets/{ds['id']}/freeze")).status_code == 200
    return ds["id"], tasks


async def evaluate(
    client: httpx.AsyncClient,
    sm: SM,
    queue: RunQueue,
    store: LocalArtifactStore,
    ds: str,
    script: list[dict[str, Any]],
) -> str:
    version = await agent_version(sm)
    async with sm() as s:  # give the version its script
        v = await s.get(AgentVersion, version)
        assert v is not None
        v.model_settings = {"script": script}
        v.tools = ["read_file", "write_file", "list_dir", "search", "shell", "run_tests"]
        v.shell_allowlist = ["python"]
        v.limits = {"max_steps": 10}
        await s.commit()
    resp = await client.post(
        "/api/v1/evaluations",
        json={"name": "calc-bench", "dataset_id": ds, "agent_version_id": str(version)},
    )
    assert resp.status_code == 202, resp.text
    ex, _ = build(sm, queue, store)
    for _ in range(resp.json()["progress"]["runs_total"]):
        await handle_next(ex, queue)
    return str(resp.json()["id"])


async def test_benchmark_flow_through_the_api(
    client: httpx.AsyncClient,
    sessionmaker: SM,
    queue: RunQueue,
    artifact_store: LocalArtifactStore,
) -> None:
    ds, tasks = await make_dataset(client)
    fixer = await evaluate(client, sessionmaker, queue, artifact_store, ds, FIX_THE_BUG)
    quitter = await evaluate(client, sessionmaker, queue, artifact_store, ds, GIVE_UP)

    progress = (await client.get(f"/api/v1/evaluations/{fixer}")).json()["progress"]
    assert progress["complete"] is True and progress["runs_by_status"]["COMPLETED"] == 3

    summary = (await client.get(f"/api/v1/evaluations/{fixer}/summary")).json()
    assert (summary["graded"], summary["passed"], summary["ungraded"]) == (2, 1, 1)
    assert summary["success_rate"] == 0.5 and summary["complete"] is True
    assert summary["failure_categories"] == {"passed": 1, "tests_failed": 1}
    assert (await client.get(f"/api/v1/evaluations/{quitter}/summary")).json()["passed"] == 0

    compare = await client.get("/api/v1/evaluations/compare", params={"ids": f"{fixer},{quitter}"})
    assert compare.status_code == 200
    by_task = {t["task_id"]: t for t in compare.json()["per_task"]}
    assert by_task[tasks["fixable"]]["delta_vs_first"][quitter] == -1.0
    assert by_task[tasks["impossible"]]["delta_vs_first"][quitter] == 0.0
    assert by_task[tasks["ungraded"]]["success_rate"] == {fixer: None, quitter: None}

    results = await client.get(f"/api/v1/evaluations/{fixer}/results")
    for resp in (results, compare):
        assert MARKER not in resp.text
