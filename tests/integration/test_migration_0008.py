"""Migration 0008 (evaluation lease and error-reason checks): the backfill of
legacy RUNNING rows without a lease and ERROR rows without a reason, both new
CHECKs, and the downgrade (drops them, no data lost). Schema drift against head
is covered by test_migrations.test_migrations_match_models_exactly."""

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tests.integration.phase5_seed import running_run
from tests.integration.test_migration_0003 import scalar
from tests.integration.test_migrations import migrate

ERROR_ROW = (
    "status, failure_category, error_message, finished_at, duration_ms",
    "'ERROR', 'evaluation_error', 'x', now(), 0",
)


@pytest.fixture
async def at_0007(make_db: Callable[[], Awaitable[str]]) -> AsyncIterator[tuple[str, AsyncEngine]]:
    url = await make_db()
    await migrate(url, "upgrade", "0007")
    engine = create_async_engine(url)
    yield url, engine
    await engine.dispose()


async def _evaluation(
    engine: AsyncEngine, columns: str, values: str, **params: object
) -> uuid.UUID:
    """One evaluation row on a fresh run/attempt, inserted as raw SQL."""
    _, attempt_id = await running_run(engine)
    evaluation_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                f"INSERT INTO attempt_evaluations (id, attempt_id, evaluator_version, {columns})"
                f" VALUES (:id, :a, 'v1', {values})"
            ),
            {"id": evaluation_id, "a": attempt_id, **params},
        )
    return evaluation_id


async def _row(engine: AsyncEngine, evaluation_id: uuid.UUID, column: str) -> object:
    return await scalar(
        engine, f"SELECT {column} FROM attempt_evaluations WHERE id = '{evaluation_id}'"
    )


async def test_upgrade_backfills_unleased_running_and_reasonless_error_rows(
    at_0007: tuple[str, AsyncEngine],
) -> None:
    url, engine = at_0007
    running = await _evaluation(engine, "status", "'RUNNING'")
    leased = await _evaluation(
        engine, "status, lease_expires_at", "'RUNNING', now() + interval '1 h'"
    )
    bare_error = await _evaluation(engine, *ERROR_ROW)
    kinded_error = await _evaluation(
        engine, ERROR_ROW[0] + ", error_kind", ERROR_ROW[1] + ", 'retries_exhausted'"
    )
    reasoned_error = await _evaluation(
        engine,
        ERROR_ROW[0] + ", error_reason, error_kind",
        ERROR_ROW[1] + ", 'sandbox_failed', 'non_retryable'",
    )
    pending = await _evaluation(engine, "status", "'PENDING'")

    await migrate(url, "upgrade", "0008")

    # RUNNING without a lease is recoverable at once: the lease is already expired.
    assert await scalar(
        engine,
        f"SELECT lease_expires_at <= now() FROM attempt_evaluations WHERE id = '{running}'",
    )
    # A real lease is left alone.
    assert await scalar(
        engine,
        f"SELECT lease_expires_at > now() + interval '30 min' FROM attempt_evaluations"
        f" WHERE id = '{leased}'",
    )
    assert await _row(engine, bare_error, "error_reason") == "unclassified"
    assert await _row(engine, bare_error, "error_kind") == "non_retryable"
    assert await _row(engine, kinded_error, "error_reason") == "unclassified"
    assert await _row(engine, kinded_error, "error_kind") == "retries_exhausted"
    assert await _row(engine, reasoned_error, "error_reason") == "sandbox_failed"
    assert await _row(engine, pending, "error_reason") is None
    assert await _row(engine, pending, "lease_expires_at") is None


async def test_checks_reject_a_leaseless_running_row_and_a_reasonless_error(
    at_0007: tuple[str, AsyncEngine],
) -> None:
    url, engine = at_0007
    await migrate(url, "upgrade", "0008")
    await _evaluation(engine, "status, lease_expires_at", "'RUNNING', now()")
    await _evaluation(engine, ERROR_ROW[0] + ", error_reason", ERROR_ROW[1] + ", 'check_error'")
    # A PENDING row may keep the last attempt's reason (G13).
    await _evaluation(
        engine,
        "status, eval_attempts, max_eval_attempts, error_reason",
        "'PENDING', 1, 3, 'sandbox_failed'",
    )
    with pytest.raises(IntegrityError, match="ck_attempt_evaluations_lease"):
        await _evaluation(engine, "status", "'RUNNING'")
    with pytest.raises(IntegrityError, match="ck_attempt_evaluations_lease"):
        await _evaluation(engine, "status, lease_expires_at", "'PENDING', now()")
    with pytest.raises(IntegrityError, match="ck_attempt_evaluations_error_reason"):
        await _evaluation(engine, *ERROR_ROW)


async def test_downgrade_drops_both_checks_and_loses_no_data(
    at_0007: tuple[str, AsyncEngine],
) -> None:
    url, engine = at_0007
    await migrate(url, "upgrade", "0008")
    ok = await _evaluation(
        engine, ERROR_ROW[0] + ", error_reason", ERROR_ROW[1] + ", 'check_error'"
    )
    await migrate(url, "downgrade", "0007")
    assert (
        await scalar(
            engine,
            "SELECT count(*) FROM pg_constraint WHERE conname IN"
            " ('ck_attempt_evaluations_lease', 'ck_attempt_evaluations_error_reason')",
        )
        == 0
    )
    assert await _row(engine, ok, "error_reason") == "check_error"
    await _evaluation(engine, *ERROR_ROW)  # allowed again at 0007
    await migrate(url, "upgrade", "0008")  # and back: the reasonless row is backfilled
    assert (
        await scalar(
            engine, "SELECT count(*) FROM attempt_evaluations WHERE error_reason = 'unclassified'"
        )
        == 1
    )
