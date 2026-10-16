"""The T-terminal functions from migration 0004, p5_finish and p5_cancel:
their transitions, fencing, and behaviour under real two-session concurrency.

Sessions are plain asyncpg connections so a test can hold a row lock in one
session while another session's call waits on it."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from agenteval_core.config import Settings
from tests.integration.phase5_seed import add_artifact, running_run

FINISH = "SELECT p5_finish($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb)"
CANCEL = "SELECT result, storage_keys FROM p5_cancel($1, $2, $3, $4::jsonb)"
# The reconciler's recovery selection for RUNNING runs (Step 5), used here only
# to prove the functions' locks make it skip instead of wait.
RECOVER_SCAN = """SELECT r.id FROM runs r JOIN run_attempts a ON a.run_id = r.id
    AND a.attempt_no = r.attempt_count AND a.status = 'RUNNING'
    WHERE r.id = $1 AND r.status = 'RUNNING' FOR UPDATE OF r, a SKIP LOCKED"""


@pytest.fixture
async def connect(settings: Settings) -> AsyncIterator[Any]:
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
    opened: list[asyncpg.Connection] = []

    async def open_() -> asyncpg.Connection:
        conn = await asyncpg.connect(dsn)
        opened.append(conn)
        return conn

    yield open_
    for conn in opened:
        if not conn.is_closed():
            await conn.close()


async def finish(
    conn: asyncpg.Connection,
    run: uuid.UUID,
    att: uuid.UUID,
    epoch: int = 1,
    *,
    target: str = "COMPLETED",
    outcome: str = "SUCCESS",
    reason: str | None = None,
    retryable: bool | None = None,
    capture: str | None = None,
    next_at: Any = None,
    dead_letter: str | None = None,
    error: str | None = None,
    attempt: dict[str, Any] | None = None,
) -> str:
    return str(
        await conn.fetchval(
            FINISH,
            run,
            att,
            epoch,
            target,
            outcome,
            reason,
            retryable,
            capture,
            next_at,
            dead_letter,
            error,
            json.dumps(attempt or {}),
        )
    )


async def cancel(
    conn: asyncpg.Connection,
    run: uuid.UUID,
    att: uuid.UUID,
    epoch: int = 1,
    attempt: dict[str, Any] | None = None,
) -> tuple[str, list[str]]:
    row = await conn.fetchrow(CANCEL, run, att, epoch, json.dumps(attempt or {}))
    return row["result"], list(row["storage_keys"])


async def flag(conn: asyncpg.Connection, run: uuid.UUID, reason: str = "user") -> str:
    """The cancel API's flag update: only CLAIMED/RUNNING runs, only once."""
    return str(
        await conn.execute(
            "UPDATE runs SET cancel_requested_at = now(), cancel_reason = $2 WHERE id = $1 "
            "AND status IN ('CLAIMED','RUNNING') AND cancel_requested_at IS NULL",
            run,
            reason,
        )
    )


async def state(conn: asyncpg.Connection, run: uuid.UUID, att: uuid.UUID) -> asyncpg.Record:
    return await conn.fetchrow(
        """SELECT r.status AS run_status, r.outcome AS run_outcome, r.error_message,
             r.next_attempt_at IS NOT NULL AS has_next, r.dead_letter_reason,
             r.finished_at IS NOT NULL AS run_finished, r.lease_expires_at IS NULL AS no_lease,
             a.status AS attempt_status, a.outcome AS attempt_outcome, a.failure_reason,
             a.retryable, a.workspace_capture, a.exit_code, a.stdout, a.steps, a.duration_ms,
             (SELECT count(*) FROM attempt_artifacts x WHERE x.attempt_id = a.id) AS artifacts
           FROM runs r JOIN run_attempts a ON a.run_id = r.id WHERE r.id = $1 AND a.id = $2""",
        run,
        att,
    )


async def wait_until_blocked(observer: asyncpg.Connection, pid: int) -> None:
    for _ in range(400):
        waiting = await observer.fetchval(
            "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = $1", pid
        )
        if waiting:
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"backend {pid} never blocked on a lock")


# -- transitions ---------------------------------------------------------------------


