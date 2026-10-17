"""Migration 0009 (run_replays): the table and its constraints, and a downgrade that
refuses while any replay is recorded (it would forget which requests were applied).
Schema drift against the models is covered by test_migrations."""

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tests.integration.phase5_seed import running_run
from tests.integration.test_migration_0003 import scalar
from tests.integration.test_migrations import migrate


@pytest.fixture
async def at_0009(make_db: Callable[[], Awaitable[str]]) -> AsyncIterator[tuple[str, AsyncEngine]]:
    url = await make_db()
    await migrate(url, "upgrade", "0009")
    engine = create_async_engine(url)
    yield url, engine
    await engine.dispose()


async def replay(
    engine: AsyncEngine,
    run_id: uuid.UUID,
    *,
    no: int = 1,
    additional: int = 1,
    key: str | None = None,
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO run_replays (id, run_id, idempotency_key, replay_no,"
                " additional_attempts) VALUES (gen_random_uuid(), :r, :k, :n, :a)"
            ),
            {"r": run_id, "k": key, "n": no, "a": additional},
        )


async def test_upgrade_creates_an_empty_table_with_its_constraints(
    at_0009: tuple[str, AsyncEngine],
) -> None:
    _, engine = at_0009
    assert await scalar(engine, "SELECT count(*) FROM run_replays") == 0
    names = await scalar(
        engine,
        "SELECT string_agg(conname, ',' ORDER BY conname) FROM pg_constraint"
        " WHERE conrelid = 'run_replays'::regclass",
    )
    assert names == (
        "ck_run_replays_additional_attempts,ck_run_replays_replay_no,"
        "fk_run_replays_run_id_runs,pk_run_replays,uq_run_replays_idempotency_key,"
        "uq_run_replays_run_id_replay_no"
    )


async def test_constraints(at_0009: tuple[str, AsyncEngine]) -> None:
    _, engine = at_0009
    run_id, _ = await running_run(engine)
    await replay(engine, run_id, key="k")
    await replay(engine, run_id, no=2)  # keyless replays never collide on the key
    await replay(engine, run_id, no=3)
    with pytest.raises(IntegrityError, match="uq_run_replays_idempotency_key"):
        other, _ = await running_run(engine)
        await replay(engine, other, key="k")
    with pytest.raises(IntegrityError, match="uq_run_replays_run_id_replay_no"):
        await replay(engine, run_id, no=2)
    for bad in (0, 10):
        with pytest.raises(IntegrityError, match="ck_run_replays_additional_attempts"):
            await replay(engine, run_id, no=9, additional=bad)
    with pytest.raises(IntegrityError, match="ck_run_replays_replay_no"):
        await replay(engine, run_id, no=0)
    with pytest.raises(IntegrityError, match="fk_run_replays_run_id_runs"):
        await replay(engine, uuid.uuid4(), no=1)


async def test_downgrade_drops_the_empty_table_and_upgrade_restores_it(
    at_0009: tuple[str, AsyncEngine],
) -> None:
    url, engine = at_0009
    await migrate(url, "downgrade", "0008")
    assert await scalar(engine, "SELECT to_regclass('run_replays') IS NULL")
    await migrate(url, "upgrade", "0009")
    assert await scalar(engine, "SELECT count(*) FROM run_replays") == 0


async def test_downgrade_refuses_while_a_replay_is_recorded(
    at_0009: tuple[str, AsyncEngine],
) -> None:
    url, engine = at_0009
    run_id, _ = await running_run(engine)
    await replay(engine, run_id, key="k")
    with pytest.raises(RuntimeError, match="refusing to downgrade 0009: 1 replay"):
        await migrate(url, "downgrade", "0008")
    assert await scalar(engine, "SELECT count(*) FROM run_replays") == 1  # nothing was dropped

    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM run_replays"))
    await migrate(url, "downgrade", "0008")  # "remove the rows first" works
    assert await scalar(engine, "SELECT to_regclass('run_replays') IS NULL")
