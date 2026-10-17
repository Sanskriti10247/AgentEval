"""Phase 5 Step 8: the cancellation API (PROGRESS.md section 20).

DB-level cases use hand-seeded rows (phase5_seed, test_reconciler's seeds) so
every transition and response code is hit exactly; the worker cases drive a
real executor, lease keeper and Docker sandbox with FakeProvider. No network.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import asyncpg
import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agenteval_core.config import Settings
from agenteval_core.queue.streams import RunQueue
from agenteval_core.repositories.runs import RunRepository, StartOutcome
from agenteval_core.services import evaluations as evaluation_service
from agenteval_worker.executor import Executor
from agenteval_worker.lease_manager import LeaseManager
from tests.integration.conftest import create_task
from tests.integration.eval_seed import agent_version, create_evaluation, frozen_dataset, seed
from tests.integration.phase5_seed import running_run
from tests.integration.test_agent_executor import (
    leftover_containers,
    load,
    make_agent_run,
    turn,
)
from tests.integration.test_leases import FAST, handle_next, lease_executor, wait_until
from tests.integration.test_reconciler import (  # noqa: F401  (fixture)
    Harness,
    attempt_row,
    claimed_run,
    expire,
    make_reconciler,
    retrying_run,
    run_row,
)
from tests.integration.test_terminal_functions import cancel as p5_cancel
from tests.integration.test_terminal_functions import finish as p5_finish

SM = async_sessionmaker[AsyncSession]
API = "/api/v1"

# Internals that must never appear in a run or batch response (F15).
HIDDEN = ("lease_epoch", "lease_expires_at", "heartbeat_at", "workspace_capture", "storage_key")


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


@pytest.fixture
async def lease_manager(settings: Settings) -> AsyncIterator[LeaseManager]:
    lm = LeaseManager(settings.database_url, FAST, f"w-{uuid.uuid4().hex[:8]}")
    await lm.start()
    yield lm
    await lm.stop()


async def cancel_run(client: httpx.AsyncClient, run_id: uuid.UUID | str) -> httpx.Response:
    return await client.post(f"{API}/runs/{run_id}/cancel")


async def terminal_row(engine: AsyncEngine, status: str, outcome: str) -> uuid.UUID:
    """A run that already finished (COMPLETED or FAILED), as the worker leaves it."""
    run_id, _ = await running_run(engine)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """UPDATE runs SET status = :s, outcome = :o, finished_at = now(),
                     lease_expires_at = NULL, heartbeat_at = NULL WHERE id = :i"""
            ),
            {"s": status, "o": outcome, "i": run_id},
        )
    return run_id


async def wait_for_lock_waiter(observer: asyncpg.Connection, like: str) -> None:
    """Until some backend is blocked on a lock while running a statement like `like`."""
    for _ in range(600):
        n = await observer.fetchval(
            "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
            "AND query ILIKE $1",
            like,
        )
        if n:
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"nothing ever blocked on {like!r}")


# -- the transitions and response codes -----------------------------------------------------------


async def test_r2_a_queued_run_is_cancelled_at_once(
    client: httpx.AsyncClient, engine: AsyncEngine, sessionmaker: SM, queue: RunQueue
) -> None:
    task_id = await create_task(client)
    submitted = await client.post(f"{API}/runs", json={"task_id": task_id})
    run_id = submitted.json()["id"]

    resp = await cancel_run(client, run_id)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert (body["status"], body["outcome"], body["cancel_reason"]) == ("CANCELLED", None, "user")
    assert body["cancel_requested_at"] is not None and body["finished_at"] is not None
    assert not any(key in body for key in HIDDEN)

    row = await run_row(engine, uuid.UUID(run_id))
    assert (row.next_attempt_at, row.lease_expires_at, row.attempt_count) == (None, None, 0)
    # The message still on the stream is now stale: the claim finds nothing to claim.
    async with sessionmaker() as s:
        assert await RunRepository(s).claim(uuid.UUID(run_id), "w1") is None


async def test_r13_a_retrying_run_is_cancelled_at_once(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    run_id = await retrying_run(engine, due=False)
    resp = await cancel_run(client, run_id)
    assert resp.status_code == 202, resp.text
    assert (resp.json()["status"], resp.json()["cancel_reason"]) == ("CANCELLED", "user")
    row = await run_row(engine, run_id)
    assert (row.status, row.next_attempt_at, row.cancel_reason) == ("CANCELLED", None, "user")
    assert row.finished_at is not None and row.attempt_count == 1


@pytest.mark.parametrize("status", ["CLAIMED", "RUNNING"])
async def test_a_claimed_or_running_run_is_only_flagged(
    client: httpx.AsyncClient, engine: AsyncEngine, status: str
) -> None:
    if status == "RUNNING":
        run_id, _ = await running_run(engine)
    else:
        run_id = await claimed_run(engine, expired=False)
    before = await run_row(engine, run_id)

    resp = await cancel_run(client, run_id)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert (body["status"], body["cancel_reason"], body["finished_at"]) == (status, "user", None)
    assert body["cancel_requested_at"] is not None
    assert not any(key in body for key in HIDDEN)

    row = await run_row(engine, run_id)
    # Only the flag changed: the lease and the epoch still belong to the owner.
    assert (row.status, row.lease_epoch, row.lease_expires_at, row.worker_id) == (
        status,
        before.lease_epoch,
        before.lease_expires_at,
        before.worker_id,
    )


async def test_cancel_is_idempotent(client: httpx.AsyncClient, engine: AsyncEngine) -> None:
    running, _ = await running_run(engine)
    queued = await retrying_run(engine, due=False)
    for run_id in (running, queued):
        first = await cancel_run(client, run_id)
        assert first.status_code == 202
        stamped = (await run_row(engine, run_id)).cancel_requested_at
        again = await cancel_run(client, run_id)
        assert again.status_code == 200, again.text
        assert again.json() == first.json()
        row = await run_row(engine, run_id)
        assert row.cancel_requested_at == stamped  # nothing was written


async def test_a_batch_reason_is_not_overwritten_by_a_later_user_cancel(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    run_id, _ = await running_run(engine, cancel="batch")
    resp = await cancel_run(client, run_id)
    assert resp.status_code == 200
    assert resp.json()["cancel_reason"] == "batch"


@pytest.mark.parametrize(
    ("status", "outcome"), [("COMPLETED", "SUCCESS"), ("FAILED", "SANDBOX_ERROR")]
)
async def test_a_finished_run_answers_409_and_nothing_is_written(
    client: httpx.AsyncClient, engine: AsyncEngine, status: str, outcome: str
) -> None:
    run_id = await terminal_row(engine, status, outcome)
    before = await run_row(engine, run_id)
    resp = await cancel_run(client, run_id)
    assert resp.status_code == 409, resp.text
    assert resp.json()["title"] == "Conflict"
    assert await run_row(engine, run_id) == before


async def test_unknown_ids_are_404(client: httpx.AsyncClient) -> None:
    assert (await cancel_run(client, uuid.uuid4())).status_code == 404
    assert (await client.post(f"{API}/evaluations/{uuid.uuid4()}/cancel")).status_code == 404
    assert (await client.post(f"{API}/runs/not-a-uuid/cancel")).status_code == 422


# -- G7: cancel versus completion, both orders, two sessions ---------------------------------------


async def test_g7_finish_commits_first_so_the_cancel_is_a_409(
    client: httpx.AsyncClient, engine: AsyncEngine, connect: Any
) -> None:
    run, att = await running_run(engine)
    finisher, observer = await connect(), await connect()
    tx = finisher.transaction()
    await tx.start()
    assert await p5_finish(finisher, run, att) == "finished"  # holds the run row, uncommitted

    cancelling = asyncio.create_task(cancel_run(client, run))
    await wait_for_lock_waiter(observer, "UPDATE runs%")  # the cancel is waiting behind it
    await tx.commit()
    resp = await asyncio.wait_for(cancelling, 10)

    assert resp.status_code == 409, resp.text
    row = await run_row(engine, run)
    assert (row.status, row.outcome, row.cancel_requested_at, row.cancel_reason) == (
        "COMPLETED",
        "SUCCESS",
        None,
        None,
    )


async def test_g7_cancel_commits_first_so_the_finish_restarts_as_a_cancel(
    client: httpx.AsyncClient, engine: AsyncEngine, connect: Any
) -> None:
    run, att = await running_run(engine)
    assert (await cancel_run(client, run)).status_code == 202

    conn = await connect()
    assert await p5_finish(conn, run, att) == "restart_cancel"  # the flag beat the finish
    assert await p5_cancel(conn, run, att) == ("cancelled", [])
    row = await run_row(engine, run)
    assert (row.status, row.outcome, row.cancel_reason) == ("CANCELLED", None, "user")
    assert (await attempt_row(engine, att)).status == "CANCELLED"


async def test_g7_a_finish_blocked_behind_an_inflight_cancel_loses_to_it(
    client: httpx.AsyncClient, engine: AsyncEngine, connect: Any
) -> None:
    """The cancel statement is in flight (flag set, uncommitted); the finish
    arrives and waits for the run row, then must see the flag."""
    run, att = await running_run(engine)
    canceller, finisher, observer = await connect(), await connect(), await connect()
    tx = canceller.transaction()
    await tx.start()
    await canceller.execute(
        "UPDATE runs SET cancel_requested_at = now(), cancel_reason = 'user' WHERE id = $1", run
    )
    finishing = asyncio.create_task(p5_finish(finisher, run, att))
    await wait_for_lock_waiter(observer, "%p5_finish%")
    await tx.commit()
    assert await asyncio.wait_for(finishing, 10) == "restart_cancel"


async def test_g7_a_real_race_has_exactly_one_winner_and_the_loser_writes_nothing(
    client: httpx.AsyncClient, engine: AsyncEngine, connect: Any
) -> None:
    conn = await connect()
    outcomes = set()
    for _ in range(12):
        run, att = await running_run(engine)
        resp, result = await asyncio.gather(cancel_run(client, run), p5_finish(conn, run, att))
        row = await run_row(engine, run)
        if resp.status_code == 202:  # cancel won: the finish was turned into a restart
            assert result == "restart_cancel", result
            assert (row.status, row.outcome, row.cancel_reason) == ("RUNNING", None, "user")
            outcomes.add("cancel")
        else:  # finish won: the cancel wrote nothing
            assert (resp.status_code, result) == (409, "finished"), (resp.text, result)
            assert (row.status, row.cancel_requested_at, row.cancel_reason) == (
                "COMPLETED",
                None,
                None,
            )
            outcomes.add("finish")
    assert outcomes  # either winner is fine; neither ever produced a mixed state


async def test_cancel_stays_fast_while_an_artifact_save_holds_the_attempt_row(
    client: httpx.AsyncClient, engine: AsyncEngine, connect: Any
) -> None:
    """ADR-008: the cancel API only touches `runs`, so a long transaction that
    holds the attempt row (an artifact INSERT takes it FOR SHARE) cannot delay it."""
    run, att = await running_run(engine)
    holder = await connect()
    tx = holder.transaction()
    await tx.start()
    await holder.fetchval("SELECT 1 FROM run_attempts WHERE id = $1 FOR SHARE", att)
    try:
        t0 = asyncio.get_running_loop().time()
        resp = await asyncio.wait_for(cancel_run(client, run), 5)
        elapsed = asyncio.get_running_loop().time() - t0
    finally:
        await tx.rollback()
    assert resp.status_code == 202, resp.text
    assert elapsed < 2.0, elapsed


# -- the owner and the reconciler finish what the flag started ------------------------------------


async def test_a_cancel_pending_at_claim_ends_at_r3_as_r5(
    client: httpx.AsyncClient, engine: AsyncEngine, sessionmaker: SM
) -> None:
    task_id = await create_task(client)
    run_id = uuid.UUID((await client.post(f"{API}/runs", json={"task_id": task_id})).json()["id"])
    async with sessionmaker() as s:
        assert await RunRepository(s).claim(run_id, "w1") is not None
        await s.commit()
    assert (await cancel_run(client, run_id)).status_code == 202  # CLAIMED: flag only

    async with sessionmaker() as s:
        outcome, attempt = await RunRepository(s).start_attempt(run_id, "w1")
        await s.commit()
    assert (outcome, attempt) == (StartOutcome.CANCELLED, None)
    row = await run_row(engine, run_id)
    assert (row.status, row.cancel_reason, row.attempt_count) == ("CANCELLED", "user", 0)


async def test_a_cancel_plus_an_expired_running_lease_is_cancelled_by_the_reconciler_r11(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    make_reconciler: Callable[..., Harness],  # noqa: F811
) -> None:
    run_id, attempt_id = await running_run(engine)
    assert (await cancel_run(client, run_id)).status_code == 202
    await expire(engine, run_id)  # the owner died before it noticed

    h = make_reconciler()
    await h.rec.step_recover_running()
    row = await run_row(engine, run_id)
    assert (row.status, row.outcome, row.cancel_reason) == ("CANCELLED", None, "user")
    assert row.finished_at is not None
    attempt = await attempt_row(engine, attempt_id)
    assert (attempt.status, attempt.failure_reason) == ("ABANDONED", "lease_expired")
    assert h.kills == [str(attempt_id)]


async def test_a_cancel_plus_an_expired_claim_is_cancelled_by_the_reconciler_r5(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    make_reconciler: Callable[..., Harness],  # noqa: F811
) -> None:
    run_id = await claimed_run(engine, expired=False)
    assert (await cancel_run(client, run_id)).status_code == 202
    await expire(engine, run_id)
    await make_reconciler().rec.step_recover_claimed()
    row = await run_row(engine, run_id)
    assert (row.status, row.cancel_reason, row.unstarted_claims) == ("CANCELLED", "user", 0)


# -- real workers ---------------------------------------------------------------------------------


async def artifact_and_evaluation_counts(
    engine: AsyncEngine, attempt_id: uuid.UUID
) -> tuple[int, int]:
    async with engine.connect() as conn:
        artifacts = await conn.execute(
            text("SELECT count(*) FROM attempt_artifacts WHERE attempt_id = :a"), {"a": attempt_id}
        )
        evaluations = await conn.execute(
            text("SELECT count(*) FROM attempt_evaluations WHERE attempt_id = :a"),
            {"a": attempt_id},
        )
        return int(artifacts.scalar_one()), int(evaluations.scalar_one())


@pytest.mark.docker
async def test_cancelling_a_running_agent_run_ends_cancelled_with_no_artifact_or_evaluation(
    client: httpx.AsyncClient,
    sessionmaker: SM,
    engine: AsyncEngine,
    queue: RunQueue,
    lease_manager: LeaseManager,
) -> None:
    run_id = await make_agent_run(
        sessionmaker, queue, [turn("slow", delay_s=FAST.ttl_s * 2), turn("done")]
    )
    handle = asyncio.create_task(
        handle_next(lease_executor(sessionmaker, queue, lease_manager), queue)
    )

    async def is_running() -> bool:
        async with sessionmaker() as s:
            run = await RunRepository(s).get(run_id)
            return run is not None and run.status == "RUNNING"

    await wait_until(is_running, timeout_s=10)
    resp = await cancel_run(client, run_id)
    assert resp.status_code == 202 and resp.json()["status"] == "RUNNING"
    await asyncio.wait_for(handle, 15)

    run, [attempt], _ = await load(sessionmaker, run_id)
    assert (run.status, run.outcome, run.cancel_reason) == ("CANCELLED", None, "user")
    assert (attempt.status, attempt.failure_reason) == ("CANCELLED", "cancelled_by_user")
    assert await artifact_and_evaluation_counts(engine, attempt.id) == (0, 0)
    assert leftover_containers(run_id) == []
    final = (await client.get(f"{API}/runs/{run_id}")).json()
    assert (final["status"], final["cancel_reason"]) == ("CANCELLED", "user")


@pytest.mark.docker
async def test_cancelling_a_running_command_task_kills_its_container(
    client: httpx.AsyncClient,
    sessionmaker: SM,
    engine: AsyncEngine,
    queue: RunQueue,
    lease_manager: LeaseManager,
) -> None:
    task_id = await create_task(
        client,
        command=["python", "main.py"],
        files={"main.py": "import time\ntime.sleep(120)\n"},
        timeout_s=120,
    )
    run_id = uuid.UUID((await client.post(f"{API}/runs", json={"task_id": task_id})).json()["id"])
    handle = asyncio.create_task(
        handle_next(lease_executor(sessionmaker, queue, lease_manager), queue)
    )

    async def container_is_up() -> bool:
        return any(c.status == "running" for c in leftover_containers(run_id))

    await wait_until(container_is_up, timeout_s=30)
    resp = await cancel_run(client, run_id)
    assert resp.status_code == 202, resp.text
    # Bounded by one heartbeat (FAST: 0.5 s) plus the kill, far below the 120 s sleep.
    await asyncio.wait_for(handle, 15)

    run, [attempt], _ = await load(sessionmaker, run_id)
    assert (run.status, run.outcome, run.cancel_reason) == ("CANCELLED", None, "user")
    assert (attempt.status, attempt.failure_reason) == ("CANCELLED", "cancelled_by_user")
    assert await artifact_and_evaluation_counts(engine, attempt.id) == (0, 0)

    # The cancel kill and the sandbox's own cleanup both remove the container;
    # Docker answers the loser "removal already in progress", so it can still be
    # listed for a moment. It must go - promptly - not vanish within the same tick.
    async def containers_gone() -> bool:
        return not leftover_containers(run_id)

    await wait_until(containers_gone, timeout_s=30, interval=0.2)


async def test_the_executor_kills_the_container_of_a_command_task_on_a_cancel_beat(
    sessionmaker: SM, queue: RunQueue
) -> None:
    """Unit seam: the cancel callback kills by attempt label for a command task
    and only cancels the token for an agent task; a released slot does nothing."""
    from agenteval_core.agent.context import CancelToken
    from agenteval_worker.executor import _LeaseSlot

    killed: list[str] = []
    ex = Executor(
        sessionmaker,
        queue,
        lambda spec: None,  # type: ignore[arg-type, return-value]  # never run here
        "w-unit",
        kill_by_attempt_label=lambda attempt_id: killed.append(attempt_id) or 1,
    )

    command = _LeaseSlot(CancelToken())
    command.attempt_id, command.kill_on_cancel = uuid.uuid4(), True
    ex._on_cancel_requested(command, "user")
    await asyncio.sleep(0.05)
    assert killed == [str(command.attempt_id)] and command.token.reason == "user"

    agent = _LeaseSlot(CancelToken())
    agent.attempt_id = uuid.uuid4()
    ex._on_cancel_requested(agent, "batch")
    released = _LeaseSlot(CancelToken())
    released.attempt_id, released.kill_on_cancel, released.released = uuid.uuid4(), True, True
    ex._on_cancel_requested(released, "user")
    await asyncio.sleep(0.05)
    assert killed == [str(command.attempt_id)]  # no second kill
    assert agent.token.reason == "batch" and released.token.reason is None


# -- the batch ------------------------------------------------------------------------------------


async def batch_run_ids(engine: AsyncEngine, evaluation_id: str) -> list[uuid.UUID]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT id FROM runs WHERE evaluation_id = :e ORDER BY task_id, repetition"),
            {"e": uuid.UUID(evaluation_id)},
        )
        return [r.id for r in rows]


async def test_a_batch_cancel_cancels_every_unfinished_run_and_the_batch_completes(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    sessionmaker: SM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(evaluation_service, "CANCEL_CHUNK", 2)  # several chunks
    ds, _tasks = await frozen_dataset(client)
    version = await agent_version(sessionmaker)
    resp = await create_evaluation(client, ds, version, repetitions=3)
    evaluation_id = resp.json()["id"]
    runs = await batch_run_ids(engine, evaluation_id)
    assert len(runs) == 9
    # 3 QUEUED (untouched), 3 RETRYING, 2 RUNNING-shaped are made below; 1 already COMPLETED.
    retrying, done = runs[:3], runs[3]
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """UPDATE runs SET status = 'RETRYING', attempt_count = 1, max_attempts = 3,
                     next_attempt_at = now() + interval '1 hour' WHERE id = ANY(:ids)"""
            ),
            {"ids": retrying},
        )
        await conn.execute(
            text(
                """UPDATE runs SET status = 'COMPLETED', outcome = 'AGENT_FINISHED',
                     finished_at = now() WHERE id = :i"""
            ),
            {"i": done},
        )
    done_before = await run_row(engine, done)

    resp = await client.post(f"{API}/evaluations/{evaluation_id}/cancel")
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["cancel_requested_at"] is not None
    assert body["progress"]["complete"] is True
    assert body["progress"]["runs_total"] == 9
    # F13 (Step 9): every run status is a key, so cancelled runs are visible.
    by_status = body["progress"]["runs_by_status"]
    assert set(by_status) == {
        "QUEUED",
        "CLAIMED",
        "RUNNING",
        "RETRYING",
        "COMPLETED",
        "FAILED",
        "CANCELLED",
    }
    assert by_status["COMPLETED"] == 1 and by_status["CANCELLED"] == 8
    assert not any(key in str(body) for key in HIDDEN)

    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT id, status, cancel_reason FROM runs WHERE evaluation_id = :e"),
                {"e": uuid.UUID(evaluation_id)},
            )
        ).all()
        batch = (
            await conn.execute(
                text("SELECT cancel_requested_at, cancel_reason FROM evaluations WHERE id = :e"),
                {"e": uuid.UUID(evaluation_id)},
            )
        ).one()
    by_id = {r.id: r for r in rows}
    assert by_id[done].status == "COMPLETED" and by_id[done].cancel_reason is None
    assert all(
        r.status == "CANCELLED" and r.cancel_reason == "batch"
        for i, r in by_id.items()
        if i != done
    )
    assert await run_row(engine, done) == done_before  # a finished run is untouched
    assert (batch.cancel_reason, batch.cancel_requested_at is not None) == ("user", True)

    again = await client.post(f"{API}/evaluations/{evaluation_id}/cancel")
    assert again.status_code == 200, again.text
    assert again.json() == (await client.get(f"{API}/evaluations/{evaluation_id}")).json()


async def test_a_batch_cancel_flags_running_runs_and_leaves_scores_alone(
    client: httpx.AsyncClient, engine: AsyncEngine, sessionmaker: SM
) -> None:
    s = await seed(client, sessionmaker)  # A1..B3 finished with scores/evaluations; C3 QUEUED
    before_results = (await client.get(f"{API}/evaluations/{s.evaluation_id}/results")).json()
    before_summary = (await client.get(f"{API}/evaluations/{s.evaluation_id}/summary")).json()
    async with engine.connect() as conn:
        scores_before = (
            await conn.execute(text("SELECT count(*), sum(overall) FROM scores"))
        ).one()
    # A worker is running one of the cells (a RUNNING-shaped row, as test_leases seeds it).
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """UPDATE runs SET status = 'RUNNING', outcome = NULL, finished_at = NULL,
                     error_message = NULL, worker_id = 'w1', lease_epoch = 1, attempt_count = 0,
                     lease_expires_at = now() + interval '30 s', heartbeat_at = now()
                   WHERE id = :i"""
            ),
            {"i": s.runs["C1"]},
        )
        await conn.execute(
            text(
                "DELETE FROM attempt_evaluations WHERE attempt_id IN "
                "(SELECT id FROM run_attempts WHERE run_id = :i)"
            ),
            {"i": s.runs["C1"]},
        )
        await conn.execute(text("DELETE FROM run_attempts WHERE run_id = :i"), {"i": s.runs["C1"]})

    resp = await client.post(f"{API}/evaluations/{s.evaluation_id}/cancel")
    assert resp.status_code == 202, resp.text
    assert resp.json()["progress"]["complete"] is False  # C1 is flagged, not stopped yet

    c1 = await run_row(engine, s.runs["C1"])
    assert (c1.status, c1.cancel_reason, c1.lease_epoch) == ("RUNNING", "batch", 1)
    assert (await run_row(engine, s.runs["C3"])).status == "CANCELLED"
    for key in ("A1", "A2", "A3", "B1", "B2", "B3"):
        row = await run_row(engine, s.runs[key])
        assert row.cancel_requested_at is None and row.status in ("COMPLETED", "FAILED"), key

    after_results = (await client.get(f"{API}/evaluations/{s.evaluation_id}/results")).json()
    untouched = {"A1", "A2", "A3", "B1", "B2", "B3"}
    ids = {str(s.runs[k]) for k in untouched}
    pick = lambda page: [r for r in page["results"] if r["run_id"] in ids]  # noqa: E731
    assert pick(after_results) == pick(before_results)
    after_summary = (await client.get(f"{API}/evaluations/{s.evaluation_id}/summary")).json()
    assert after_summary["mean_score"] == before_summary["mean_score"]
    assert after_summary["passed"] == before_summary["passed"]
    async with engine.connect() as conn:
        scores_after = (await conn.execute(text("SELECT count(*), sum(overall) FROM scores"))).one()
    assert scores_after == scores_before