async def test_finish_completed(engine: AsyncEngine, connect: Any) -> None:
    run, att = await running_run(engine)
    s = await connect()
    result = await finish(
        s, run, att, attempt={"exit_code": 0, "stdout": "hi", "duration_ms": 12, "steps": None}
    )
    assert result == "finished"
    st = await state(s, run, att)
    assert (st["run_status"], st["run_outcome"], st["run_finished"], st["no_lease"]) == (
        "COMPLETED",
        "SUCCESS",
        True,
        True,
    )
    assert (st["attempt_status"], st["exit_code"], st["stdout"], st["duration_ms"]) == (
        "FINISHED",
        0,
        "hi",
        12,
    )
    assert st["failure_reason"] is None and st["retryable"] is None


async def test_finish_retrying_leaves_no_outcome_on_the_run(
    engine: AsyncEngine, connect: Any
) -> None:
    run, att = await running_run(engine)
    s = await connect()
    result = await finish(
        s,
        run,
        att,
        target="RETRYING",
        outcome="MODEL_ERROR",
        reason="model_server_error",
        retryable=True,
        next_at=await s.fetchval("SELECT now() + interval '4 s'"),
        error="upstream 500",
    )
    assert result == "finished"
    st = await state(s, run, att)
    assert (st["run_status"], st["run_outcome"], st["error_message"], st["has_next"]) == (
        "RETRYING",
        None,
        None,
        True,
    )
    assert not st["run_finished"]
    assert (st["attempt_outcome"], st["failure_reason"], st["retryable"]) == (
        "MODEL_ERROR",
        "model_server_error",
        True,
    )


async def test_finish_failed_records_the_dead_letter(engine: AsyncEngine, connect: Any) -> None:
    run, att = await running_run(engine)
    s = await connect()
    result = await finish(
        s,
        run,
        att,
        target="FAILED",
        outcome="MODEL_ERROR",
        reason="model_authentication",
        retryable=False,
        dead_letter="non_retryable",
        error="401",
    )
    assert result == "finished"
    st = await state(s, run, att)
    assert (st["run_status"], st["run_outcome"], st["dead_letter_reason"], st["error_message"]) == (
        "FAILED",
        "MODEL_ERROR",
        "non_retryable",
        "401",
    )


async def test_finish_rejects_bad_arguments_and_writes_nothing(
    engine: AsyncEngine, connect: Any
) -> None:
    run, att = await running_run(engine)
    s = await connect()
    with pytest.raises(asyncpg.InvalidParameterValueError, match="invalid target"):
        await finish(s, run, att, target="CANCELLED")
    with pytest.raises(asyncpg.InvalidParameterValueError, match="unknown attempt fields"):
        await finish(s, run, att, attempt={"status": "FINISHED"})
    # A RETRYING finish without a retry time violates ck_runs_retrying and rolls back whole.
    with pytest.raises(asyncpg.CheckViolationError, match="ck_runs_retrying"):
        await finish(
            s,
            run,
            att,
            target="RETRYING",
            outcome="MODEL_ERROR",
            reason="model_timeout",
            retryable=True,
        )
    st = await state(s, run, att)
    assert (st["run_status"], st["attempt_status"]) == ("RUNNING", "RUNNING")


async def test_cancel_deletes_artifact_rows_and_returns_file_keys(
    engine: AsyncEngine, connect: Any
) -> None:
    run, att = await running_run(engine, cancel="user")
    await add_artifact(engine, att, storage_key="a/workspace-x.tar.gz")
    s = await connect()
    result, keys = await cancel(s, run, att, attempt={"steps": 3, "duration_ms": 40})
    assert (result, keys) == ("cancelled", ["a/workspace-x.tar.gz"])
    st = await state(s, run, att)
    assert (st["run_status"], st["run_outcome"], st["run_finished"], st["no_lease"]) == (
        "CANCELLED",
        None,
        True,
        True,
    )
    assert (
        st["attempt_status"],
        st["attempt_outcome"],
        st["failure_reason"],
        st["retryable"],
        st["workspace_capture"],
        st["steps"],
        st["artifacts"],
    ) == ("CANCELLED", None, "cancelled_by_user", False, None, 3, 0)


async def test_cancel_of_a_bytea_snapshot_returns_no_keys(
    engine: AsyncEngine, connect: Any
) -> None:
    run, att = await running_run(engine, cancel="batch")
    await add_artifact(engine, att, storage_key=None)
    s = await connect()
    assert await cancel(s, run, att) == ("cancelled", [])
    st = await state(s, run, att)
    assert (st["failure_reason"], st["artifacts"]) == ("cancelled_by_batch", 0)


