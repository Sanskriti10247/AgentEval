"""Integration fixtures: real Postgres 16 + Redis 7 via testcontainers.

The schema is created by running the real Alembic migrations (not create_all),
so the migration itself is under test.
"""

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

from agenteval_api.main import create_app
from agenteval_core.config import Settings
from agenteval_core.db import create_engine, create_sessionmaker
from agenteval_core.queue.streams import RunQueue

ROOT = Path(__file__).resolve().parents[2]
SANDBOX_IMAGE = "agenteval-sandbox:py312"


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    with PostgresContainer("postgres:16", driver="asyncpg") as pg:
        url = pg.get_connection_url()
        cfg = Config(str(ROOT / "packages/core/alembic.ini"))
        cfg.attributes["database_url"] = url
        cfg.attributes["configure_logger"] = False
        command.upgrade(cfg, "head")
        yield url


@pytest.fixture(scope="session")
def redis_url() -> Iterator[str]:
    with RedisContainer("redis:7") as r:
        yield f"redis://{r.get_container_host_ip()}:{r.get_exposed_port(6379)}/0"


@pytest.fixture(scope="session")
def settings(pg_url: str, redis_url: str) -> Settings:
    return Settings(
        database_url=pg_url,
        redis_url=redis_url,
        image_allowlist=[SANDBOX_IMAGE, "agenteval-missing-image:none"],
        log_json=False,
        worker_block_ms=200,
    )


@pytest.fixture(scope="session")
async def engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    eng = create_engine(settings.database_url)
    yield eng
    await eng.dispose()


@pytest.fixture(scope="session")
def sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_sessionmaker(engine)


@pytest.fixture
async def redis(settings: Settings) -> AsyncIterator[Redis]:
    r = Redis.from_url(settings.redis_url)
    yield r
    await r.aclose()


@pytest.fixture(autouse=True)
async def clean_state(engine: AsyncEngine, settings: Settings) -> AsyncIterator[None]:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE scores, attempt_evaluations, attempt_artifacts, trajectory_events, "
                "run_attempts, runs, evaluations, tasks, datasets, agent_versions, agents"
            )
        )
    r = Redis.from_url(settings.redis_url)
    await r.flushall()
    await r.aclose()
    yield


@pytest.fixture
async def queue(redis: Redis, settings: Settings) -> RunQueue:
    q = RunQueue(redis, settings.dispatch_stream, settings.consumer_group)
    await q.ensure_group()
    return q


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            c.app = app  # type: ignore[attr-defined]
            yield c


def task_body(**overrides: object) -> dict[str, object]:
    return {
        "name": "exit-three",
        "image": SANDBOX_IMAGE,
        "command": ["python", "main.py"],
        "files": {"main.py": "print('hello from sandbox')\nraise SystemExit(3)\n"},
        "timeout_s": 20,
        **overrides,
    }


async def create_task(client: httpx.AsyncClient, **overrides: object) -> str:
    resp = await client.post("/api/v1/tasks", json=task_body(**overrides))
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.fixture
async def make_db(pg_url: str) -> AsyncIterator[Callable[[], Awaitable[str]]]:
    """Factory: each call creates an empty database and returns its URL.
    All of them are dropped when the test ends."""
    admin = create_async_engine(pg_url, isolation_level="AUTOCOMMIT")
    created: list[str] = []

    async def create() -> str:
        name = f"mig_{uuid.uuid4().hex[:12]}"
        async with admin.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
        created.append(name)
        return make_url(pg_url).set(database=name).render_as_string(hide_password=False)

    yield create
    async with admin.connect() as conn:
        for name in created:
            await conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
    await admin.dispose()
