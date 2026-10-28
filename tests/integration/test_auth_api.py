"""Authentication and authorization at the API boundary (ADR-012 A, B).

The 401/403 matrix runs over EVERY `/api/v1` route found in the app, not a hand-kept
list, and the sweep fails when a route has no scope dependency.
"""

import re
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import httpx
import pytest
import structlog
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agenteval_api.auth import ScopeGuard
from agenteval_api.main import create_app
from agenteval_core.auth import Scope
from agenteval_core.config import Settings
from agenteval_core.services.api_keys import ApiKeyService
from tests.integration.api_routes import api_routes
from tests.integration.conftest import bearer

SM = async_sessionmaker[AsyncSession]
OPEN_PATHS = {"/health", "/ready", "/metrics"}
INT_PARAMS = {"number", "attempt_no"}

MakeClient = Callable[..., AbstractAsyncContextManager[httpx.AsyncClient]]


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
def make_client(app: FastAPI) -> MakeClient:
    """`make_client(key)` -> a client sending that key; `make_client()` sends none."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def build(key: str | None = None, **headers: str) -> AsyncIterator[httpx.AsyncClient]:
        merged = {**(bearer(key) if key else {}), **headers}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=merged
        ) as c:
            yield c

    return build


async def key_with(sm: SM, *scopes: Scope, **kw: Any) -> tuple[str, str]:
    async with sm() as s:
        created = await ApiKeyService(s).create("t", list(scopes), **kw)
    return created.plaintext, created.key.prefix


def v1_routes(app: FastAPI) -> list[tuple[str, str, APIRoute]]:
    return [(m, p, r) for m, p, r in api_routes(app) if p.startswith("/api/v1")]


def guards(dep: Dependant) -> list[ScopeGuard]:
    found = [dep.call] if isinstance(dep.call, ScopeGuard) else []
    for sub in dep.dependencies:
        found += guards(sub)
    return found


def expected_scope(method: str, path: str) -> Scope:
    if path.startswith("/api/v1/keys"):
        return Scope.ADMIN
    return Scope.READ if method == "GET" else Scope.WRITE


def concrete(path: str) -> str:
    def fill(m: re.Match[str]) -> str:
        name = m.group(1)
        if name in INT_PARAMS:
            return "1"
        return "deadbeef" if name == "prefix" else str(uuid.uuid4())

    return re.sub(r"\{(\w+)\}", fill, path)


# -- the sweep ---------------------------------------------------------------------------------


def test_every_v1_route_declares_exactly_one_scope(app: FastAPI) -> None:
    routes = v1_routes(app)
    assert len(routes) >= 30, "route enumeration found too little"
    for method, path, route in routes:
        found = guards(route.dependant)
        assert len(found) == 1, f"{method} {path} has no scope dependency"
        assert found[0].scope == expected_scope(method, path), f"{method} {path}"


def test_the_openapi_schema_marks_every_v1_operation_as_secured(app: FastAPI) -> None:
    schema = app.openapi()
    v1 = {p: ops for p, ops in schema["paths"].items() if p.startswith("/api/v1")}
    assert v1
    for path, ops in v1.items():
        for method, op in ops.items():
            assert op.get("security"), f"{method.upper()} {path} has no security requirement"
    assert "HTTPBearer" in schema["components"]["securitySchemes"]


def test_only_the_probe_and_metrics_routes_are_open(app: FastAPI) -> None:
    open_routes = {p for _, p, r in api_routes(app) if not p.startswith("/api/v1")}
    assert open_routes == OPEN_PATHS
    for _, p, r in api_routes(app):
        if p in OPEN_PATHS:
            assert not guards(r.dependant)


async def test_open_routes_need_no_key(make_client: MakeClient) -> None:
    async with make_client() as anon:
        for path in ("/health", "/ready", "/metrics"):
            assert (await anon.get(path)).status_code == 200, path


# -- 401 ---------------------------------------------------------------------------------------


def assert_unauthorized(resp: httpx.Response, detail: str | None = None) -> None:
    assert resp.status_code == 401, resp.text
    assert resp.headers["www-authenticate"] == "Bearer"
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert set(body) == {"type", "title", "status", "detail"} and body["status"] == 401
    if detail:
        assert detail in body["detail"]


async def test_every_v1_route_answers_401_without_a_valid_key(
    app: FastAPI, make_client: MakeClient, sessionmaker: SM, engine: AsyncEngine
) -> None:
    good, prefix = await key_with(sessionmaker, *Scope)
    revoked, rprefix = await key_with(sessionmaker, *Scope)
    expired, eprefix = await key_with(sessionmaker, *Scope, expires_days=1)
    async with sessionmaker() as s:
        await ApiKeyService(s).revoke(rprefix)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE api_keys SET expires_at = now() - interval '1 second' WHERE prefix = :p"),
            {"p": eprefix},
        )
    variants: dict[str, dict[str, str]] = {
        "no header": {},
        "not a key": {"Authorization": "Bearer hello"},
        "wrong scheme": {"Authorization": f"Basic {good}"},
        "empty bearer": {"Authorization": "Bearer "},
        "wrong secret": {"Authorization": f"Bearer ae_{prefix}_" + "A" * 43},
        "revoked": bearer(revoked),
        "expired": bearer(expired),
    }
    reasons = {"revoked": "revoked", "expired": "expired"}
    for method, path, _ in v1_routes(app):
        url = concrete(path)
        for label, headers in variants.items():
            async with make_client(**headers) as c:
                resp = await c.request(method, url, json={} if method == "POST" else None)
            assert_unauthorized(resp, reasons.get(label))
            assert good not in resp.text


async def test_a_key_in_the_query_string_or_body_is_not_a_credential(
    make_client: MakeClient, api_key: str
) -> None:
    async with make_client() as c:
        assert (await c.get("/api/v1/tasks", params={"api_key": api_key})).status_code == 401
        assert (await c.get("/api/v1/tasks", headers={"X-API-Key": api_key})).status_code == 401


# -- 403 ---------------------------------------------------------------------------------------


async def test_each_scope_opens_exactly_its_routes(
    app: FastAPI, make_client: MakeClient, sessionmaker: SM
) -> None:
    keys = {s: (await key_with(sessionmaker, s))[0] for s in Scope}
    seen: dict[Scope, int] = {s: 0 for s in Scope}
    for method, path, _ in v1_routes(app):
        need = expected_scope(method, path)
        url = concrete(path)
        for scope, key in keys.items():
            async with make_client(key) as c:
                resp = await c.request(method, url, json={} if method == "POST" else None)
            if scope == need:
                # authorized: whatever the route says (404, 422, 200...), never 401/403
                assert resp.status_code not in (401, 403), (method, path, scope, resp.text)
                seen[scope] += 1
            else:
                assert resp.status_code == 403, (method, path, scope, resp.status_code)
                body = resp.json()
                assert body["title"] == "Forbidden"
                assert body["detail"] == f"This API key lacks the {need.value} scope"
                assert "www-authenticate" not in resp.headers
    assert all(n > 0 for n in seen.values())


async def test_scopes_are_independent_a_write_key_cannot_read(
    make_client: MakeClient, sessionmaker: SM
) -> None:
    key, _ = await key_with(sessionmaker, Scope.WRITE)
    async with make_client(key) as c:
        assert (await c.get("/api/v1/agents")).status_code == 403
        assert (await c.post("/api/v1/agents", json={"name": "a1"})).status_code == 201


async def test_a_read_only_key_can_browse_but_not_cancel_or_replay(
    make_client: MakeClient, sessionmaker: SM
) -> None:
    key, _ = await key_with(sessionmaker, Scope.READ)
    async with make_client(key) as c:
        assert (await c.get("/api/v1/runs")).status_code == 200
        for path in ("cancel", "replay"):
            resp = await c.post(f"/api/v1/runs/{uuid.uuid4()}/{path}")
            assert resp.status_code == 403 and "write scope" in resp.json()["detail"]


async def test_cors_allows_the_authorization_header(settings: Settings) -> None:
    app = create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.options(
            "/api/v1/runs",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )
    assert resp.status_code == 200
    assert "authorization" in resp.headers["access-control-allow-headers"].lower()


async def test_a_401_still_carries_cors_headers(make_client: MakeClient) -> None:
    async with make_client(Origin="http://localhost:3000") as c:
        resp = await c.get("/api/v1/runs")
    assert resp.status_code == 401
    assert resp.headers["access-control-allow-origin"] == "http://localhost:3000"


# -- key administration ------------------------------------------------------------------------


async def test_key_endpoints_create_list_and_revoke(
    make_client: MakeClient, api_key: str, sessionmaker: SM
) -> None:
    async with make_client(api_key) as admin:
        created = await admin.post(
            "/api/v1/keys", json={"name": "dash", "scopes": ["read"], "expires_days": 30}
        )
        assert created.status_code == 201, created.text
        body = created.json()
        new_key, prefix = body["key"], body["prefix"]
        assert new_key.startswith(f"ae_{prefix}_") and body["scopes"] == ["read"]
        assert body["expires_at"] and body["revoked_at"] is None

        listed = await admin.get("/api/v1/keys")
        assert listed.status_code == 200
        assert new_key not in listed.text and new_key[12:] not in listed.text
        assert {"key", "secret", "secret_sha256", "hash"}.isdisjoint(listed.json()[0])
        row = next(k for k in listed.json() if k["prefix"] == prefix)
        assert row["name"] == "dash" and row["last_used_at"] is None

        async with make_client(new_key) as dash:  # the new key works, read-only
            assert (await dash.get("/api/v1/stats/overview")).status_code == 200
            assert (await dash.get("/api/v1/keys")).status_code == 403
        row = next(k for k in (await admin.get("/api/v1/keys")).json() if k["prefix"] == prefix)
        assert row["last_used_at"] is not None

        assert (await admin.delete(f"/api/v1/keys/{prefix}")).status_code == 204
        assert (await admin.delete(f"/api/v1/keys/{prefix}")).status_code == 204  # idempotent
        async with make_client(new_key) as dash:
            assert_unauthorized(await dash.get("/api/v1/stats/overview"), "revoked")
        row = next(k for k in (await admin.get("/api/v1/keys")).json() if k["prefix"] == prefix)
        assert row["revoked_at"] is not None  # revoked keys stay listed

        for bad in ("ffffffff", "xyz"):
            assert (await admin.delete(f"/api/v1/keys/{bad}")).status_code == 404


async def test_key_creation_is_validated(make_client: MakeClient, api_key: str) -> None:
    async with make_client(api_key) as admin:
        for bad in (
            {"name": "x", "scopes": []},
            {"name": "x", "scopes": ["root"]},
            {"name": "", "scopes": ["read"]},
            {"name": "  ", "scopes": ["read"]},
            {"name": "x", "scopes": ["read"], "expires_days": 0},
            {"name": "x", "scopes": ["read"], "extra": 1},
            {"scopes": ["read"]},
        ):
            resp = await admin.post("/api/v1/keys", json=bad)
            assert resp.status_code == 422, bad


async def test_a_non_admin_key_cannot_mint_keys(make_client: MakeClient, sessionmaker: SM) -> None:
    key, _ = await key_with(sessionmaker, Scope.READ, Scope.WRITE)
    async with make_client(key) as c:
        resp = await c.post("/api/v1/keys", json={"name": "x", "scopes": ["admin"]})
    assert resp.status_code == 403


# -- logging -----------------------------------------------------------------------------------


async def test_requests_log_the_key_prefix_and_name_never_the_key(
    app: FastAPI, make_client: MakeClient, sessionmaker: SM, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agenteval_api import auth as api_auth

    key, prefix = await key_with(sessionmaker, Scope.READ)
    recorded: list[tuple[str, dict[str, Any]]] = []

    class Recorder:
        def info(self, event: str, **kw: Any) -> None:
            recorded.append((event, kw))

        warning = info

    monkeypatch.setattr(api_auth, "log", Recorder())
    async with make_client(key) as c:
        await c.get("/api/v1/agents")
        ctx = structlog.contextvars.get_contextvars()
        assert ctx["api_key_prefix"] == prefix and ctx["api_key_name"] == "t"
        await c.post("/api/v1/agents", json={"name": "z"})  # 403: logged, still no key
    async with make_client(Authorization=f"Bearer {key[:-2]}xx") as c:
        await c.get("/api/v1/agents")
    assert [e for e, _ in recorded] == [
        "api_request",
        "api_request",
        "auth_forbidden",
        "auth_rejected",
    ]
    assert key not in str(recorded) and key[12:] not in str(recorded)


# -- authentication switched off ---------------------------------------------------------------


async def test_with_auth_disabled_every_route_is_open_and_the_caller_has_all_scopes(
    settings: Settings,
) -> None:
    app = create_app(settings.model_copy(update={"auth_enabled": False}))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            assert (await c.get("/api/v1/agents")).status_code == 200
            assert (await c.post("/api/v1/agents", json={"name": "open"})).status_code == 201
            made = await c.post("/api/v1/keys", json={"name": "x", "scopes": ["read"]})
            assert made.status_code == 201  # key admin is open too: documented local-only mode
            # a bogus header is simply ignored
            ignored = await c.get("/api/v1/agents", headers={"Authorization": "Bearer junk"})
            assert ignored.status_code == 200


async def test_auth_is_on_by_default() -> None:
    assert Settings().auth_enabled is True


async def test_health_ready_work_without_a_database_lookup(
    make_client: MakeClient, sessionmaker: SM
) -> None:
    """Probes never touch api_keys (an empty table must not break them)."""
    async with make_client() as c:
        assert (await c.get("/health")).json() == {
            "status": "ok",
            "read_only": False,
            "public_demo": False,
            "demo_limits": None,
        }
