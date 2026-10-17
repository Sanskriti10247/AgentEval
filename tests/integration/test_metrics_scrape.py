"""Scrape-time gauges (ADR-010 A) against real Postgres and Redis: the values
match seeded rows and stream entries, a failing source is counted and never
fails the scrape, and /metrics serves the same numbers over HTTP."""

import asyncio
import uuid

import httpx
from prometheus_client import REGISTRY
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agenteval_api.main import create_app
from agenteval_core.config import Settings
from agenteval_core.db import create_engine, create_sessionmaker
from agenteval_core.observability import scrape
from agenteval_core.queue.streams import RunQueue
from tests.integration.phase5_seed import insert_task, running_run

SM = async_sessionmaker[AsyncSession]
DEAD_REDIS = "redis://127.0.0.1:1/0"
DEAD_PG = "postgresql+asyncpg://x:x@127.0.0.1:1/x"


def gauge(name: str, **labels: str) -> float | None:
    return REGISTRY.get_sample_value(name, labels)


def errors(source: str) -> float:
    return gauge("agenteval_scrape_errors_total", source=source) or 0.0


async def refresh(sm: SM, redis: Redis, settings: Settings) -> None:
    await scrape.refresh(
        sm,
        redis,
        streams=[settings.dispatch_stream, settings.evaluation_stream],
        group=settings.consumer_group,
        live_worker_window_s=90,
    )


async def seed_runs(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        task = await insert_task(conn)
        for age_s in (5, 100):  # the oldest QUEUED run is 100 s old
            await conn.execute(
                text(
                    "INSERT INTO runs (id, task_id, status, attempt_count, created_at) VALUES"
                    " (:id, :t, 'QUEUED', 0, now() - make_interval(secs => :age))"
                ),
                {"id": uuid.uuid4(), "t": task, "age": age_s},
            )
        await conn.execute(
            text(
                "INSERT INTO runs (id, task_id, status, attempt_count, max_attempts,"
                " next_attempt_at) VALUES (:id, :t, 'RETRYING', 1, 3, now() - interval '30 s')"
            ),
            {"id": uuid.uuid4(), "t": task},
        )
        await conn.execute(
            text(
                "INSERT INTO runs (id, task_id, status, attempt_count, outcome, finished_at)"
                " VALUES (:id, :t, 'COMPLETED', 1, 'SUCCESS', now())"
            ),
            {"id": uuid.uuid4(), "t": task},
        )


async def seed_evaluations(engine: AsyncEngine) -> None:
    for _ in range(2):  # two PENDING, each on its own attempt
        _, attempt_id = await running_run(engine)
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO attempt_evaluations (id, attempt_id, status, evaluator_version,"
                    " max_eval_attempts) VALUES (:id, :a, 'PENDING', 'v1', 3)"
                ),
                {"id": uuid.uuid4(), "a": attempt_id},
            )
    _, attempt_id = await running_run(engine)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO attempt_evaluations (id, attempt_id, status, evaluator_version,"
                " max_eval_attempts, finished_at) VALUES (:id, :a, 'SKIPPED', 'v1', 3, now())"
            ),
            {"id": uuid.uuid4(), "a": attempt_id},
        )


async def seed_workers(engine: AsyncEngine) -> None:
    rows = [
        ("w-live-1", "now()", None),
        ("w-live-2", "now() - interval '20 s'", None),
        ("w-stale", "now() - interval '1 hour'", None),  # silent: not live
        ("w-stopped", "now()", "shutdown"),  # stopped: not live
    ]
    async with engine.begin() as conn:
        for worker_id, beat, stop in rows:
            await conn.execute(
                text(
                    "INSERT INTO workers (id, hostname, pid, version, concurrency,"
                    f" last_heartbeat_at, stopped_at, stop_reason) VALUES (:id, 'h', 1, 'v', 2,"
                    f" {beat}, CASE WHEN CAST(:s AS text) IS NULL THEN NULL ELSE now() END, :s)"
                ),
                {"id": worker_id, "s": stop},
            )


