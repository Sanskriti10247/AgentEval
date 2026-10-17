"""Rate limiting (ADR-012 C): Redis fixed windows per key, read and write budgets,
429 + headers, window reset, fail-open when Redis is down."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from prometheus_client import REGISTRY
from redis.asyncio import Redis

from agenteval_api.main import create_app
from agenteval_api.ratelimit import RateLimiter
from agenteval_core.auth import Scope
from agenteval_core.config import Settings
from agenteval_core.services.api_keys import ApiKeyService
from tests.integration.conftest import bearer

DEAD_REDIS = "redis://127.0.0.1:1/0"


def counter(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


@asynccontextmanager
async def limited(
    settings: Settings, *, read: int, write: int, **extra: object
) -> AsyncIterator[FastAPI]:
    cfg = settings.model_copy(
        update={"rate_limit_read_per_min": read, "rate_limit_write_per_min": write, **extra}
    )
    app = create_app(cfg)
    async with app.router.lifespan_context(app):
        yield app


def client_for(app: FastAPI, key: str | None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers=bearer(key) if key else {},
    )


async def new_key(sessionmaker, *scopes: Scope) -> str:  # type: ignore[no-untyped-def]
    async with sessionmaker() as s:
        return (await ApiKeyService(s).create("rl", list(scopes) or list(Scope))).plaintext


async def test_over_the_read_budget_is_429_with_retry_after_and_rate_limit_headers(
    settings: Settings, sessionmaker
) -> None:  # type: ignore[no-untyped-def]
    key = await new_key(sessionmaker)
    before = counter("agenteval_rate_limited_total", kind="read")
    async with limited(settings, read=3, write=100) as app, client_for(app, key) as c:
        remaining = []
        for _ in range(3):
            ok = await c.get("/api/v1/agents")
            assert ok.status_code == 200
            assert ok.headers["x-ratelimit-limit"] == "3"
            assert 1 <= int(ok.headers["x-ratelimit-reset"]) <= 60
            remaining.append(ok.headers["x-ratelimit-remaining"])
        assert remaining == ["2", "1", "0"]

        over = await c.get("/api/v1/agents")
        assert over.status_code == 429
        assert over.headers["content-type"].startswith("application/problem+json")
        assert over.json()["title"] == "Too Many Requests"
        assert "3 read requests per minute" in over.json()["detail"]
        assert over.headers["x-ratelimit-limit"] == "3"
        assert over.headers["x-ratelimit-remaining"] == "0"
        assert 1 <= int(over.headers["retry-after"]) <= 60
        assert over.headers["retry-after"] == over.headers["x-ratelimit-reset"]
        assert "www-authenticate" not in over.headers
    assert counter("agenteval_rate_limited_total", kind="read") == before + 1


async def test_read_and_write_budgets_are_separate(settings: Settings, sessionmaker) -> None:  # type: ignore[no-untyped-def]
    key = await new_key(sessionmaker)
    async with limited(settings, read=2, write=2) as app, client_for(app, key) as c:
        assert [(await c.get("/api/v1/agents")).status_code for _ in range(3)] == [200, 200, 429]
        # reads are exhausted, writes are untouched
        posts = [
            (await c.post("/api/v1/agents", json={"name": f"a{i}"})).status_code for i in range(3)
        ]
        assert posts == [201, 201, 429]
        assert (await c.get("/api/v1/agents")).status_code == 429


async def test_each_key_has_its_own_budget(settings: Settings, sessionmaker) -> None:  # type: ignore[no-untyped-def]
    a, b = await new_key(sessionmaker), await new_key(sessionmaker)
    async with limited(settings, read=1, write=1) as app:
        async with client_for(app, a) as ca, client_for(app, b) as cb:
            assert (await ca.get("/api/v1/agents")).status_code == 200
            assert (await ca.get("/api/v1/agents")).status_code == 429
            assert (await cb.get("/api/v1/agents")).status_code == 200


async def test_the_window_resets(settings: Settings, sessionmaker, redis: Redis) -> None:  # type: ignore[no-untyped-def]
    key = await new_key(sessionmaker)
    async with limited(settings, read=2, write=2) as app, client_for(app, key) as c:
        app.state.ratelimiter = RateLimiter(app.state.redis, window_s=1)
        assert [(await c.get("/api/v1/agents")).status_code for _ in range(3)] == [200, 200, 429]
        await asyncio.sleep(1.2)
        again = await c.get("/api/v1/agents")
        assert again.status_code == 200 and again.headers["x-ratelimit-remaining"] == "1"


async def test_the_counter_always_has_an_expiry(
    settings: Settings, sessionmaker, redis: Redis
) -> None:  # type: ignore[no-untyped-def]
    key = await new_key(sessionmaker)
    async with limited(settings, read=5, write=5) as app, client_for(app, key) as c:
        await c.get("/api/v1/agents")
    keys = [k async for k in redis.scan_iter("ratelimit:read:key:*")]
    assert len(keys) == 1
    ttl = await redis.pttl(keys[0])
    assert 0 < ttl <= 60_000
    await redis.persist(keys[0])  # an expiry lost somehow is repaired by the next hit
    async with limited(settings, read=5, write=5) as app, client_for(app, key) as c:
        await c.get("/api/v1/agents")
    assert 0 < await redis.pttl(keys[0]) <= 60_000


async def test_concurrent_requests_share_one_atomic_counter(
    settings: Settings, sessionmaker
) -> None:  # type: ignore[no-untyped-def]
    key = await new_key(sessionmaker)
    async with limited(settings, read=5, write=5) as app, client_for(app, key) as c:
        codes = await asyncio.gather(*(c.get("/api/v1/agents") for _ in range(20)))
    statuses = sorted(r.status_code for r in codes)
    assert statuses == [200] * 5 + [429] * 15


async def test_a_zero_budget_disables_limiting(settings: Settings, sessionmaker) -> None:  # type: ignore[no-untyped-def]
    key = await new_key(sessionmaker)
    async with limited(settings, read=0, write=0) as app, client_for(app, key) as c:
        for _ in range(10):
            resp = await c.get("/api/v1/agents")
            assert resp.status_code == 200 and "x-ratelimit-limit" not in resp.headers


async def test_redis_down_fails_open_and_is_counted(settings: Settings, sessionmaker) -> None:  # type: ignore[no-untyped-def]
    key = await new_key(sessionmaker)
    before = counter("agenteval_rate_limit_errors_total")
    async with limited(settings, read=1, write=1) as app, client_for(app, key) as c:
        app.state.ratelimiter = RateLimiter(Redis.from_url(DEAD_REDIS))
        for _ in range(4):  # far over the budget of 1, all allowed
            resp = await c.get("/api/v1/agents")
            assert resp.status_code == 200
            assert "x-ratelimit-limit" not in resp.headers
    assert counter("agenteval_rate_limit_errors_total") == before + 4


async def test_without_auth_the_budget_is_per_client_ip(settings: Settings) -> None:
    async with limited(settings, read=2, write=2, auth_enabled=False) as app:
        async with client_for(app, None) as c:
            assert [(await c.get("/api/v1/agents")).status_code for _ in range(3)] == [
                200,
                200,
                429,
            ]
        # a different client address has its own budget
        other = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("10.1.2.3", 5000)),
            base_url="http://test",
        )
        async with other as c2:
            assert (await c2.get("/api/v1/agents")).status_code == 200


async def test_open_routes_are_never_limited(settings: Settings) -> None:
    async with limited(settings, read=1, write=1) as app, client_for(app, None) as c:
        for path in ("/health", "/ready", "/metrics"):
            for _ in range(3):
                assert (await c.get(path)).status_code == 200


async def test_unauthenticated_requests_do_not_spend_a_keys_budget(
    settings: Settings, sessionmaker
) -> None:  # type: ignore[no-untyped-def]
    key = await new_key(sessionmaker)
    async with limited(settings, read=2, write=2) as app:
        async with client_for(app, "ae_00000000_" + "A" * 43) as bad:
            for _ in range(5):
                assert (await bad.get("/api/v1/agents")).status_code == 401
        async with client_for(app, key) as c:
            assert (await c.get("/api/v1/agents")).status_code == 200
