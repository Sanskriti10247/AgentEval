"""TrajectoryRepository.append_fenced (ADR-008 B): locks the attempt row
`FOR SHARE` only (no `runs` join or lock any more). Appends succeed while the
attempt is owned, are fenced once its epoch moves (recovery, or a second
claim), and the fence does not deadlock against p5_cancel's attempt-then-run
lock order (a focused two-session check; the fuller races live in
test_terminal_functions.py, which already exercises concurrent `FOR SHARE`
holders against p5_finish/p5_cancel)."""

import asyncio

import asyncpg
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agenteval_core.agent.events import EventType, TrajectoryEventData
from agenteval_core.config import Settings
from agenteval_core.models import RunAttempt, TrajectoryEvent
from agenteval_core.repositories.trajectory import TrajectoryRepository
from tests.integration.phase5_seed import running_run

SM = async_sessionmaker[AsyncSession]


def event(seq: int) -> TrajectoryEventData:
    return TrajectoryEventData(seq=seq, step=seq, event_type=EventType.MODEL_CALL)


async def test_append_succeeds_while_the_attempt_is_owned(
    engine: AsyncEngine, sessionmaker: SM
) -> None:
    _, attempt_id = await running_run(engine, epoch=1)
    async with sessionmaker() as s:
        attempt = await s.get(RunAttempt, attempt_id)
        assert attempt is not None
        ok = await TrajectoryRepository(s).append_fenced(attempt, [event(0), event(1)])
        assert ok
        await s.commit()
    async with sessionmaker() as s:
        rows = (
            await s.execute(
                select(TrajectoryEvent.seq)
                .where(TrajectoryEvent.attempt_id == attempt_id)
                .order_by(TrajectoryEvent.seq)
            )
        ).scalars()
        assert list(rows) == [0, 1]


async def test_append_is_fenced_once_the_epoch_moves(engine: AsyncEngine, sessionmaker: SM) -> None:
    """Simulates recovery bumping the epoch (A3 + R9/R10): the attempt object
    held by the (now stale) owner still carries the old epoch."""
    _, attempt_id = await running_run(engine, epoch=1)
    async with sessionmaker() as s:
        stale_attempt = await s.get(RunAttempt, attempt_id)
        assert stale_attempt is not None

    async with sessionmaker() as s:
        attempt = await s.get(RunAttempt, attempt_id)
        assert attempt is not None
        attempt.lease_epoch = 2  # recovery would have bumped this in the DB
        await s.commit()

    async with sessionmaker() as s:
        ok = await TrajectoryRepository(s).append_fenced(stale_attempt, [event(0)])
        assert not ok
        await s.rollback()
    async with sessionmaker() as s:
        count = (
            await s.execute(select(TrajectoryEvent).where(TrajectoryEvent.attempt_id == attempt_id))
        ).scalars()
        assert list(count) == []


async def test_append_is_fenced_once_the_attempt_is_no_longer_running(
    engine: AsyncEngine, sessionmaker: SM
) -> None:
    """The guard only reads `attempt.id` and `attempt.lease_epoch` from the
    object the caller passes in - it checks the *database's* current status,
    so a stale in-memory copy that still says RUNNING changes nothing."""
    _, attempt_id = await running_run(engine, epoch=1)
    async with sessionmaker() as s:
        attempt = await s.get(RunAttempt, attempt_id)
        assert attempt is not None  # status='RUNNING' in memory, epoch=1
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE run_attempts SET status = 'ABANDONED', outcome = 'INTERNAL_ERROR', "
                "failure_reason = 'lease_expired', retryable = true, finished_at = now() "
                "WHERE id = :a"
            ),
            {"a": attempt_id},
        )
    async with sessionmaker() as s:
        ok = await TrajectoryRepository(s).append_fenced(attempt, [event(0)])
        assert not ok


async def test_fence_does_not_deadlock_against_p5_cancel(
    engine: AsyncEngine, settings: Settings
) -> None:
    """A trajectory append holds the attempt row FOR SHARE; p5_cancel takes
    the same row FOR NO KEY UPDATE first, then the run row. Both lock the
    attempt row in the same order, so the append only ever waits - it never
    forms a cycle with p5_cancel's own lock order."""
    run_id, attempt_id = await running_run(engine, cancel="user")
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
    appender, canceller = await asyncpg.connect(dsn), await asyncpg.connect(dsn)
    try:
        tx = appender.transaction()
        await tx.start()
        await appender.execute("SELECT 1 FROM run_attempts WHERE id = $1 FOR SHARE", attempt_id)
        call = asyncio.create_task(
            canceller.fetchrow(
                "SELECT result, storage_keys FROM p5_cancel($1, $2, $3, '{}'::jsonb)",
                run_id,
                attempt_id,
                1,
            )
        )
        await asyncio.sleep(0.2)
        assert not call.done()  # waiting on the share lock, not deadlocked
        await tx.commit()
        row = await asyncio.wait_for(call, 5)
        assert row["result"] == "cancelled"
    finally:
        await appender.close()
        await canceller.close()


@pytest.mark.parametrize("cancel_requested", [False])
async def test_append_ignores_runs_entirely(
    engine: AsyncEngine, sessionmaker: SM, cancel_requested: bool
) -> None:
    """ADR-008 B: the fence never locks or reads `runs`. A run row that
    disagrees with its attempt (deliberately broken here) still lets the
    append through, because the guard only looks at run_attempts."""
    _, attempt_id = await running_run(engine, epoch=1)
    async with sessionmaker() as s:
        attempt = await s.get(RunAttempt, attempt_id)
        assert attempt is not None
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE runs SET lease_epoch = lease_epoch + 5 WHERE id = :r"),
            {"r": attempt.run_id},
        )
    async with sessionmaker() as s:
        ok = await TrajectoryRepository(s).append_fenced(attempt, [event(0)])
        assert ok  # unaffected by the run row's epoch
        await s.commit()