async def test_stale_epoch_and_finished_attempts_are_fenced(
    engine: AsyncEngine, connect: Any
) -> None:
    run, att = await running_run(engine, epoch=2)
    s = await connect()
    assert await finish(s, run, att, epoch=1) == "fenced"
    assert await cancel(s, run, att, epoch=1) == ("fenced", [])
    assert await finish(s, run, att, epoch=2) == "finished"
    assert await finish(s, run, att, epoch=2) == "fenced"  # already FINISHED
    st = await state(s, run, att)
    assert (st["run_status"], st["attempt_status"]) == ("COMPLETED", "FINISHED")


async def test_finish_with_a_pending_cancel_writes_nothing_and_releases_its_locks(
    engine: AsyncEngine, connect: Any
) -> None:
    run, att = await running_run(engine, cancel="user")
    s, other = await connect(), await connect()
    assert await finish(s, run, att) == "restart_cancel"
    st = await state(s, run, att)
    assert (st["run_status"], st["attempt_status"]) == ("RUNNING", "RUNNING")
    async with other.transaction():
        await other.execute("SET LOCAL lock_timeout = '200ms'")
        await other.execute("SELECT 1 FROM run_attempts WHERE id = $1 FOR UPDATE", att)
        await other.execute("SELECT 1 FROM runs WHERE id = $1 FOR UPDATE", run)
    assert (await cancel(s, run, att))[0] == "cancelled"


async def test_inconsistent_rows_raise_instead_of_guessing(
    engine: AsyncEngine, connect: Any
) -> None:
    run, att = await running_run(engine)
    s = await connect()
    # The run left RUNNING without its attempt (I1 broken by hand).
    await s.execute(
        "UPDATE runs SET status = 'FAILED', outcome = 'INTERNAL_ERROR', lease_expires_at = NULL "
        "WHERE id = $1",
        run,
    )
    with pytest.raises(asyncpg.RaiseError, match="does not match its RUNNING attempt"):
        await finish(s, run, att)
    run2, att2 = await running_run(engine)  # no cancel request
    with pytest.raises(asyncpg.RaiseError, match="not RUNNING with a cancel request"):
        await cancel(s, run2, att2)


# -- concurrency -----------------------------------------------------------------------


async def test_cancel_flag_committing_first_wins_over_finish(
    engine: AsyncEngine, connect: Any
) -> None:
    """The cancel API holds the run row (flag written, not yet committed);
    p5_finish takes the attempt row, waits for the run row, then sees the flag."""
    run, att = await running_run(engine)
    api, owner, observer = await connect(), await connect(), await connect()
    tx = api.transaction()
    await tx.start()
    assert await flag(api, run) == "UPDATE 1"
    pid = await owner.fetchval("SELECT pg_backend_pid()")
    call = asyncio.create_task(finish(owner, run, att))
    await wait_until_blocked(observer, pid)
    await tx.commit()
    assert await call == "restart_cancel"
    assert (await cancel(owner, run, att))[0] == "cancelled"
    st = await state(observer, run, att)
    assert (st["run_status"], st["attempt_status"], st["artifacts"]) == (
        "CANCELLED",
        "CANCELLED",
        0,
    )


async def test_finish_committing_first_makes_the_cancel_api_write_nothing(
    engine: AsyncEngine, connect: Any
) -> None:
    run, att = await running_run(engine)
    owner, api = await connect(), await connect()
    assert await finish(owner, run, att) == "finished"
    assert await flag(api, run) == "UPDATE 0"  # the API answers 409
    st = await api.fetchrow("SELECT status, cancel_requested_at FROM runs WHERE id = $1", run)
    assert (st["status"], st["cancel_requested_at"]) == ("COMPLETED", None)


async def test_recovery_skips_a_run_whose_finish_is_in_progress(
    engine: AsyncEngine, connect: Any
) -> None:
    run, att = await running_run(engine)
    holder, owner, reconciler, observer = (
        await connect(),
        await connect(),
        await connect(),
        await connect(),
    )
    tx = holder.transaction()
    await tx.start()
    await holder.execute("SELECT 1 FROM runs WHERE id = $1 FOR UPDATE", run)
    pid = await owner.fetchval("SELECT pg_backend_pid()")
    call = asyncio.create_task(finish(owner, run, att))
    await wait_until_blocked(observer, pid)  # holds the attempt row, waits for the run row
    async with reconciler.transaction():
        await reconciler.execute("SET LOCAL lock_timeout = '1s'")
        assert await reconciler.fetch(RECOVER_SCAN, run) == []  # skipped, did not wait
    await tx.rollback()
    assert await call == "finished"


