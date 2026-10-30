"""Public demo mode (AGENTEVAL_PUBLIC_DEMO=true): no API key, and the only writes are
launching evaluations and runs of what already exists, cancelling and replaying, within the
demo's caps. Everything else that writes answers 403 before it is looked at."""

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agenteval_api.auth import PUBLIC_DEMO_DETAIL, ScopeGuard
from agenteval_api.main import create_app
from agenteval_core.auth import Scope
from agenteval_core.config import Settings
from agenteval_core.services.api_keys import ApiKeyService
from tests.integration.conftest import bearer
from tests.integration.eval_seed import agent_version, create_evaluation, frozen_dataset
from tests.integration.test_auth_api import concrete, guards, v1_routes

SM = async_sessionmaker[AsyncSession]

# The five routes a visitor may call; nothing else that writes.
OPEN_WRITES = {
    ("POST", "/api/v1/runs"),
    ("POST", "/api/v1/runs/{run_id}/cancel"),
    ("POST", "/api/v1/runs/{run_id}/replay"),
    ("POST", "/api/v1/evaluations"),
    ("POST", "/api/v1/evaluations/{evaluation_id}/cancel"),
}


def demo_app(settings: Settings, **update: object) -> FastAPI:
    return create_app(
        settings.model_copy(
            update={"public_demo": True, "demo_max_repetitions": 2, "demo_max_active_runs": 7}
            | update
        )
    )


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    app = demo_app(settings)
    async with app.router.lifespan_context(app):
        yield app


def client_for(app: FastAPI, headers: dict[str, str] | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers or {}
    )


def assert_demo_forbidden(resp: httpx.Response) -> None:
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == PUBLIC_DEMO_DETAIL


def is_open_to_visitors(app: FastAPI, method: str, path: str) -> bool:
    [route] = [r for m, p, r in v1_routes(app) if (m, p) == (method, path)]
    [guard] = guards(route.dependant)
    return guard.public_demo


def test_exactly_the_launch_cancel_and_replay_routes_are_open_to_visitors(app: FastAPI) -> None:
    opened = {(m, p) for m, p, _ in v1_routes(app) if is_open_to_visitors(app, m, p)}
    assert opened == OPEN_WRITES
    for method, path, route in v1_routes(app):
        [guard] = guards(route.dependant)
        assert isinstance(guard, ScopeGuard)
        if guard.public_demo:  # still the write scope for a real key
            assert guard.scope is Scope.WRITE, (method, path)


async def test_every_other_write_and_admin_route_answers_403_even_with_an_admin_key(
    app: FastAPI, sessionmaker: SM
) -> None:
    async with sessionmaker() as s:
        admin = (await ApiKeyService(s).create("admin", list(Scope))).plaintext
    closed = [
        (m, p)
        for m, p, r in v1_routes(app)
        if guards(r.dependant)[0].scope is not Scope.READ and (m, p) not in OPEN_WRITES
    ]
    assert ("POST", "/api/v1/agents") in closed and ("GET", "/api/v1/keys") in closed
    assert ("POST", "/api/v1/datasets/{dataset_id}/freeze") in closed
    for headers in ({}, bearer(admin)):
        async with client_for(app, headers) as c:
            for method, path in closed:
                resp = await c.request(
                    method, concrete(path), json={} if method == "POST" else None
                )
                assert_demo_forbidden(resp)


async def test_health_reports_the_mode_and_its_limits(app: FastAPI, settings: Settings) -> None:
    async with client_for(app) as c:
        assert (await c.get("/health")).json() == {
            "status": "ok",
            "read_only": False,
            "public_demo": True,
            "demo_limits": {"max_repetitions": 2, "max_active_runs": 7, "concurrent_runs": 2},
        }
    plain = create_app(settings)
    async with plain.router.lifespan_context(plain), client_for(plain) as c:
        assert (await c.get("/health")).json() == {
            "status": "ok",
            "read_only": False,
            "public_demo": False,
            "demo_limits": None,
        }


async def test_read_only_wins_over_public_demo(settings: Settings) -> None:
    app = demo_app(settings, read_only=True)
    async with app.router.lifespan_context(app), client_for(app) as c:
        health = (await c.get("/health")).json()
        assert (health["read_only"], health["public_demo"], health["demo_limits"]) == (
            True,
            False,
            None,
        )
        resp = await c.post("/api/v1/runs", json={"task_id": str(uuid.uuid4())})
        assert resp.status_code == 403 and resp.json()["detail"] != PUBLIC_DEMO_DETAIL


async def test_a_visitor_launches_cancels_and_is_capped_without_a_key(
    app: FastAPI, client: httpx.AsyncClient, sessionmaker: SM, engine: AsyncEngine
) -> None:
    # Seeded the normal way (an admin key on a regular API): 3 tasks, frozen.
    dataset_id, tasks = await frozen_dataset(client)
    version_id = await agent_version(sessionmaker)

    async with client_for(app, {"Authorization": "Bearer not-a-key"}) as visitor:
        over = await create_evaluation(visitor, dataset_id, version_id, repetitions=3)
        assert over.status_code == 422 and "at most 2 repetitions" in over.json()["detail"]

        ok = await create_evaluation(visitor, dataset_id, version_id, repetitions=2)
        assert ok.status_code == 202, ok.text  # 6 runs queued
        evaluation_id = ok.json()["id"]

        run = await visitor.post(
            "/api/v1/runs", json={"task_id": tasks["A"], "agent_version_id": str(version_id)}
        )
        assert run.status_code == 202, run.text  # 7 of 7

        full = await visitor.post(
            "/api/v1/runs", json={"task_id": tasks["A"], "agent_version_id": str(version_id)}
        )
        assert full.status_code == 429 and full.headers["Retry-After"] == "30"
        assert "7 runs are queued or running" in full.json()["detail"]
        busy = await create_evaluation(visitor, dataset_id, version_id, repetitions=1)
        assert busy.status_code == 429

        # Cancelling frees the slots again.
        cancelled = await visitor.post(f"/api/v1/evaluations/{evaluation_id}/cancel")
        assert cancelled.status_code == 202, cancelled.text
        assert (await visitor.post(f"/api/v1/runs/{run.json()['id']}/cancel")).status_code == 202
        again = await visitor.post(
            "/api/v1/runs", json={"task_id": tasks["B"], "agent_version_id": str(version_id)}
        )
        assert again.status_code == 202, again.text

        # Replay is open too (it answers on the run's state: this one isn't FAILED).
        replay = await visitor.post(f"/api/v1/runs/{again.json()['id']}/replay")
        assert replay.status_code == 409

        # Nothing a visitor sends creates an agent, a version or a task.
        assert_demo_forbidden(await visitor.post("/api/v1/agents", json={"name": "x"}))
        assert_demo_forbidden(await visitor.post("/api/v1/tasks", json={}))

    async with engine.connect() as conn:
        agents = (await conn.execute(text("SELECT count(*) FROM agents"))).scalar_one()
    assert agents == 1


async def test_visitor_writes_are_rate_limited_per_client(settings: Settings) -> None:
    app = demo_app(settings, rate_limit_write_per_min=1)
    async with app.router.lifespan_context(app), client_for(app) as c:
        body = {"task_id": str(uuid.uuid4())}
        codes = [(await c.post("/api/v1/runs", json=body)).status_code for _ in range(2)]
    assert codes == [404, 429]
