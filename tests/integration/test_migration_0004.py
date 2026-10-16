"""Migration 0004 (reliability schema): upgrade with Phase 1-4 data including
the legacy shapes the backfill handles, the downgrade guard, and a clean
round trip. Schema drift against the models is covered by
test_migrations.test_migrations_match_models_exactly."""

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tests.integration.test_migration_0003 import scalar, seed_phase3, version_of
from tests.integration.test_migrations import migrate

Ids = dict[str, uuid.UUID]

# Legacy shapes, inserted at 0003: a stuck CLAIMED run, a stuck RUNNING run with
# its RUNNING attempt, a FINISHED attempt with a failed outcome, evaluations in
# ERROR / RUNNING / PENDING, and a bytea snapshot.
LEGACY_ROWS = [
    """INSERT INTO runs (id, task_id, status, attempt_count, worker_id, claimed_at)
       VALUES (:claimed_run, :cmd_task, 'CLAIMED', 0, 'w-old', now())""",
    """INSERT INTO runs (id, task_id, status, attempt_count, worker_id, claimed_at, started_at)
       VALUES (:running_run, :cmd_task, 'RUNNING', 1, 'w-old', now(), now())""",
    """INSERT INTO run_attempts (id, run_id, attempt_no, worker_id, status, oom_killed, stdout,
         stderr, stdout_truncated, stderr_truncated, started_at)
       VALUES (:running_attempt, :running_run, 1, 'w-old', 'RUNNING', false, '', '', false,
         false, now())""",
    """INSERT INTO runs (id, task_id, status, outcome, attempt_count)
       VALUES (:failed_run, :cmd_task, 'FAILED', 'SANDBOX_ERROR', 1)""",
    """INSERT INTO run_attempts (id, run_id, attempt_no, worker_id, status, outcome, oom_killed,
         stdout, stderr, stdout_truncated, stderr_truncated, error_message, started_at)
       VALUES (:failed_attempt, :failed_run, 1, 'w-old', 'FINISHED', 'SANDBOX_ERROR', false, '',
         '', false, false, 'boom', now())""",
    """INSERT INTO attempt_evaluations (id, attempt_id, status, evaluator_version,
         failure_category, error_message, finished_at)
       VALUES (:error_eval, :agent_attempt, 'ERROR', 'v1', 'evaluation_error', 'x', now())""",
    """INSERT INTO attempt_evaluations (id, attempt_id, status, evaluator_version, started_at)
       VALUES (:running_eval, :failed_attempt, 'RUNNING', 'v1', now())""",
    """INSERT INTO attempt_evaluations (id, attempt_id, status, evaluator_version)
       VALUES (:pending_eval, :cmd_attempt, 'PENDING', 'v1')""",
    """INSERT INTO attempt_artifacts (id, attempt_id, kind, content, size_bytes, sha256)
       VALUES (:artifact, :agent_attempt, 'workspace', '\\x0102'::bytea, 2, repeat('a', 64))""",
]
LEGACY_IDS = (
    "claimed_run",
    "running_run",
    "running_attempt",
    "failed_run",
    "failed_attempt",
    "error_eval",
    "running_eval",
    "pending_eval",
    "artifact",
)


async def seed_legacy(engine: AsyncEngine) -> Ids:
    ids = await seed_phase3(engine)
    ids |= {k: uuid.uuid4() for k in LEGACY_IDS}
    async with engine.begin() as conn:
        for sql in LEGACY_ROWS:
            await conn.execute(text(sql), {k: v for k, v in ids.items() if f":{k}" in sql})
    return ids


@pytest.fixture
async def at_0003(make_db: Callable[[], Awaitable[str]]) -> AsyncIterator[tuple[str, AsyncEngine]]:
    url = await make_db()
    await migrate(url, "upgrade", "0003")
    engine = create_async_engine(url)
    yield url, engine
    await engine.dispose()


async def row(engine: AsyncEngine, sql: str, **params: Any) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params)).one()


async def test_upgrade_backfills_legacy_rows(at_0003: tuple[str, AsyncEngine]) -> None:
    url, engine = at_0003
    ids = await seed_legacy(engine)
    await migrate(url, "upgrade", "0004")
    # 0004 is no longer head (0005 added the attempt failure_reason presence
    # rule); drift against head is test_migrations.test_migrations_match_models_exactly.
    assert await version_of(engine) == "0004"

    # New run columns take their defaults; stuck claims and runs get an expired-now lease.
    run = await row(
        engine,
        "SELECT lease_epoch, max_attempts, replay_count, unstarted_claims, dead_letter_reason, "
        "failure_reason, cancel_requested_at, next_attempt_at FROM runs WHERE id = :id",
        id=ids["cmd_run"],
    )
    assert tuple(run) == (0, 1, 0, 0, None, None, None, None)
    for key in ("claimed_run", "running_run"):
        assert await scalar(
            engine, "SELECT lease_expires_at <= now() FROM runs WHERE id = :id", id=ids[key]
        )
    assert (
        await scalar(engine, "SELECT lease_expires_at FROM runs WHERE id = :id", id=ids["cmd_run"])
        is None
    )

    # Failed FINISHED attempts are 'unclassified' and not retryable; others are untouched.
    failed = await row(
        engine,
        "SELECT failure_reason, retryable, workspace_capture, lease_epoch FROM run_attempts "
        "WHERE id = :id",
        id=ids["failed_attempt"],
    )
    assert tuple(failed) == ("unclassified", False, None, 0)
    for key in ("cmd_attempt", "agent_attempt", "running_attempt"):
        ok = await row(
            engine, "SELECT failure_reason, retryable FROM run_attempts WHERE id = :id", id=ids[key]
        )
        assert tuple(ok) == (None, None)

    # Evaluations: ERROR gets a reason and kind, RUNNING gets an expired legacy lease.
    err = await row(
        engine,
        "SELECT error_reason, error_kind, eval_attempts, max_eval_attempts "
        "FROM attempt_evaluations WHERE id = :id",
        id=ids["error_eval"],
    )
    assert tuple(err) == ("unclassified", "non_retryable", 0, 1)
    running = await row(
        engine,
        "SELECT worker_id, lease_expires_at <= now(), error_reason FROM attempt_evaluations "
        "WHERE id = :id",
        id=ids["running_eval"],
    )
    assert tuple(running) == ("legacy", True, None)
    pending = await row(
        engine,
        "SELECT worker_id, lease_expires_at, error_reason FROM attempt_evaluations WHERE id = :id",
        id=ids["pending_eval"],
    )
    assert tuple(pending) == (None, None, None)

    # Phase 4 snapshots stay readable in place.
    art = await row(
        engine,
        "SELECT content, storage_key, metadata FROM attempt_artifacts WHERE id = :id",
        id=ids["artifact"],
    )
    assert (bytes(art.content), art.storage_key, art.metadata) == (b"\x01\x02", None, {})

    # The functions exist and the new table is empty.
    assert (
        await scalar(
            engine, "SELECT count(*) FROM pg_proc WHERE proname IN ('p5_finish','p5_cancel')"
        )
        == 2
    )
    assert await scalar(engine, "SELECT count(*) FROM workers") == 0