async def test_recovery_committing_first_fences_both_functions(
    engine: AsyncEngine, connect: Any
) -> None:
    run, att = await running_run(engine, cancel="user")
    s = await connect()
    async with s.transaction():
        assert len(await s.fetch(RECOVER_SCAN, run)) == 1
        await s.execute(
            "UPDATE run_attempts SET status = 'ABANDONED', outcome = 'INTERNAL_ERROR', "
            "failure_reason = 'lease_expired', retryable = true, finished_at = now() "
            "WHERE id = $1",
            att,
        )
        await s.execute(
            "UPDATE runs SET status = 'CANCELLED', lease_epoch = lease_epoch + 1, "
            "lease_expires_at = NULL, finished_at = now() WHERE id = $1",
            run,
        )
    assert await finish(s, run, att) == "fenced"
    assert await cancel(s, run, att) == ("fenced", [])


async def test_heartbeat_is_not_blocked_while_a_cancel_waits_on_the_attempt_row(
    engine: AsyncEngine, connect: Any
) -> None:
    """A trajectory append holds the attempt row FOR SHARE (ADR-008 B); the
    cancel call waits on it before touching the run row, so heartbeats pass."""
    run, att = await running_run(engine, cancel="user")
    appender, owner, keeper, observer = (
        await connect(),
        await connect(),
        await connect(),
        await connect(),
    )
    tx = appender.transaction()
    await tx.start()
    await appender.execute("SELECT 1 FROM run_attempts WHERE id = $1 FOR SHARE", att)
    pid = await owner.fetchval("SELECT pg_backend_pid()")
    call = asyncio.create_task(cancel(owner, run, att))
    await wait_until_blocked(observer, pid)
    await keeper.execute("SET lock_timeout = '1s'")
    renewed = await keeper.execute(
        "UPDATE runs SET lease_expires_at = now() + interval '30 s', heartbeat_at = now() "
        "WHERE id = $1 AND status IN ('CLAIMED','RUNNING') AND lease_epoch = 1",
        run,
    )
    assert renewed == "UPDATE 1"
    await tx.commit()
    assert (await call)[0] == "cancelled"


async def test_two_finishes_for_one_attempt_commit_exactly_once(
    engine: AsyncEngine, connect: Any
) -> None:
    run, att = await running_run(engine)
    holder, a, b, observer = (await connect(), await connect(), await connect(), await connect())
    tx = holder.transaction()
    await tx.start()
    await holder.execute("SELECT 1 FROM run_attempts WHERE id = $1 FOR SHARE", att)
    pids = [await c.fetchval("SELECT pg_backend_pid()") for c in (a, b)]
    calls = [asyncio.create_task(finish(c, run, att)) for c in (a, b)]
    for pid in pids:
        await wait_until_blocked(observer, pid)
    await tx.commit()
    assert sorted(await asyncio.gather(*calls)) == ["fenced", "finished"]


async def test_a_call_whose_client_died_still_completes_atomically(
    engine: AsyncEngine, connect: Any
) -> None:
    run, att = await running_run(engine, cancel="user")
    await add_artifact(engine, att)
    holder, owner, observer = await connect(), await connect(), await connect()
    tx = holder.transaction()
    await tx.start()
    await holder.execute("SELECT 1 FROM run_attempts WHERE id = $1 FOR SHARE", att)
    pid = await owner.fetchval("SELECT pg_backend_pid()")
    call = asyncio.create_task(cancel(owner, run, att))
    await wait_until_blocked(observer, pid)
    owner.terminate()  # the worker process is gone mid-call
    with pytest.raises((asyncpg.ConnectionDoesNotExistError, asyncpg.InterfaceError)):
        await call
    await tx.commit()
    for _ in range(400):  # the server finishes the statement, then notices the client is gone
        if not await observer.fetchval("SELECT count(*) FROM pg_stat_activity WHERE pid = $1", pid):
            break
        await asyncio.sleep(0.01)
    st = await state(observer, run, att)
    assert (st["run_status"], st["attempt_status"], st["artifacts"]) == (
        "CANCELLED",
        "CANCELLED",
        0,
    )
