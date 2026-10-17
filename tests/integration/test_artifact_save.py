"""T-artifact (ADR-008 A/E, Step 6): the save protocol against real Postgres
and a real artifact directory - file first, then one fenced INSERT.

Every file case of the ADR's failure table: a fenced save removes its file, a
retry with the same bytes is success, different bytes under the same
(attempt, kind) are never stored (the original stays intact), a cancel unlinks
after commit, the writer's deadline and the statement timeout leave neither a
row nor a file, a stale epoch inserts nothing, and a save racing p5_cancel
never leaves a row on a CANCELLED attempt. No Docker."""

import asyncio
import hashlib
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agenteval_core.artifacts import ArtifactStore, LocalArtifactStore, StoredArtifact
from agenteval_core.config import Settings
from agenteval_core.models import RunAttempt
from agenteval_core.reliability.taxonomy import WorkspaceCapture
from agenteval_core.repositories.artifacts import ArtifactRepository, SaveResult
from agenteval_core.repositories.runs import FinishOutcome
from agenteval_core.states import Outcome
from agenteval_worker.agent_runner import AgentAttemptResult
from agenteval_worker.executor import Executor
from tests.integration.phase5_seed import running_run
from tests.integration.test_terminal_functions import cancel, flag, wait_until_blocked

SM = async_sessionmaker[AsyncSession]
DATA = b"snapshot bytes " * 50
SHA = hashlib.sha256(DATA).hexdigest()
META = {"normalized_paths": ["a.py"]}


@pytest.fixture
async def connect(settings: Settings) -> AsyncIterator[Callable[[], Awaitable[asyncpg.Connection]]]:
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


async def save(
    sm: SM,
    store: ArtifactStore,
    run: uuid.UUID,
    attempt: uuid.UUID,
    *,
    epoch: int = 1,
    data: bytes = DATA,
    insert_timeout_s: float = 10.0,
    deadline_s: float = 15.0,
    clock: Callable[[], float] = time.monotonic,
) -> SaveResult:
    """What the executor does: commit only a STORED save, otherwise roll back."""
    async with sm() as s:
        result = await ArtifactRepository(s, store).save(
            run_id=run,
            attempt_id=attempt,
            epoch=epoch,
            data=data,
            metadata=META,
            insert_timeout_s=insert_timeout_s,
            deadline_s=deadline_s,
            clock=clock,
        )
        if result is SaveResult.STORED:
            await s.commit()
        return result


async def rows(engine: AsyncEngine, attempt: uuid.UUID) -> list[Any]:
    async with engine.connect() as conn:
        return list(
            (
                await conn.execute(
                    text(
                        "SELECT storage_key, sha256, size_bytes, metadata,"
                        " content IS NULL AS no_content"
                        " FROM attempt_artifacts WHERE attempt_id = :a ORDER BY created_at"
                    ),
                    {"a": attempt},
                )
            ).all()
        )


async def files(store: LocalArtifactStore) -> list[str]:
    return sorted(k for k, _ in await store.list_files())


# -- the happy path and idempotence ------------------------------------------------------------


async def test_save_writes_the_file_then_the_row(
    engine: AsyncEngine, sessionmaker: SM, artifact_store: LocalArtifactStore
) -> None:
    run, att = await running_run(engine)
    assert await save(sessionmaker, artifact_store, run, att) is SaveResult.STORED

    [row] = await rows(engine, att)
    assert row.storage_key == f"{att}/workspace-{SHA}.tar.gz"
    assert (row.sha256, row.size_bytes, row.no_content) == (SHA, len(DATA), True)
    assert row.metadata == META  # the snapshot's normalized_paths travel with the row
    assert await artifact_store.read(row.storage_key, row.size_bytes, row.sha256) == DATA
    assert await files(artifact_store) == [row.storage_key]


async def test_a_retry_with_the_same_bytes_is_success_and_keeps_the_file(
    engine: AsyncEngine, sessionmaker: SM, artifact_store: LocalArtifactStore
) -> None:
    run, att = await running_run(engine)
    assert await save(sessionmaker, artifact_store, run, att) is SaveResult.STORED
    assert await save(sessionmaker, artifact_store, run, att) is SaveResult.STORED
    [row] = await rows(engine, att)
    assert await files(artifact_store) == [row.storage_key]
    assert await artifact_store.read(row.storage_key, row.size_bytes, row.sha256) == DATA