async def test_clean_round_trip_with_legacy_data(at_0003: tuple[str, AsyncEngine]) -> None:
    url, engine = at_0003
    ids = await seed_legacy(engine)
    await migrate(url, "upgrade", "0004")
    await migrate(url, "downgrade", "0003")  # only backfilled values: allowed
    assert await version_of(engine) == "0003"
    for col in ("lease_epoch", "failure_reason", "error_reason", "storage_key"):
        assert (
            await scalar(
                engine,
                "SELECT count(*) FROM information_schema.columns WHERE column_name = :c",
                c=col,
            )
            == 0
        )
    assert await scalar(engine, "SELECT count(*) FROM pg_proc WHERE proname LIKE 'p5\\_%'") == 0
    assert (
        await scalar(engine, "SELECT status FROM runs WHERE id = :id", id=ids["claimed_run"])
        == "CLAIMED"
    )
    await migrate(url, "upgrade", "0004")
    assert await version_of(engine) == "0004"


# One case per kind of Phase 5 state the downgrade must not silently destroy.
WID = "w-1"
PHASE5_DATA: dict[str, list[str]] = {
    "runs in RETRYING or CANCELLED": [
        "UPDATE runs SET status = 'CANCELLED', outcome = NULL, cancel_requested_at = now(), "
        "cancel_reason = 'user' WHERE id = :cmd_run"
    ],
    "runs with Phase 5 budgets or history": [
        "UPDATE runs SET max_attempts = 3 WHERE id = :cmd_run"
    ],
    "attempts ABANDONED or CANCELLED": [
        "UPDATE run_attempts SET status = 'ABANDONED', outcome = 'INTERNAL_ERROR', "
        "failure_reason = 'lease_expired', retryable = true WHERE id = :running_attempt"
    ],
    "attempts with a recorded reason or capture": [
        "UPDATE run_attempts SET failure_reason = 'sandbox_lost', retryable = true "
        "WHERE id = :failed_attempt"
    ],
    "evaluations with grading history": [
        "UPDATE attempt_evaluations SET max_eval_attempts = 3 WHERE id = :pending_eval"
    ],
    "batches with Phase 5 settings": [
        "INSERT INTO datasets (id, name, version, status, frozen_at) "
        "VALUES ('22222222-2222-2222-2222-222222222222', 'd', '1', 'FROZEN', now())",
        "INSERT INTO evaluations (id, name, dataset_id, agent_version_id, repetitions, "
        "scoring_config, scoring_config_hash, run_max_attempts) "
        "VALUES (gen_random_uuid(), 'e', '22222222-2222-2222-2222-222222222222', :version, 1, "
        "'{}', repeat('d', 64), 3)",
    ],
    "file-backed artifacts": [
        "INSERT INTO attempt_artifacts (id, attempt_id, kind, storage_key, size_bytes, sha256) "
        "VALUES (gen_random_uuid(), :cmd_attempt, 'workspace', 'k', 1, repeat('b', 64))"
    ],
    "workers": [
        f"INSERT INTO workers (id, hostname, pid, version, concurrency) "
        f"VALUES ('{WID}', 'h', 1, 'v', 2)"
    ],
}


@pytest.mark.parametrize("kind", sorted(PHASE5_DATA))
async def test_downgrade_refuses_while_phase5_data_exists(
    at_0003: tuple[str, AsyncEngine], kind: str
) -> None:
    url, engine = at_0003
    ids = await seed_legacy(engine)
    await migrate(url, "upgrade", "0004")
    async with engine.begin() as conn:
        for sql in PHASE5_DATA[kind]:
            await conn.execute(text(sql), {k: v for k, v in ids.items() if f":{k}" in sql})

    with pytest.raises(RuntimeError, match="refusing to downgrade 0004") as info:
        await migrate(url, "downgrade", "0003")
    assert f"{kind}: " in str(info.value)
    # Rolled back as a whole: still at 0004, the Phase 5 columns and functions intact.
    assert await version_of(engine) == "0004"
    assert (
        await scalar(
            engine,
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'runs' AND column_name = 'lease_epoch'",
        )
        == 1
    )
    assert (
        await scalar(
            engine, "SELECT count(*) FROM pg_proc WHERE proname IN ('p5_finish','p5_cancel')"
        )
        == 2
    )
