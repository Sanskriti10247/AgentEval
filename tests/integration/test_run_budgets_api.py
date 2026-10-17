"""Phase 5 Step 9: execution and grading budgets on the API (F6, F9, G1; PROGRESS.md
section 21). A budget is pinned when the run (or batch) is created and is part of the
Idempotency-Key comparison; a batch's budgets flow to its runs and their evaluations."""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from agenteval_api.main import create_app
from agenteval_core.artifacts import LocalArtifactStore
from agenteval_core.config import Settings
from agenteval_core.queue.streams import RunQueue
from tests.integration.conftest import bearer, create_task
from tests.integration.eval_seed import SM, agent_version, frozen_dataset
from tests.integration.test_agent_executor import handle_next
from tests.integration.test_evaluation_runner import build
from tests.integration.test_reconciler import run_row

API = "/api/v1"


async def submit(client: httpx.AsyncClient, task_id: str, **extra: Any) -> httpx.Response:
    headers = {"Idempotency-Key": extra.pop("key")} if "key" in extra else {}
    return await client.post(f"{API}/runs", json={"task_id": task_id, **extra}, headers=headers)


# -- POST /runs ----------------------------------------------------------------------------------


async def test_a_submitted_run_is_pinned_with_the_setting_by_default(
    client: httpx.AsyncClient, engine: AsyncEngine, settings: Settings
) -> None:
    assert settings.run_max_attempts == 3  # AGENTEVAL_RUN_MAX_ATTEMPTS default
    resp = await submit(client, await create_task(client))
    assert resp.status_code == 202, resp.text
    assert resp.json()["max_attempts"] == 3
    assert (await run_row(engine, uuid.UUID(resp.json()["id"]))).max_attempts == 3


@pytest.mark.parametrize("budget", [1, 5, 10])
async def test_an_explicit_budget_is_pinned_on_the_run(
    client: httpx.AsyncClient, engine: AsyncEngine, budget: int
) -> None:
    resp = await submit(client, await create_task(client), max_attempts=budget)
    assert resp.status_code == 202 and resp.json()["max_attempts"] == budget
    assert (await run_row(engine, uuid.UUID(resp.json()["id"]))).max_attempts == budget


async def test_the_default_follows_the_setting(settings: Settings, api_key: str) -> None:
    app = create_app(settings.model_copy(update={"run_max_attempts": 6}))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", headers=bearer(api_key)
        ) as c:
            resp = await submit(c, await create_task(c))
            assert resp.json()["max_attempts"] == 6
            assert (await submit(c, await create_task(c), max_attempts=2)).json()[
                "max_attempts"
            ] == 2


@pytest.mark.parametrize("budget", [0, 11, -1, 2.5, "x"])
async def test_a_budget_outside_1_to_10_is_422(client: httpx.AsyncClient, budget: Any) -> None:
    task_id = await create_task(client)
    resp = await submit(client, task_id, max_attempts=budget)
    assert resp.status_code == 422, resp.text
    listed = await client.get(f"{API}/runs")
    assert listed.json()["runs"] == []  # nothing was created


async def test_the_budget_is_part_of_the_submit_idempotency_comparison(
    client: httpx.AsyncClient,
) -> None:
    task_id = await create_task(client)
    first = await submit(client, task_id, key="k", max_attempts=2)
    assert first.status_code == 202

    same = await submit(client, task_id, key="k", max_attempts=2)
    assert same.status_code == 200 and same.json()["id"] == first.json()["id"]
    different = await submit(client, task_id, key="k", max_attempts=4)
    assert different.status_code == 409 and "max_attempts" in different.json()["detail"]
    default = await submit(client, task_id, key="k")  # omitted = the default (3), not 2
    assert default.status_code == 409

    defaulted = await submit(client, task_id, key="d")
    assert defaulted.status_code == 202
    assert (await submit(client, task_id, key="d", max_attempts=3)).status_code == 200


async def test_direct_inserts_keep_the_phase_4_budget_of_one(engine: AsyncEngine) -> None:
    run_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tasks (id, name, task_type, image, command, files, timeout_s,"
                " memory_mb, cpu_limit) VALUES (:t, 'x', 'command', 'i', '[\"true\"]', '{}', 30,"
                " 256, 1.0)"
            ),
            {"t": run_id},
        )
        await conn.execute(
            text(
                "INSERT INTO runs (id, task_id, status, attempt_count) VALUES (:r, :t, 'QUEUED', 0)"
            ),
            {"r": uuid.uuid4(), "t": run_id},
        )
    assert (await engine_scalar(engine, "SELECT max_attempts FROM runs")) == 1


async def engine_scalar(engine: AsyncEngine, sql: str) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(text(sql))).scalar_one()


# -- POST /evaluations ---------------------------------------------------------------------------


@pytest.fixture
async def batch_inputs(
    client: httpx.AsyncClient, sessionmaker: SM
) -> AsyncIterator[tuple[str, uuid.UUID]]:
    ds, _ = await frozen_dataset(client)
    yield ds, await agent_version(sessionmaker)


