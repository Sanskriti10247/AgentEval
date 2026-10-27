"""Read-only mode (AGENTEVAL_READ_ONLY=true), the public demo's API.

The sweep runs over every `/api/v1` route found in the app: anything that needs more
than the read scope answers 403 with or without a key, reads need no key, and the
probes pass with no worker running.
"""

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agenteval_api.auth import READ_ONLY_DETAIL
from agenteval_api.main import create_app
from agenteval_core.auth import Scope
from agenteval_core.config import Settings
from agenteval_core.services.api_keys import ApiKeyService
from tests.integration.conftest import bearer
from tests.integration.test_auth_api import concrete, expected_scope, v1_routes

SM = async_sessionmaker[AsyncSession]


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    app = create_app(settings.model_copy(update={"read_only": True}))
    async with app.router.lifespan_context(app):
        yield app


def client_for(app: FastAPI, headers: dict[str, str] | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers or {}
    )


def assert_read_only_forbidden(resp: httpx.Response) -> None:
    assert resp.status_code == 403, resp.text
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.json() == {
        "type": "about:blank",
        "title": "Forbidden",
        "status": 403,
        "detail": READ_ONLY_DETAIL,
    }


async def test_every_route_beyond_read_answers_403_even_with_an_admin_key(
    app: FastAPI, sessionmaker: SM
) -> None:
    async with sessionmaker() as s:
        admin = (await ApiKeyService(s).create("admin", list(Scope))).plaintext
    writes = [(m, p) for m, p, _ in v1_routes(app) if expected_scope(m, p) is not Scope.READ]
    assert len(writes) >= 10, "route enumeration found too few write routes"
    assert any(p == "/api/v1/keys" and m == "GET" for m, p in writes)  # admin reads too
    for headers in ({}, bearer(admin)):
        async with client_for(app, headers) as c:
            for method, path in writes:
                resp = await c.request(
                    method, concrete(path), json={} if method == "POST" else None
                )
                assert_read_only_forbidden(resp)


async def test_reads_need_no_key_and_ignore_an_invalid_one(app: FastAPI) -> None:
    reads = [(m, p) for m, p, _ in v1_routes(app) if expected_scope(m, p) is Scope.READ]
    for headers in ({}, {"Authorization": "Bearer not-a-key"}):
        async with client_for(app, headers) as c:
            for method, path in reads:
                resp = await c.request(method, concrete(path))
                # whatever the route says (200, 404, 422), never an auth answer
                assert resp.status_code not in (401, 403), (method, path, resp.text)
            for path in ("/api/v1/agents", "/api/v1/evaluations", "/api/v1/runs"):
                assert (await c.get(path)).status_code == 200, path
            overview = await c.get("/api/v1/stats/overview")
            assert overview.status_code == 200
            assert overview.json()["active_workers"] == 0


async def test_probes_pass_without_a_worker_and_health_reports_read_only(app: FastAPI) -> None:
    async with client_for(app) as c:
        assert (await c.get("/health")).json() == {"status": "ok", "read_only": True}
        ready = await c.get("/ready")
        assert ready.status_code == 200, ready.text
        assert ready.json() == {"ready": True, "checks": {"postgres": "ok", "redis": "ok"}}
        assert (await c.get("/metrics")).status_code == 200


async def test_a_forbidden_write_changes_nothing(app: FastAPI) -> None:
    async with client_for(app) as c:
        resp = await c.post("/api/v1/agents", json={"name": f"demo-{uuid.uuid4().hex[:6]}"})
        assert_read_only_forbidden(resp)
        assert (await c.get("/api/v1/agents")).json()["agents"] == []


async def test_reads_are_still_rate_limited_per_client(settings: Settings) -> None:
    app = create_app(settings.model_copy(update={"read_only": True, "rate_limit_read_per_min": 2}))
    async with app.router.lifespan_context(app), client_for(app) as c:
        codes = [(await c.get("/api/v1/agents")).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