async def test_different_bytes_under_the_same_attempt_and_kind_are_rejected(
    engine: AsyncEngine, sessionmaker: SM, artifact_store: LocalArtifactStore
) -> None:
    """Never overwrite: the original row and file stay intact; the newcomer's
    file is removed."""
    run, att = await running_run(engine)
    await save(sessionmaker, artifact_store, run, att)
    [original] = await rows(engine, att)

    assert await save(sessionmaker, artifact_store, run, att, data=b"other") is SaveResult.FENCED

    assert [r.storage_key for r in await rows(engine, att)] == [original.storage_key]
    assert await files(artifact_store) == [original.storage_key]
    assert (
        await artifact_store.read(original.storage_key, original.size_bytes, original.sha256)
        == DATA
    )


# -- fencing -----------------------------------------------------------------------------------


async def test_a_stale_epoch_inserts_nothing_and_removes_its_file(
    engine: AsyncEngine, sessionmaker: SM, artifact_store: LocalArtifactStore
) -> None:
    """Recovery moved the attempt on: epoch 2 owns it, our save carries epoch 1."""
    run, att = await running_run(engine, epoch=2)
    assert await save(sessionmaker, artifact_store, run, att, epoch=1) is SaveResult.FENCED
    assert await rows(engine, att) == []
    assert await files(artifact_store) == []


@pytest.mark.parametrize("status", ["ABANDONED", "FINISHED"])
async def test_a_save_for_an_attempt_no_longer_running_is_fenced(
    engine: AsyncEngine,
    sessionmaker: SM,
    artifact_store: LocalArtifactStore,
    status: str,
) -> None:
    run, att = await running_run(engine)
    async with engine.begin() as conn:
        if status == "ABANDONED":
            await conn.execute(
                text(
                    "UPDATE run_attempts SET status = 'ABANDONED', outcome = 'INTERNAL_ERROR',"
                    " failure_reason = 'lease_expired', retryable = true, finished_at = now()"
                    " WHERE id = :a"
                ),
                {"a": att},
            )
        else:
            await conn.execute(
                text(
                    "UPDATE run_attempts SET status = 'FINISHED', outcome = 'SUCCESS',"
                    " finished_at = now() WHERE id = :a"
                ),
                {"a": att},
            )
    assert await save(sessionmaker, artifact_store, run, att) is SaveResult.FENCED
    assert await rows(engine, att) == []
    assert await files(artifact_store) == []


async def test_a_pending_cancel_blocks_the_insert_and_removes_the_file(
    engine: AsyncEngine, sessionmaker: SM, artifact_store: LocalArtifactStore
) -> None:
    run, att = await running_run(engine, cancel="user")
    assert await save(sessionmaker, artifact_store, run, att) is SaveResult.FENCED
    assert await rows(engine, att) == []
    assert await files(artifact_store) == []


# -- deadline and statement timeout ------------------------------------------------------------


async def test_a_save_past_its_deadline_inserts_nothing_and_removes_its_file(
    engine: AsyncEngine, sessionmaker: SM, artifact_store: LocalArtifactStore
) -> None:
    """E: more than SAVE_DEADLINE between the rename and the statement."""
    run, att = await running_run(engine)
    late = lambda: time.monotonic() + 16.0  # noqa: E731
    result = await save(sessionmaker, artifact_store, run, att, deadline_s=15.0, clock=late)
    assert result is SaveResult.FAILED
    assert await rows(engine, att) == []
    assert await files(artifact_store) == []


async def test_a_save_within_its_deadline_is_stored(
    engine: AsyncEngine, sessionmaker: SM, artifact_store: LocalArtifactStore
) -> None:
    run, att = await running_run(engine)
    early = lambda: time.monotonic() + 14.0  # noqa: E731
    assert (
        await save(sessionmaker, artifact_store, run, att, deadline_s=15.0, clock=early)
        is SaveResult.STORED
    )


async def test_the_statement_timeout_fails_the_save_with_no_row_and_no_file(
    engine: AsyncEngine,
    sessionmaker: SM,
    artifact_store: LocalArtifactStore,
    connect: Callable[[], Awaitable[asyncpg.Connection]],
) -> None:
    """Someone holds the attempt row (as a finish/cancel would): the save's
    FOR SHARE waits, the statement timeout cancels it, and the save is FAILED."""
    run, att = await running_run(engine)
    holder = await connect()
    tx = holder.transaction()
    await tx.start()
    await holder.fetchval("SELECT 1 FROM run_attempts WHERE id = $1 FOR NO KEY UPDATE", att)
    try:
        started = time.monotonic()
        result = await save(sessionmaker, artifact_store, run, att, insert_timeout_s=0.3)
        assert time.monotonic() - started < 5
    finally:
        await tx.rollback()
    assert result is SaveResult.FAILED
    assert await rows(engine, att) == []
    assert await files(artifact_store) == []


