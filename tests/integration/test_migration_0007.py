"""Migration 0007 (workers stop reason): the backfill of already-stopped
workers to 'shutdown', both CHECKs, and the downgrade (drops the column, but
refuses while any worker is recorded as lost). Schema drift against head is
covered by test_migrations.test_migrations_match_models_exactly."""

from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tests.integration.test_migration_0003 import scalar
from tests.integration.test_migrations import migrate


@pytest.fixture
async def at_0006(make_db: Callable[[], Awaitable[str]]) -> AsyncIterator[tuple[str, AsyncEngine]]:
    url = await make_db()
    await migrate(url, "upgrade", "0006")
    engine = create_async_engine(url)
    yield url, engine
    await engine.dispose()


async def _worker(engine: AsyncEngine, worker_id: str, *, stopped: bool, **extra: str) -> None:
    columns = "id, hostname, pid, version, concurrency, stopped_at" + "".join(
        f", {k}" for k in extra
    )
    values = ":id, 'h', 1, 't', 1, " + ("now()" if stopped else "NULL")
    values += "".join(f", '{v}'" for v in extra.values())
    async with engine.begin() as conn:
        await conn.execute(
            text(f"INSERT INTO workers ({columns}) VALUES ({values})"), {"id": worker_id}
        )


async def test_upgrade_backfills_stopped_workers_as_shutdown(
    at_0006: tuple[str, AsyncEngine],
) -> None:
    url, engine = at_0006
    await _worker(engine, "stopped", stopped=True)
    await _worker(engine, "running", stopped=False)
    await migrate(url, "upgrade", "0007")
    assert (
        await scalar(engine, "SELECT stop_reason FROM workers WHERE id = 'stopped'") == "shutdown"
    )
    assert await scalar(engine, "SELECT stop_reason FROM workers WHERE id = 'running'") is None


async def test_checks_reject_a_bad_or_inconsistent_stop_reason(
    at_0006: tuple[str, AsyncEngine],
) -> None:
    url, engine = at_0006
    await migrate(url, "upgrade", "0007")
    await _worker(engine, "lost", stopped=True, stop_reason="lost")
    with pytest.raises(IntegrityError, match="ck_workers_stop_reason"):
        await _worker(engine, "bad", stopped=True, stop_reason="crashed")
    with pytest.raises(IntegrityError, match="ck_workers_stop_reason_iff_stopped"):
        await _worker(engine, "reason-without-stop", stopped=False, stop_reason="lost")
    with pytest.raises(IntegrityError, match="ck_workers_stop_reason_iff_stopped"):
        await _worker(engine, "stop-without-reason", stopped=True)


async def test_downgrade_drops_the_column_and_constraints(
    at_0006: tuple[str, AsyncEngine],
) -> None:
    url, engine = at_0006
    await migrate(url, "upgrade", "0007")
    await _worker(engine, "w", stopped=True, stop_reason="shutdown")
    await migrate(url, "downgrade", "0006")
    assert (
        await scalar(
            engine,
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'workers' AND column_name = 'stop_reason'",
        )
        == 0
    )
    assert await scalar(engine, "SELECT count(*) FROM workers") == 1
    await migrate(url, "upgrade", "0007")  # and back: the row backfills to 'shutdown'
    assert await scalar(engine, "SELECT stop_reason FROM workers WHERE id = 'w'") == "shutdown"


async def test_downgrade_refuses_while_a_worker_is_recorded_lost(
    at_0006: tuple[str, AsyncEngine],
) -> None:
    url, engine = at_0006
    await migrate(url, "upgrade", "0007")
    await _worker(engine, "w", stopped=True, stop_reason="lost")
    with pytest.raises(RuntimeError, match="refusing to downgrade 0007"):
        await migrate(url, "downgrade", "0006")
    assert await scalar(engine, "SELECT stop_reason FROM workers WHERE id = 'w'") == "lost"