async def test_gauges_match_the_seeded_rows_and_streams(
    engine: AsyncEngine, sessionmaker: SM, redis: Redis, queue: RunQueue, settings: Settings
) -> None:
    await seed_runs(engine)
    await seed_evaluations(engine)
    await seed_workers(engine)
    for _ in range(3):
        await queue.publish(uuid.uuid4())
    await queue.read("c1", count=2, block_ms=100)  # delivered, never acked: 2 pending
    before = (errors("postgres"), errors("redis"))

    await refresh(sessionmaker, redis, settings)

    assert gauge("agenteval_runs_by_status", status="QUEUED") == 2
    assert gauge("agenteval_runs_by_status", status="RETRYING") == 1
    assert gauge("agenteval_runs_by_status", status="COMPLETED") == 1
    assert gauge("agenteval_runs_by_status", status="CANCELLED") == 0  # every status is present
    # Three attempts' runs from running_run() are RUNNING.
    assert gauge("agenteval_runs_by_status", status="RUNNING") == 3
    assert gauge("agenteval_evaluations_by_status", status="PENDING") == 2
    assert gauge("agenteval_evaluations_by_status", status="SKIPPED") == 1
    assert gauge("agenteval_backlog", kind="queued_run") == 2
    assert gauge("agenteval_backlog", kind="retrying_run") == 1
    assert gauge("agenteval_backlog", kind="pending_evaluation") == 2
    assert 99 < (gauge("agenteval_backlog_oldest_age_seconds", kind="queued_run") or 0) < 130
    assert 29 < (gauge("agenteval_backlog_oldest_age_seconds", kind="retrying_run") or 0) < 60
    assert (gauge("agenteval_backlog_oldest_age_seconds", kind="pending_evaluation") or 0) >= 0
    assert gauge("agenteval_workers_live") == 2
    assert gauge("agenteval_stream_length", stream=settings.dispatch_stream) == 3
    assert gauge("agenteval_stream_pending", stream=settings.dispatch_stream) == 2
    assert gauge("agenteval_stream_length", stream=settings.evaluation_stream) == 0
    assert (errors("postgres"), errors("redis")) == before


async def test_an_empty_system_reports_zeroes_and_no_errors(
    sessionmaker: SM, redis: Redis, settings: Settings
) -> None:
    """No stream and no group yet (nothing was ever published) is empty, not broken."""
    before = (errors("postgres"), errors("redis"))
    await refresh(sessionmaker, redis, settings)
    assert gauge("agenteval_backlog", kind="queued_run") == 0
    assert gauge("agenteval_backlog_oldest_age_seconds", kind="queued_run") == 0
    assert gauge("agenteval_workers_live") == 0
    assert gauge("agenteval_stream_length", stream=settings.dispatch_stream) == 0
    assert gauge("agenteval_stream_pending", stream=settings.dispatch_stream) == 0
    assert (errors("postgres"), errors("redis")) == before


async def test_a_broken_redis_is_counted_and_postgres_gauges_survive(
    engine: AsyncEngine, sessionmaker: SM, settings: Settings
) -> None:
    await seed_runs(engine)
    before = errors("redis")
    broken = Redis.from_url(DEAD_REDIS)
    try:
        await refresh(sessionmaker, broken, settings)
    finally:
        await broken.aclose()
    assert errors("redis") == before + 1
    assert gauge("agenteval_backlog", kind="queued_run") == 2
    assert gauge("agenteval_stream_length", stream=settings.dispatch_stream) is None


async def test_a_broken_postgres_is_counted_and_redis_gauges_survive(
    redis: Redis, queue: RunQueue, settings: Settings
) -> None:
    await queue.publish(uuid.uuid4())
    before = errors("postgres")
    bad_engine = create_engine(DEAD_PG)
    try:
        await refresh(create_sessionmaker(bad_engine), redis, settings)
    finally:
        await bad_engine.dispose()
    assert errors("postgres") == before + 1
    assert gauge("agenteval_runs_by_status", status="QUEUED") is None
    assert gauge("agenteval_stream_length", stream=settings.dispatch_stream) == 1


async def test_metrics_endpoint_serves_the_same_numbers(
    client: httpx.AsyncClient, engine: AsyncEngine, queue: RunQueue
) -> None:
    await seed_runs(engine)
    await queue.publish(uuid.uuid4())
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert 'agenteval_backlog{kind="queued_run"} 2.0' in resp.text
    assert 'agenteval_runs_by_status{status="RETRYING"} 1.0' in resp.text
    assert 'agenteval_stream_length{stream="runs.dispatch"} 1.0' in resp.text


async def test_metrics_endpoint_is_200_when_redis_is_down(
    settings: Settings, engine: AsyncEngine
) -> None:
    await seed_runs(engine)
    before = errors("redis")
    app = create_app(settings.model_copy(update={"redis_url": DEAD_REDIS}))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/metrics")
    assert resp.status_code == 200
    assert errors("redis") == before + 1
    assert 'agenteval_backlog{kind="queued_run"} 2.0' in resp.text
    assert "agenteval_stream_length{" not in resp.text


async def test_overlapping_scrapes_all_succeed(client: httpx.AsyncClient) -> None:
    responses = await asyncio.gather(*(client.get("/metrics") for _ in range(5)))
    assert [r.status_code for r in responses] == [200] * 5