async def test_a_failed_file_write_is_a_failed_save(
    engine: AsyncEngine, sessionmaker: SM, artifact_store: LocalArtifactStore
) -> None:
    class DiskFull(LocalArtifactStore):
        async def put(self, attempt_id: uuid.UUID, kind: str, data: bytes) -> StoredArtifact:
            raise OSError("no space left on device")

    run, att = await running_run(engine)
    broken = DiskFull(artifact_store.root)
    assert await save(sessionmaker, broken, run, att) is SaveResult.FAILED
    assert await rows(engine, att) == []


# -- racing the terminal transitions -----------------------------------------------------------


async def test_a_save_blocked_behind_cancel_never_leaves_a_row_on_a_cancelled_attempt(
    engine: AsyncEngine,
    sessionmaker: SM,
    artifact_store: LocalArtifactStore,
    connect: Callable[[], Awaitable[asyncpg.Connection]],
) -> None:
    """p5_cancel holds the attempt row; the save's statement waits for it; when
    the cancel commits, the statement re-checks the latest row version (status
    is CANCELLED now), inserts nothing, and the save removes its file."""
    run, att = await running_run(engine)
    canceller, observer = await connect(), await connect()
    tx = canceller.transaction()
    await tx.start()
    await canceller.fetchval("SELECT 1 FROM run_attempts WHERE id = $1 FOR NO KEY UPDATE", att)
    saver = asyncio.create_task(save(sessionmaker, artifact_store, run, att))
    blocked_pid = None
    for _ in range(400):
        blocked_pid = await observer.fetchval(
            "SELECT pid FROM pg_stat_activity WHERE wait_event_type = 'Lock'"
            " AND query LIKE '%attempt_artifacts%' AND pid <> pg_backend_pid()"
        )
        if blocked_pid:
            break
        await asyncio.sleep(0.01)
    assert blocked_pid, "the save never blocked on the attempt row"
    await wait_until_blocked(observer, blocked_pid)

    assert await flag(canceller, run) == "UPDATE 1"
    assert (await cancel(canceller, run, att))[0] == "cancelled"
    await tx.commit()

    assert await asyncio.wait_for(saver, 10) is SaveResult.FENCED
    assert await rows(engine, att) == []
    assert await files(artifact_store) == []
    async with engine.connect() as conn:
        status = (
            await conn.execute(text("SELECT status FROM run_attempts WHERE id = :a"), {"a": att})
        ).scalar_one()
    assert status == "CANCELLED"


async def test_cancel_waits_for_a_save_holding_the_attempt_row_then_deletes_its_row(
    engine: AsyncEngine,
    sessionmaker: SM,
    artifact_store: LocalArtifactStore,
    connect: Callable[[], Awaitable[asyncpg.Connection]],
) -> None:
    """The save has inserted its row but not committed (it holds the attempt
    row FOR SHARE): p5_cancel waits, then deletes the row and reports its key
    so the caller can unlink the file after commit. A CANCELLED attempt never
    keeps a row."""
    run, att = await running_run(engine)
    observer, canceller = await connect(), await connect()
    async with sessionmaker() as s:
        result = await ArtifactRepository(s, artifact_store).save(
            run_id=run,
            attempt_id=att,
            epoch=1,
            data=DATA,
            metadata=META,
            insert_timeout_s=10,
            deadline_s=15,
        )
        assert result is SaveResult.STORED  # inserted, transaction still open
        await flag(observer, run)
        pid = await canceller.fetchval("SELECT pg_backend_pid()")
        cancelling = asyncio.create_task(cancel(canceller, run, att))
        await wait_until_blocked(observer, pid)
        await s.commit()
    outcome, keys = await asyncio.wait_for(cancelling, 10)

    assert outcome == "cancelled" and keys == [f"{att}/workspace-{SHA}.tar.gz"]
    assert await rows(engine, att) == []
    # Not unlinked by the function: that is the caller's job, after commit.
    assert await files(artifact_store) == keys


