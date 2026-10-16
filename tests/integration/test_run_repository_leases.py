"""Step 4: RunRepository.claim seeding the lease, start_attempt's cancel
precedence (R3/R5) and fencing, and fail_claimed clearing the lease so
ck_runs_lease holds (R6). No Docker, no FakeProvider: these exercise the
repository directly against the real migrated schema."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agenteval_core.models import Run, Task
from agenteval_core.repositories.runs import RunRepository, StartOutcome

SM = async_sessionmaker[AsyncSession]


async def _queued_run(sm: SM, *, max_attempts: int = 3) -> uuid.UUID:
    async with sm() as s:
        task = Task(
            name="cmd",
            task_type="command",
            image="agenteval-sandbox:py312",
            command=["true"],
            timeout_s=30,
            memory_mb=256,
            cpu_limit=1.0,
        )
        s.add(task)
        await s.flush()
        run = Run(task_id=task.id, status="QUEUED", attempt_count=0, max_attempts=max_attempts)
        s.add(run)
        await s.commit()
        return run.id


async def test_claim_seeds_the_lease(sessionmaker: SM) -> None:
    run_id = await _queued_run(sessionmaker)
    async with sessionmaker() as s:
        run = await RunRepository(s).claim(run_id, "w1", ttl_s=30.0)
        assert run is not None
        assert run.lease_epoch == 1
        assert run.lease_expires_at is not None
        assert run.heartbeat_at is not None
        now = datetime.now(UTC)
        assert now < run.lease_expires_at < now + timedelta(seconds=31)
        await s.commit()


async def test_start_attempt_runs_normally(sessionmaker: SM) -> None:
    run_id = await _queued_run(sessionmaker)
    async with sessionmaker() as s:
        run = await RunRepository(s).claim(run_id, "w1", ttl_s=30.0)
        assert run is not None
        await s.commit()
    async with sessionmaker() as s:
        outcome, attempt = await RunRepository(s).start_attempt(run_id, "w1")
        assert outcome is StartOutcome.STARTED
        assert attempt is not None
        assert (attempt.attempt_no, attempt.lease_epoch) == (1, 1)
        await s.commit()


async def test_start_attempt_with_a_pending_cancel_applies_r5(sessionmaker: SM) -> None:
    """R3 guarded by cancel_requested_at IS NULL; 0 rows + a pending cancel
    -> R5 (CLAIMED -> CANCELLED), with no attempt ever created."""
    run_id = await _queued_run(sessionmaker)
    async with sessionmaker() as s:
        run = await RunRepository(s).claim(run_id, "w1", ttl_s=30.0)
        assert run is not None
        await s.commit()
    async with sessionmaker() as s:
        await s.execute(
            text(
                "UPDATE runs SET cancel_requested_at = now(), cancel_reason = 'user' WHERE id = :id"
            ),
            {"id": run_id},
        )
        await s.commit()
    async with sessionmaker() as s:
        outcome, attempt = await RunRepository(s).start_attempt(run_id, "w1")
        assert (outcome, attempt) == (StartOutcome.CANCELLED, None)
        await s.commit()
    async with sessionmaker() as s:
        row = (
            await s.execute(
                text(
                    "SELECT status, lease_expires_at, lease_epoch, "
                    "(SELECT count(*) FROM run_attempts WHERE run_id = runs.id) AS attempts "
                    "FROM runs WHERE id = :id"
                ),
                {"id": run_id},
            )
        ).one()
        assert (row.status, row.lease_expires_at, row.attempts) == ("CANCELLED", None, 0)
        assert row.lease_epoch == 2  # bumped by R5


async def test_start_attempt_is_fenced_by_a_stale_epoch(sessionmaker: SM) -> None:
    """A second start_attempt call under the same (now stale) worker_id, after
    someone else's claim moved the epoch forward, must not resurrect it."""
    run_id = await _queued_run(sessionmaker)
    async with sessionmaker() as s:
        await RunRepository(s).claim(run_id, "w1", ttl_s=30.0)
        await s.commit()
    async with sessionmaker() as s:
        outcome, attempt = await RunRepository(s).start_attempt(run_id, "w1")
        assert outcome is StartOutcome.STARTED
        assert attempt is not None
        await s.commit()
    # A second start_attempt for the same (now RUNNING, not CLAIMED) run: 0
    # rows, no cancel pending -> fenced, no new attempt.
    async with sessionmaker() as s:
        outcome, attempt = await RunRepository(s).start_attempt(run_id, "w1")
        assert (outcome, attempt) == (StartOutcome.FENCED, None)
        await s.commit()


async def test_fail_claimed_clears_the_lease(sessionmaker: SM) -> None:
    """R6: the lease must be cleared so ck_runs_lease holds once the run is
    FAILED - not just the status."""
    run_id = await _queued_run(sessionmaker)
    async with sessionmaker() as s:
        run = await RunRepository(s).claim(run_id, "w1", ttl_s=30.0)
        assert run is not None and run.lease_expires_at is not None
        await s.commit()
    async with sessionmaker() as s:
        ok = await RunRepository(s).fail_claimed(run_id, "w1", "inconsistent at claim")
        assert ok
        await s.commit()
    async with sessionmaker() as s:
        row = (
            await s.execute(
                text("SELECT status, lease_expires_at FROM runs WHERE id = :id"), {"id": run_id}
            )
        ).one()
        assert (row.status, row.lease_expires_at) == ("FAILED", None)


async def test_fail_claimed_loses_to_a_pending_cancel(sessionmaker: SM) -> None:
    run_id = await _queued_run(sessionmaker)
    async with sessionmaker() as s:
        await RunRepository(s).claim(run_id, "w1", ttl_s=30.0)
        await s.commit()
    async with sessionmaker() as s:
        await s.execute(
            text(
                "UPDATE runs SET cancel_requested_at = now(), cancel_reason = 'user' WHERE id = :id"
            ),
            {"id": run_id},
        )
        await s.commit()
    async with sessionmaker() as s:
        ok = await RunRepository(s).fail_claimed(run_id, "w1", "should not apply")
        assert not ok  # the guard excludes a pending cancel; nothing was written
        await s.commit()
    async with sessionmaker() as s:
        status = (
            await s.execute(text("SELECT status FROM runs WHERE id = :id"), {"id": run_id})
        ).scalar_one()
        assert status == "CLAIMED"


@pytest.mark.parametrize("ttl_s", [1.0, 60.0])
async def test_claim_ttl_is_configurable(sessionmaker: SM, ttl_s: float) -> None:
    run_id = await _queued_run(sessionmaker)
    async with sessionmaker() as s:
        before = datetime.now(UTC)
        run = await RunRepository(s).claim(run_id, "w1", ttl_s=ttl_s)
        assert run is not None and run.lease_expires_at is not None
        assert before + timedelta(seconds=ttl_s - 1) < run.lease_expires_at
        assert run.lease_expires_at < before + timedelta(seconds=ttl_s + 5)
        await s.commit()
