"""Datasets and evaluation-spec tasks through the API (Phase 4 step 6):
DRAFT -> FROZEN rules, write-only specs, and spec validation (including the
2 MiB limit) before anything is persisted, with sanitised errors (D11)."""

import asyncio
import uuid
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agenteval_core.evaluation.spec import MAX_EXPECTED_BYTES
from agenteval_core.evaluation.taxonomy import MAX_EVALUATION_SPEC_BYTES
from agenteval_core.models import Task
from tests.integration.conftest import SANDBOX_IMAGE

SM = async_sessionmaker[AsyncSession]
MARKER = "HIDDEN_MARKER_d5c2"
SPEC = {
    "checks": [{"kind": "python_tests", "name": f"chk_{MARKER}", "expected_tests": 1}],
    "hidden_files": {"test_hidden.py": f"# {MARKER}\n"},
    "protected_paths": ["test_calc.py"],
}


def agent_task(**kw: Any) -> dict[str, Any]:
    return {
        "name": "fix-add",
        "task_type": "agent",
        "image": SANDBOX_IMAGE,
        "instructions": "Fix calc.py.",
        "files": {"calc.py": "x = 1\n", "test_calc.py": "pass\n"},
        "timeout_s": 120,
        **kw,
    }


async def dataset(client: httpx.AsyncClient, name: str | None = None) -> str:
    resp = await client.post(
        "/api/v1/datasets", json={"name": name or f"d-{uuid.uuid4().hex[:6]}", "version": "1"}
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def task_count(sm: SM) -> int:
    async with sm() as s:
        return int((await s.execute(select(func.count()).select_from(Task))).scalar_one())


async def test_dataset_lifecycle(client: httpx.AsyncClient) -> None:
    ds = await dataset(client, "bench")
    dup = await client.post("/api/v1/datasets", json={"name": "bench", "version": "1"})
    assert dup.status_code == 409
    assert (await client.post(f"/api/v1/datasets/{ds}/freeze")).status_code == 409  # empty

    t = await client.post("/api/v1/tasks", json=agent_task(dataset_id=ds, difficulty="hard"))
    assert t.status_code == 201, t.text
    frozen = await client.post(f"/api/v1/datasets/{ds}/freeze")
    again = await client.post(f"/api/v1/datasets/{ds}/freeze")
    assert frozen.status_code == again.status_code == 200
    assert frozen.json() == again.json()  # idempotent
    body = frozen.json()
    assert body["status"] == "FROZEN" and body["frozen_at"] is not None
    assert body["task_ids"] == [t.json()["id"]] and body["task_count"] == 1

    late = await client.post("/api/v1/tasks", json=agent_task(dataset_id=ds))
    assert late.status_code == 409
    missing = await client.post("/api/v1/tasks", json=agent_task(dataset_id=str(uuid.uuid4())))
    assert missing.status_code == 404
    assert (await client.get(f"/api/v1/datasets/{uuid.uuid4()}")).status_code == 404


async def test_the_spec_is_write_only(client: httpx.AsyncClient) -> None:
    ds = await dataset(client)
    created = await client.post("/api/v1/tasks", json=agent_task(dataset_id=ds, evaluation=SPEC))
    assert created.status_code == 201, created.text
    fetched = await client.get(f"/api/v1/tasks/{created.json()['id']}")
    for resp in (created, fetched):
        body = resp.json()
        assert "evaluation" not in body
        assert body["has_evaluation"] is True and body["dataset_id"] == ds
        assert MARKER not in resp.text
    plain = await client.post("/api/v1/tasks", json=agent_task())
    assert plain.json()["has_evaluation"] is False  # R7: allowed, stays ungraded


async def test_command_tasks_take_no_spec_or_dataset(client: httpx.AsyncClient) -> None:
    ds = await dataset(client)
    command = {"name": "c", "image": SANDBOX_IMAGE, "command": ["true"]}
    for extra in ({"evaluation": SPEC}, {"dataset_id": ds}):
        resp = await client.post("/api/v1/tasks", json={**command, **extra})
        assert resp.status_code == 422, resp.text
        assert MARKER not in resp.text


async def test_invalid_specs_are_refused_before_persistence(
    client: httpx.AsyncClient, sessionmaker: SM
) -> None:
    before = await task_count(sessionmaker)
    not_a_task_file = {**SPEC, "protected_paths": [f"{MARKER}.py"]}
    resp = await client.post("/api/v1/tasks", json=agent_task(evaluation=not_a_task_file))
    assert resp.status_code == 422
    assert resp.json()["detail"] == "protected_paths must name task files"
    assert MARKER not in resp.text

    broken = {**SPEC, "hidden_files": {"conftest.py": MARKER}}  # a reserved name
    resp = await client.post("/api/v1/tasks", json=agent_task(evaluation=broken))
    assert resp.status_code == 422 and MARKER not in resp.text
    assert await task_count(sessionmaker) == before


async def test_a_spec_over_2_mib_is_refused_with_a_fixed_message(
    client: httpx.AsyncClient, sessionmaker: SM
) -> None:
    """Every field is within its own limit, but the whole spec isn't: the
    service catches it before the database CHECK ever could."""
    expected = (MARKER * (MAX_EXPECTED_BYTES // len(MARKER)))[:MAX_EXPECTED_BYTES]
    checks = [{"kind": "final_answer", "name": f"a{i}", "expected": expected} for i in range(20)]
    big = {"checks": checks, "hidden_files": {"h.txt": MARKER * 50_000}}
    before = await task_count(sessionmaker)
    resp = await client.post("/api/v1/tasks", json=agent_task(evaluation=big))
    assert resp.status_code == 422
    assert resp.json()["detail"] == f"evaluation spec exceeds {MAX_EVALUATION_SPEC_BYTES} bytes"
    assert MARKER not in resp.text
    assert await task_count(sessionmaker) == before


async def test_freeze_and_add_task_race_never_leaves_a_late_task(
    client: httpx.AsyncClient,
) -> None:
    ds = await dataset(client)
    assert (await client.post("/api/v1/tasks", json=agent_task(dataset_id=ds))).status_code == 201
    adds = [client.post("/api/v1/tasks", json=agent_task(dataset_id=ds)) for _ in range(8)]
    freeze = client.post(f"/api/v1/datasets/{ds}/freeze")
    *added, frozen = await asyncio.gather(*adds, freeze)
    assert frozen.status_code == 200
    accepted = {r.json()["id"] for r in added if r.status_code == 201}
    assert all(r.status_code in (201, 409) for r in added)
    final = (await client.get(f"/api/v1/datasets/{ds}")).json()
    # Every accepted task is in the frozen set, and the frozen set is final.
    assert accepted <= set(final["task_ids"])
    assert final["task_ids"] == frozen.json()["task_ids"]