async def make_batch(
    client: httpx.AsyncClient, inputs: tuple[str, uuid.UUID], **extra: Any
) -> httpx.Response:
    key = extra.pop("key", None)
    return await client.post(
        f"{API}/evaluations",
        json={
            "name": "b",
            "dataset_id": inputs[0],
            "agent_version_id": str(inputs[1]),
            "repetitions": 2,
            **extra,
        },
        headers={"Idempotency-Key": key} if key else {},
    )


async def run_budgets(engine: AsyncEngine, evaluation_id: str) -> set[int]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT DISTINCT max_attempts FROM runs WHERE evaluation_id = :e"),
            {"e": uuid.UUID(evaluation_id)},
        )
        return {r.max_attempts for r in rows}


async def test_a_batch_pins_both_budgets_and_copies_the_run_budget_to_every_run(
    client: httpx.AsyncClient, engine: AsyncEngine, batch_inputs: tuple[str, uuid.UUID]
) -> None:
    resp = await make_batch(client, batch_inputs, run_max_attempts=2, eval_max_attempts=5)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert (body["run_max_attempts"], body["eval_max_attempts"]) == (2, 5)
    assert body["progress"]["runs_total"] == 6
    assert await run_budgets(engine, body["id"]) == {2}
    row = await engine_scalar(
        engine, "SELECT run_max_attempts * 10 + eval_max_attempts FROM evaluations"
    )
    assert row == 25
    assert (await client.get(f"{API}/evaluations/{body['id']}")).json()["run_max_attempts"] == 2


async def test_a_batch_defaults_to_the_settings(
    client: httpx.AsyncClient, engine: AsyncEngine, batch_inputs: tuple[str, uuid.UUID],
    settings: Settings,
) -> None:  # fmt: skip
    body = (await make_batch(client, batch_inputs)).json()
    assert (body["run_max_attempts"], body["eval_max_attempts"]) == (
        settings.run_max_attempts, settings.eval_max_attempts,
    )  # fmt: skip
    assert (body["run_max_attempts"], body["eval_max_attempts"]) == (3, 3)
    assert await run_budgets(engine, body["id"]) == {3}


@pytest.mark.parametrize("field", ["run_max_attempts", "eval_max_attempts"])
@pytest.mark.parametrize("budget", [0, 11, -3, "x"])
async def test_a_batch_budget_outside_1_to_10_is_422(
    client: httpx.AsyncClient, batch_inputs: tuple[str, uuid.UUID], field: str, budget: Any
) -> None:
    resp = await make_batch(client, batch_inputs, **{field: budget})
    assert resp.status_code == 422, resp.text


async def test_g1_both_budgets_are_part_of_the_batch_idempotency_comparison(
    client: httpx.AsyncClient, engine: AsyncEngine, batch_inputs: tuple[str, uuid.UUID]
) -> None:
    first = await make_batch(
        client, batch_inputs, key="g1", run_max_attempts=2, eval_max_attempts=4
    )
    assert first.status_code == 202

    same = await make_batch(client, batch_inputs, key="g1", run_max_attempts=2, eval_max_attempts=4)
    assert same.status_code == 200 and same.json()["id"] == first.json()["id"]
    for different in (
        {"run_max_attempts": 3, "eval_max_attempts": 4},
        {"run_max_attempts": 2, "eval_max_attempts": 5},
        {"run_max_attempts": 2},  # eval omitted = the default 3, not 4
        {},
    ):
        resp = await make_batch(client, batch_inputs, key="g1", **different)
        assert resp.status_code == 409, (different, resp.text)
    assert await run_budgets(engine, first.json()["id"]) == {2}  # no extra runs either


@pytest.mark.docker
async def test_the_batch_grading_budget_reaches_the_evaluations_a_worker_records(
    client: httpx.AsyncClient,
    sessionmaker: SM,
    queue: RunQueue,
    artifact_store: LocalArtifactStore,
    engine: AsyncEngine,
) -> None:
    """eval_max_attempts travels batch -> evaluation row through the real worker (the batch's
    value, not the worker's setting, which build() leaves at 1)."""
    ds, _ = await frozen_dataset(client)
    version = await agent_version(sessionmaker)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE agent_versions SET model_settings = CAST(:s AS jsonb) WHERE id = :v"),
            {"s": '{"script": [{"type": "turn", "text": "done"}]}', "v": version},
        )
    resp = await client.post(
        f"{API}/evaluations",
        json={
            "name": "b", "dataset_id": ds, "agent_version_id": str(version),
            "repetitions": 1, "run_max_attempts": 2, "eval_max_attempts": 5,
        },
    )  # fmt: skip
    assert resp.status_code == 202, resp.text
    ex, _ = build(sessionmaker, queue, artifact_store)
    for _ in range(resp.json()["progress"]["runs_total"]):
        await handle_next(ex, queue)

    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT e.max_eval_attempts, r.max_attempts FROM attempt_evaluations e"
                    " JOIN run_attempts a ON a.id = e.attempt_id JOIN runs r ON r.id = a.run_id"
                )
            )
        ).all()
    assert rows and {tuple(r) for r in rows} == {(5, 2)}
