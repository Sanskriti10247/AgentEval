"""Migration 0006 (runs lease check): the backfill for a CLAIMED/RUNNING row
still missing a lease (a blue-green deploy window against an
already-migrated-to-0005 database, or a row written directly by a test), the
new CHECK itself, and the downgrade (drops only the constraint). Schema drift
against head is covered by test_migrations.test_migrations_match_models_exactly."""

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tests.integration.phase5_seed import insert_task
from tests.integration.test_migration_0003 import scalar
from tests.integration.test_migrations import migrate

Ids = dict[str, uuid.UUID]


@pytest.fixture
async def at_0005(make_db: Callable[[], Awaitable[str]]) -> AsyncIterator[tuple[str, AsyncEngine]]:
    url = await make_db()
    await migrate(url, "upgrade", "0005")
    engine = create_async_engine(url)
    yield url, engine
    await engine.dispose()


async def _claimed_run(engine: AsyncEngine, *, lease_expires_at: str | None) -> uuid.UUID:
    """A CLAIMED run, as it would look if written before Step 4's claim path
    started setting lease_expires_at (0004/0005 never required one)."""
    run_id = uuid.uuid4()
    async with engine.begin() as conn:
        task_id = await insert_task(conn)
        await conn.execute(
            text(
                "INSERT INTO runs (id, task_id, status, attempt_count, worker_id, claimed_at, "
                "lease_expires_at) VALUES (:id, :task, 'CLAIMED', 0, 'w1', now(), "
                + (lease_expires_at or "NULL")
                + ")"
            ),
            {"id": run_id, "task": task_id},
        )
    return run_id


async def test_upgrade_backfills_a_claimed_run_still_missing_a_lease(
    at_0005: tuple[str, AsyncEngine],
) -> None:
    url, engine = at_0005
    run_id = await _claimed_run(engine, lease_expires_at=None)
    await migrate(url, "upgrade", "0006")
    assert (
        await scalar(
            engine, "SELECT lease_expires_at IS NOT NULL FROM runs WHERE id = :id", id=run_id
        )
        is True
    )


async def test_upgrade_leaves_an_already_leased_run_alone(
    at_0005: tuple[str, AsyncEngine],
) -> None:
    url, engine = at_0005
    run_id = await _claimed_run(engine, lease_expires_at="now() + interval '5 s'")
    before = await scalar(engine, "SELECT lease_expires_at FROM runs WHERE id = :id", id=run_id)
    await migrate(url, "upgrade", "0006")
    after = await scalar(engine, "SELECT lease_expires_at FROM runs WHERE id = :id", id=run_id)
    assert after == before


async def test_check_rejects_a_claimed_run_with_no_lease(at_0005: tuple[str, AsyncEngine]) -> None:
    url, engine = at_0005
    await migrate(url, "upgrade", "0006")
    with pytest.raises(IntegrityError, match="ck_runs_lease"):
        await _claimed_run(engine, lease_expires_at=None)


async def test_check_accepts_a_claimed_run_with_a_lease(at_0005: tuple[str, AsyncEngine]) -> None:
    url, engine = at_0005
    await migrate(url, "upgrade", "0006")
    run_id = await _claimed_run(engine, lease_expires_at="now() + interval '30 s'")
    assert await scalar(engine, "SELECT status FROM runs WHERE id = :id", id=run_id) == "CLAIMED"


async def test_check_rejects_a_queued_run_carrying_a_lease(
    at_0005: tuple[str, AsyncEngine],
) -> None:
    url, engine = at_0005
    await migrate(url, "upgrade", "0006")
    with pytest.raises(IntegrityError, match="ck_runs_lease"):
        async with engine.begin() as conn:
            task_id = await insert_task(conn)
            await conn.execute(
                text(
                    "INSERT INTO runs (id, task_id, status, attempt_count, lease_expires_at) "
                    "VALUES (:id, :task, 'QUEUED', 0, now() + interval '30 s')"
                ),
                {"id": uuid.uuid4(), "task": task_id},
            )


async def test_downgrade_drops_only_the_constraint(at_0005: tuple[str, AsyncEngine]) -> None:
    url, engine = at_0005
    await migrate(url, "upgrade", "0006")
    await migrate(url, "downgrade", "0005")
    # The constraint is gone: the same insert that 0006 rejected now succeeds.
    run_id = await _claimed_run(engine, lease_expires_at=None)
    assert (
        await scalar(engine, "SELECT lease_expires_at FROM runs WHERE id = :id", id=run_id) is None
    )
    # Nothing else from 0004/0005 was touched: the T-terminal functions still exist.
    assert (
        await scalar(
            engine, "SELECT count(*) FROM pg_proc WHERE proname IN ('p5_finish','p5_cancel')"
        )
        == 2
    )
    await migrate(url, "upgrade", "0006")