async def test_the_executor_unlinks_a_cancelled_attempts_files_only_after_commit(
    engine: AsyncEngine, sessionmaker: SM, artifact_store: LocalArtifactStore, queue: Any
) -> None:
    """finish_attempt hits restart_cancel -> p5_cancel; the executor deletes the
    returned keys after its commit. The store is wrapped to record what a
    separate connection sees at the moment of each delete."""
    run, att = await running_run(engine)
    assert await save(sessionmaker, artifact_store, run, att) is SaveResult.STORED
    [row] = await rows(engine, att)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE runs SET cancel_requested_at = now(), cancel_reason = 'user' WHERE id=:r"),
            {"r": run},
        )

    seen: list[tuple[str, int, str]] = []

    class Spy(LocalArtifactStore):
        async def delete(self, key: str) -> None:
            async with engine.connect() as conn:
                n = (
                    await conn.execute(
                        text("SELECT count(*) FROM attempt_artifacts WHERE attempt_id = :a"),
                        {"a": att},
                    )
                ).scalar_one()
                st = (
                    await conn.execute(
                        text("SELECT status FROM run_attempts WHERE id=:a"), {"a": att}
                    )
                ).scalar_one()
            seen.append((key, n, st))
            await super().delete(key)

    executor = Executor(
        sessionmaker,
        queue,
        None,
        "w1",
        artifact_store=Spy(artifact_store.root),  # type: ignore[arg-type]
    )
    async with sessionmaker() as s:
        attempt = await s.get(RunAttempt, att)
        assert attempt is not None
    result = AgentAttemptResult(
        outcome=Outcome.AGENT_FINISHED, duration_ms=1, workspace_capture=WorkspaceCapture.STORED
    )

    assert await executor._persist(attempt, result) is FinishOutcome.CANCELLED

    assert seen == [(row.storage_key, 0, "CANCELLED")]  # committed first, then unlinked
    assert await files(artifact_store) == []
    assert await rows(engine, att) == []


async def test_a_finish_that_is_not_cancelled_keeps_the_artifact_and_records_the_capture(
    engine: AsyncEngine, sessionmaker: SM, artifact_store: LocalArtifactStore, queue: Any
) -> None:
    run, att = await running_run(engine)
    await save(sessionmaker, artifact_store, run, att)
    executor = Executor(sessionmaker, queue, None, "w1", artifact_store=artifact_store)  # type: ignore[arg-type]
    async with sessionmaker() as s:
        attempt = await s.get(RunAttempt, att)
        assert attempt is not None
    result = AgentAttemptResult(
        outcome=Outcome.AGENT_FINISHED, duration_ms=1, workspace_capture=WorkspaceCapture.STORED
    )
    assert await executor._persist(attempt, result) is FinishOutcome.FINISHED
    async with engine.connect() as conn:
        capture = (
            await conn.execute(
                text("SELECT workspace_capture FROM run_attempts WHERE id = :a"), {"a": att}
            )
        ).scalar_one()
    assert capture == "stored"
    assert len(await rows(engine, att)) == 1 and len(await files(artifact_store)) == 1


# -- the executor's save step ------------------------------------------------------------------


async def test_save_workspace_turns_a_snapshot_into_a_capture_value(
    engine: AsyncEngine, sessionmaker: SM, artifact_store: LocalArtifactStore, queue: Any
) -> None:
    from agenteval_worker.sandbox.evaluation import WorkspaceSnapshot

    run, att = await running_run(engine)
    executor = Executor(sessionmaker, queue, None, "w1", artifact_store=artifact_store)  # type: ignore[arg-type]
    async with sessionmaker() as s:
        attempt = await s.get(RunAttempt, att)
        assert attempt is not None
    snap = WorkspaceSnapshot(DATA, SHA, ("a.py",))
    pending = AgentAttemptResult(outcome=Outcome.AGENT_FINISHED, duration_ms=1, snapshot=snap)

    saved = await executor._save_workspace(attempt, pending)

    assert isinstance(saved, AgentAttemptResult)
    assert saved.workspace_capture is WorkspaceCapture.STORED and saved.snapshot is None
    [row] = await rows(engine, att)
    assert row.metadata == {"normalized_paths": ["a.py"]}

    # A stale attempt (fenced) or a missing store both become persist_failed.
    stale = attempt  # still carries epoch 1; the row's epoch is bumped below
    await artifact_store.delete(row.storage_key)
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM attempt_artifacts"))
        await conn.execute(
            text("UPDATE run_attempts SET lease_epoch = 9 WHERE id = :a"), {"a": att}
        )
    fenced = await executor._save_workspace(stale, pending)
    assert isinstance(fenced, AgentAttemptResult)
    assert fenced.workspace_capture is WorkspaceCapture.PERSIST_FAILED
    assert await files(artifact_store) == []  # the fenced save removed its file

    no_store = Executor(sessionmaker, queue, None, "w2")  # type: ignore[arg-type]
    gone = await no_store._save_workspace(attempt, pending)
    assert isinstance(gone, AgentAttemptResult)
    assert gone.workspace_capture is WorkspaceCapture.PERSIST_FAILED


async def test_results_without_a_snapshot_pass_through_untouched(
    sessionmaker: SM, artifact_store: LocalArtifactStore, queue: Any
) -> None:
    executor = Executor(sessionmaker, queue, None, "w1", artifact_store=artifact_store)  # type: ignore[arg-type]
    done = AgentAttemptResult(
        outcome=Outcome.AGENT_FINISHED, duration_ms=1, workspace_capture=WorkspaceCapture.NOT_NEEDED
    )
    assert await executor._save_workspace(None, done) is done  # type: ignore[arg-type]
