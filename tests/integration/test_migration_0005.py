"""Migration 0005 (attempt failure_reason required): the backfill for a
FINISHED+failed attempt that still has no reason (a blue-green deploy window
against an already-migrated-to-0004 database), the new CHECK itself, and the
downgrade (drops only the constraint). Schema drift against head is covered
by test_migrations.test_migrations_match_models_exactly."""

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


async def row(engine: AsyncEngine, sql: str, **params: object) -> object:
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params)).one()


@pytest.fixture
async def at_0004(make_db: Callable[[], Awaitable[str]]) -> AsyncIterator[tuple[str, AsyncEngine]]:
    url = await make_db()
    await migrate(url, "upgrade", "0004")
    engine = create_async_engine(url)
    yield url, engine
    await engine.dispose()


async def _finished_sandbox_attempt(engine: AsyncEngine, *, failure_reason: str | None) -> Ids:
    """A FINISHED attempt with a failed (SANDBOX_ERROR) outcome, as it would
    look if written before Step 3's code started recording a reason (0004
    never required one)."""
    ids: Ids = {"run": uuid.uuid4(), "attempt": uuid.uuid4()}
    async with engine.begin() as conn:
        task = await insert_task(conn)
        await conn.execute(
            text(
                "INSERT INTO runs (id, task_id, status, outcome, attempt_count) "
                "VALUES (:run, :task, 'FAILED', 'SANDBOX_ERROR', 1)"
            ),
            {"run": ids["run"], "task": task},
        )
        await conn.execute(
            text(
                "INSERT INTO run_attempts (id, run_id, attempt_no, worker_id, status, outcome, "
                "oom_killed, stdout, stderr, stdout_truncated, stderr_truncated, started_at, "
                "failure_reason, retryable) "
                "VALUES (:attempt, :run, 1, 'w1', 'FINISHED', 'SANDBOX_ERROR', false, '', '', "
                "false, false, now(), :reason, :retryable)"
            ),
            {
                "attempt": ids["attempt"],
                "run": ids["run"],
                "reason": failure_reason,
                "retryable": None if failure_reason is None else False,
            },
        )
    return ids


async def test_upgrade_backfills_an_attempt_still_missing_a_reason(
    at_0004: tuple[str, AsyncEngine],
) -> None:
    url, engine = at_0004
    ids = await _finished_sandbox_attempt(engine, failure_reason=None)
    await migrate(url, "upgrade", "0005")
    got = await row(
        engine,
        "SELECT failure_reason, retryable FROM run_attempts WHERE id = :id",
        id=ids["attempt"],
    )
    assert tuple(got) == ("unclassified", False)


async def test_upgrade_leaves_an_already_classified_attempt_alone(
    at_0004: tuple[str, AsyncEngine],
) -> None:
    url, engine = at_0004
    ids = await _finished_sandbox_attempt(engine, failure_reason="sandbox_create_failed")
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE run_attempts SET retryable = true WHERE id = :id"), {"id": ids["attempt"]}
        )
    await migrate(url, "upgrade", "0005")
    got = await row(
        engine,
        "SELECT failure_reason, retryable FROM run_attempts WHERE id = :id",
        id=ids["attempt"],
    )
    assert tuple(got) == ("sandbox_create_failed", True)


async def test_check_rejects_a_finished_failure_with_no_reason(
    at_0004: tuple[str, AsyncEngine],
) -> None:
    url, engine = at_0004
    await migrate(url, "upgrade", "0005")
    with pytest.raises(IntegrityError, match="ck_run_attempts_reason"):
        await _finished_sandbox_attempt(engine, failure_reason=None)


async def test_check_accepts_a_finished_failure_with_a_reason(
    at_0004: tuple[str, AsyncEngine],
) -> None:
    url, engine = at_0004
    await migrate(url, "upgrade", "0005")
    ids = await _finished_sandbox_attempt(engine, failure_reason="sandbox_lost")
    assert (
        await scalar(
            engine, "SELECT failure_reason FROM run_attempts WHERE id = :id", id=ids["attempt"]
        )
        == "sandbox_lost"
    )


async def test_downgrade_drops_only_the_constraint(at_0004: tuple[str, AsyncEngine]) -> None:
    url, engine = at_0004
    await migrate(url, "upgrade", "0005")
    await migrate(url, "downgrade", "0004")
    # The constraint is gone: the same insert that 0005 rejected now succeeds.
    ids = await _finished_sandbox_attempt(engine, failure_reason=None)
    assert (
        await scalar(
            engine, "SELECT failure_reason FROM run_attempts WHERE id = :id", id=ids["attempt"]
        )
        is None
    )
    # Nothing else from 0004 was touched: the T-terminal functions still exist.
    assert (
        await scalar(
            engine, "SELECT count(*) FROM pg_proc WHERE proname IN ('p5_finish','p5_cancel')"
        )
        == 2
    )
    await migrate(url, "upgrade", "0005")
