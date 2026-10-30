"""Migration 0011 (evaluation budget): evaluations.max_cost_usd with its CHECK, the
evaluation_budget_exhausted attempt reason, and a downgrade that refuses while an attempt
carries that reason. Schema drift against the models is covered by test_migrations."""

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tests.integration.phase5_seed import running_run
from tests.integration.test_migration_0003 import scalar, version_of
from tests.integration.test_migrations import migrate

FINISH = """UPDATE run_attempts SET status = 'FINISHED', outcome = 'INTERNAL_ERROR',
    failure_reason = :reason, retryable = false, finished_at = now() WHERE id = :id"""


@pytest.fixture
async def at_0011(make_db: Callable[[], Awaitable[str]]) -> AsyncIterator[tuple[str, AsyncEngine]]:
    url = await make_db()
    await migrate(url, "upgrade", "0011")
    engine = create_async_engine(url)
    yield url, engine
    await engine.dispose()


async def finish(engine: AsyncEngine, attempt_id: uuid.UUID, reason: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(FINISH), {"id": attempt_id, "reason": reason})


async def test_upgrade_adds_a_nullable_positive_cap(at_0011: tuple[str, AsyncEngine]) -> None:
    _, engine = at_0011
    column = await scalar(
        engine,
        "SELECT data_type || ',' || numeric_precision || ',' || numeric_scale || ',' || is_nullable"
        " FROM information_schema.columns"
        " WHERE table_name = 'evaluations' AND column_name = 'max_cost_usd'",
    )
    assert column == "numeric,12,6,YES"
    check = await scalar(
        engine,
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
        " WHERE conname = 'ck_evaluations_max_cost_usd'",
    )
    assert check == "CHECK ((max_cost_usd > (0)::numeric))"


async def test_the_new_reason_is_accepted_and_others_still_refused(
    at_0011: tuple[str, AsyncEngine],
) -> None:
    _, engine = at_0011
    _, attempt = await running_run(engine)
    await finish(engine, attempt, "evaluation_budget_exhausted")
    _, other = await running_run(engine)
    with pytest.raises(IntegrityError, match="ck_run_attempts_failure_reason_values"):
        await finish(engine, other, "budget_gone")


async def test_downgrade_refuses_while_an_attempt_has_the_reason_then_round_trips(
    at_0011: tuple[str, AsyncEngine],
) -> None:
    url, engine = at_0011
    _, attempt = await running_run(engine)
    await finish(engine, attempt, "evaluation_budget_exhausted")
    with pytest.raises(RuntimeError, match="refusing to downgrade 0011: 1 attempt"):
        await migrate(url, "downgrade", "0010")
    assert await version_of(engine) == "0011"

    await finish(engine, attempt, "internal_unexpected")
    await migrate(url, "downgrade", "0010")
    assert await version_of(engine) == "0010"
    assert not await scalar(
        engine,
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns"
        " WHERE table_name = 'evaluations' AND column_name = 'max_cost_usd')",
    )
    _, again = await running_run(engine)
    with pytest.raises(IntegrityError, match="ck_run_attempts_failure_reason_values"):
        await finish(engine, again, "evaluation_budget_exhausted")
    await migrate(url, "upgrade", "head")
    assert await version_of(engine) == "0011"
