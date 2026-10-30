"""The reconciler against real
Postgres and Redis. Every transition, SKIP LOCKED, fencing after recovery,
the advisory lock, side effects strictly after commit, redispatch, stream
hygiene and the worker registry. No Docker: the container kill and the orphan
sweep are injected recorders."""

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import asyncpg
import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agenteval_core.config import Settings
from agenteval_core.queue.streams import RunQueue
from agenteval_core.reliability.policy import RECONCILER_LOCK_NS
from agenteval_core.repositories.recovery import (
    STEP_CLAIMED,
    STEP_RUNNING,
    RecoveryRepository,
)
from agenteval_core.repositories.runs import RunRepository
from agenteval_core.repositories.workers import WorkerRepository
from agenteval_worker.reconciler import Reconciler
from tests.integration.eval_seed import agent_version, create_evaluation, frozen_dataset
from tests.integration.phase5_seed import insert_task, running_run
from tests.integration.test_terminal_functions import finish

SM = async_sessionmaker[AsyncSession]
TTL = 0.2  # a worker is lost, a consumer dead, after 3 x TTL = 0.6 s


# -- fixtures and seeds ------------------------------------------------------------------------


class Harness:
    def __init__(self, rec: Reconciler, kills: list[str], sweeps: list[int]) -> None:
        self.rec = rec
        self.kills = kills
        self.sweeps = sweeps


@pytest.fixture
def make_reconciler(sessionmaker: SM, queue: RunQueue) -> Callable[..., Harness]:
    def build(
        worker_id: str = "w-rec",
        *,
        redispatch_after_s: float = 60.0,
        sweep_interval_s: float = 300.0,
        clock: Callable[[], float] = lambda: 0.0,
    ) -> Harness:
        kills: list[str] = []
        sweeps: list[int] = []

        def sweep() -> int:
            sweeps.append(1)
            return 3

        rec = Reconciler(
            sessionmaker,
            queue,
            worker_id,
            interval_s=0.05,
            lease_ttl_s=TTL,
            redispatch_after_s=redispatch_after_s,
            orphan_sweep_interval_s=sweep_interval_s,
            kill_attempt=lambda attempt_id: kills.append(attempt_id) or 1,
            sweep_orphans=sweep,
            clock=clock,
        )
        return Harness(rec, kills, sweeps)

    return build


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


async def claimed_run(
    engine: AsyncEngine,
    *,
    expired: bool = True,
    unstarted: int = 0,
    cancel: str | None = None,
    epoch: int = 1,
) -> uuid.UUID:
    run_id = uuid.uuid4()
    async with engine.begin() as conn:
        task_id = await insert_task(conn)
        await conn.execute(
            text(
                """INSERT INTO runs (id, task_id, status, attempt_count, worker_id, lease_epoch,
                     lease_expires_at, heartbeat_at, claimed_at, unstarted_claims,
                     cancel_requested_at, cancel_reason)
                   VALUES (:id, :task, 'CLAIMED', 0, 'w1', :e,
                     now() + CAST(CAST(:off AS text) AS interval), now(), now(), :u,
                     CASE WHEN CAST(:c AS text) IS NULL THEN NULL ELSE now() END, :c)"""
            ),
            {
                "id": run_id,
                "task": task_id,
                "e": epoch,
                "off": "-10 seconds" if expired else "30 seconds",
                "u": unstarted,
                "c": cancel,
            },
        )
    return run_id


async def expired_running(
    engine: AsyncEngine, *, max_attempts: int = 3, cancel: str | None = None
) -> tuple[uuid.UUID, uuid.UUID]:
    run_id, attempt_id = await running_run(engine, max_attempts=max_attempts, cancel=cancel)
    await expire(engine, run_id)
    return run_id, attempt_id


async def expire(engine: AsyncEngine, run_id: uuid.UUID) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE runs SET lease_expires_at = now() - interval '10 seconds' WHERE id = :i"),
            {"i": run_id},
        )


async def retrying_run(engine: AsyncEngine, *, due: bool) -> uuid.UUID:
    run_id = uuid.uuid4()
    async with engine.begin() as conn:
        task_id = await insert_task(conn)
        await conn.execute(
            text(
                """INSERT INTO runs (id, task_id, status, attempt_count, max_attempts,
                     lease_epoch, next_attempt_at, dispatched_at)
                   VALUES (:id, :task, 'RETRYING', 1, 3, 2,
                     now() + CAST(CAST(:off AS text) AS interval), now())"""
            ),
            {"id": run_id, "task": task_id, "off": "-10 seconds" if due else "1 hour"},
        )
    return run_id


async def queued_run(
    engine: AsyncEngine, *, dispatched: str | None, created: str = "-5 minutes"
) -> uuid.UUID:
    """`dispatched` and `created` are SQL intervals relative to now;
    `dispatched=None` stores NULL."""
    run_id = uuid.uuid4()
    async with engine.begin() as conn:
        task_id = await insert_task(conn)
        await conn.execute(
            text(
                """INSERT INTO runs (id, task_id, status, attempt_count, dispatched_at, created_at)
                   VALUES (:id, :task, 'QUEUED', 0,
                     CASE WHEN CAST(:d AS text) IS NULL THEN NULL
                          ELSE now() + CAST(CAST(:d AS text) AS interval) END,
                     now() + CAST(CAST(:c AS text) AS interval))"""
            ),
            {"id": run_id, "task": task_id, "d": dispatched, "c": created},
        )
    return run_id


async def run_row(engine: AsyncEngine, run_id: uuid.UUID) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(text("SELECT * FROM runs WHERE id = :i"), {"i": run_id})).one()


async def attempt_row(engine: AsyncEngine, attempt_id: uuid.UUID) -> Any:
    async with engine.connect() as conn:
        return (
            await conn.execute(text("SELECT * FROM run_attempts WHERE id = :i"), {"i": attempt_id})
        ).one()


async def streamed(redis: Redis, settings: Settings) -> list[uuid.UUID]:
    entries = await redis.xrange(settings.dispatch_stream)
    return [uuid.UUID(f[b"run_id"].decode()) for _id, f in entries]


# -- 1a: CLAIMED -------------------------------------------------------------------------------


async def test_r4a_requeues_an_expired_claim_and_publishes_it(
    engine: AsyncEngine, make_reconciler: Callable[..., Harness], redis: Redis, settings: Settings
) -> None:
    run_id = await claimed_run(engine, unstarted=0)
    h = make_reconciler()
    assert await h.rec.step_recover_claimed() == [run_id]

    row = await run_row(engine, run_id)
    assert (row.status, row.unstarted_claims, row.worker_id) == ("QUEUED", 1, None)
    assert (row.lease_epoch, row.lease_expires_at) == (2, None)
    assert row.dispatched_at is not None  # stamped after the publish
    assert await streamed(redis, settings) == [run_id]


async def test_r4b_at_the_limit_fails_the_run_as_claim_abandoned(
    engine: AsyncEngine, make_reconciler: Callable[..., Harness], redis: Redis, settings: Settings
) -> None:
    run_id = await claimed_run(engine, unstarted=2)
    assert await make_reconciler().rec.step_recover_claimed() == []

    row = await run_row(engine, run_id)
    assert (row.status, row.outcome, row.failure_reason) == (
        "FAILED",
        "INTERNAL_ERROR",
        "claim_abandoned",
    )
    assert (row.dead_letter_reason, row.unstarted_claims) == ("retries_exhausted", 3)
    assert row.error_message == "the run's claim expired 3 times before any attempt started"
    assert (row.lease_epoch, row.lease_expires_at) == (2, None)
    assert row.finished_at is not None
    assert row.attempt_count == 0  # an unstarted claim is never an attempt
    assert await streamed(redis, settings) == []


async def test_r5_cancel_flag_on_an_expired_claim_cancels(
    engine: AsyncEngine, make_reconciler: Callable[..., Harness]
) -> None:
    run_id = await claimed_run(engine, cancel="user", unstarted=2)  # cancel beats R4b too
    await make_reconciler().rec.step_recover_claimed()

    row = await run_row(engine, run_id)
    assert (row.status, row.cancel_reason, row.unstarted_claims) == ("CANCELLED", "user", 2)
    assert row.finished_at is not None
    assert (row.lease_epoch, row.lease_expires_at) == (2, None)


# -- 1b: RUNNING -------------------------------------------------------------------------------


async def test_r9_abandons_the_attempt_and_schedules_a_retry(
    engine: AsyncEngine, make_reconciler: Callable[..., Harness]
) -> None:
    run_id, attempt_id = await expired_running(engine, max_attempts=3)
    h = make_reconciler()
    assert await h.rec.step_recover_running() == [attempt_id]
    assert h.kills == [str(attempt_id)]  # after commit

    attempt = await attempt_row(engine, attempt_id)
    assert (attempt.status, attempt.outcome, attempt.failure_reason, attempt.retryable) == (
        "ABANDONED",
        "INTERNAL_ERROR",
        "lease_expired",
        True,
    )
    assert attempt.finished_at is not None
    row = await run_row(engine, run_id)
    assert (row.status, row.outcome, row.error_message) == ("RETRYING", None, None)
    assert row.next_attempt_at is not None and row.finished_at is None
    assert (row.lease_epoch, row.lease_expires_at) == (2, None)
    async with engine.connect() as conn:
        delay = (
            await conn.execute(
                text("SELECT extract(epoch FROM next_attempt_at - now()) FROM runs WHERE id = :i"),
                {"i": run_id},
            )
        ).scalar_one()
    assert 0.5 < float(delay) < 4.0  # backoff(1) = 2 s * U(0.8, 1.2)


async def test_r10_exhausted_budget_fails_the_run_with_a_dead_letter_reason(
    engine: AsyncEngine, make_reconciler: Callable[..., Harness]
) -> None:
    run_id, attempt_id = await expired_running(engine, max_attempts=1)
    await make_reconciler().rec.step_recover_running()

    assert (await attempt_row(engine, attempt_id)).status == "ABANDONED"
    row = await run_row(engine, run_id)
    assert (row.status, row.outcome, row.dead_letter_reason) == (
        "FAILED",
        "INTERNAL_ERROR",
        "retries_exhausted",
    )
    assert row.error_message and row.finished_at is not None
    assert (row.lease_epoch, row.lease_expires_at, row.next_attempt_at) == (2, None, None)


async def test_r11_cancel_flag_on_an_expired_run_cancels_it(
    engine: AsyncEngine, make_reconciler: Callable[..., Harness]
) -> None:
    run_id, attempt_id = await expired_running(engine, cancel="batch")
    await make_reconciler().rec.step_recover_running()

    row = await run_row(engine, run_id)
    assert (row.status, row.outcome, row.cancel_reason) == ("CANCELLED", None, "batch")
    assert row.finished_at is not None and row.lease_epoch == 2
    attempt = await attempt_row(engine, attempt_id)
    assert (attempt.status, attempt.failure_reason) == ("ABANDONED", "lease_expired")


async def test_unexpired_leases_are_left_alone(
    engine: AsyncEngine, make_reconciler: Callable[..., Harness], redis: Redis, settings: Settings
) -> None:
    claimed = await claimed_run(engine, expired=False)
    running, attempt_id = await running_run(engine)  # lease 30 s ahead
    h = make_reconciler()
    await h.rec.tick()

    assert (await run_row(engine, claimed)).status == "CLAIMED"
    row = await run_row(engine, running)
    assert (row.status, row.lease_epoch) == ("RUNNING", 1)
    assert (await attempt_row(engine, attempt_id)).status == "RUNNING"
    assert h.kills == [] and await streamed(redis, settings) == []


# -- 3: retries due ----------------------------------------------------------------------------


async def test_r12_a_due_retry_is_requeued_and_published(
    engine: AsyncEngine, make_reconciler: Callable[..., Harness], redis: Redis, settings: Settings
) -> None:
    due = await retrying_run(engine, due=True)
    not_due = await retrying_run(engine, due=False)
    assert await make_reconciler().rec.step_retries_due() == [due]

    row = await run_row(engine, due)
    assert (row.status, row.next_attempt_at) == ("QUEUED", None)
    assert row.dispatched_at is not None
    assert await streamed(redis, settings) == [due]
    row = await run_row(engine, not_due)
    assert (row.status, row.next_attempt_at is not None) == ("RETRYING", True)


# -- SKIP LOCKED: recovery never waits -----------------------------------------------------------


@pytest.mark.parametrize("held", ["attempt", "run"])
async def test_running_recovery_skips_a_locked_row_without_waiting(
    engine: AsyncEngine,
    make_reconciler: Callable[..., Harness],
    connect: Callable[[], Awaitable[asyncpg.Connection]],
    held: str,
) -> None:
    run_id, attempt_id = await expired_running(engine)
    holder = await connect()
    tx = holder.transaction()
    await tx.start()
    if held == "attempt":  # what a trajectory append holds
        await holder.fetchval("SELECT 1 FROM run_attempts WHERE id = $1 FOR SHARE", attempt_id)
    else:  # what a keeper renewal / the cancel API holds
        await holder.fetchval("SELECT 1 FROM runs WHERE id = $1 FOR UPDATE", run_id)

    h = make_reconciler()
    assert await asyncio.wait_for(h.rec.step_recover_running(), 3) == []  # no wait
    assert (await run_row(engine, run_id)).status == "RUNNING"
    assert (await attempt_row(engine, attempt_id)).status == "RUNNING"

    await tx.rollback()
    assert await h.rec.step_recover_running() == [attempt_id]
    assert (await run_row(engine, run_id)).status == "RETRYING"


async def test_claim_recovery_skips_a_locked_run_without_waiting(
    engine: AsyncEngine,
    make_reconciler: Callable[..., Harness],
    connect: Callable[[], Awaitable[asyncpg.Connection]],
) -> None:
    run_id = await claimed_run(engine)
    holder = await connect()
    tx = holder.transaction()
    await tx.start()
    await holder.fetchval("SELECT 1 FROM runs WHERE id = $1 FOR UPDATE", run_id)

    h = make_reconciler()
    assert await asyncio.wait_for(h.rec.step_recover_claimed(), 3) == []
    assert (await run_row(engine, run_id)).status == "CLAIMED"
    await tx.rollback()
    assert await h.rec.step_recover_claimed() == [run_id]


# -- fencing after recovery --------------------------------------------------------------------


async def test_stale_owner_is_fenced_after_recovery(
    engine: AsyncEngine,
    make_reconciler: Callable[..., Harness],
    connect: Callable[[], Awaitable[asyncpg.Connection]],
) -> None:
    run_id, attempt_id = await expired_running(engine)
    await make_reconciler().rec.step_recover_running()

    conn = await connect()
    # The stale owner's p5_finish, still at the old epoch (1).
    assert await finish(conn, run_id, attempt_id, 1) == "fenced"
    # A keeper-style renewal at the old epoch matches no row.
    renewed = await conn.fetch(
        "UPDATE runs SET lease_expires_at = now() + interval '30 s', heartbeat_at = now() "
        "WHERE (id, lease_epoch) IN (($1::uuid, 1::bigint)) AND status IN ('CLAIMED','RUNNING') "
        "RETURNING id",
        run_id,
    )
    assert renewed == []
    # The trajectory fence (attempt row FOR SHARE at status RUNNING, epoch 1) finds nothing.
    assert (
        await conn.fetchval(
            "SELECT count(*) FROM run_attempts WHERE id = $1 AND status = 'RUNNING' "
            "AND lease_epoch = 1",
            attempt_id,
        )
        == 0
    )
    assert (await run_row(engine, run_id)).status == "RETRYING"


async def test_a_late_renewal_wins_the_race_and_recovery_skips_the_run(
    engine: AsyncEngine,
    make_reconciler: Callable[..., Harness],
    connect: Callable[[], Awaitable[asyncpg.Connection]],
) -> None:
    """A keeper renewal that commits before recovery locks the row: the
    re-evaluated predicate no longer matches, so nothing is recovered."""
    run_id, _ = await expired_running(engine)
    conn = await connect()
    await conn.execute(
        "UPDATE runs SET lease_expires_at = now() + interval '30 s' WHERE id = $1", run_id
    )
    assert await make_reconciler().rec.step_recover_running() == []
    assert (await run_row(engine, run_id)).status == "RUNNING"


# -- the advisory lock -------------------------------------------------------------------------


async def test_a_step_is_skipped_while_another_reconciler_holds_its_lock(
    engine: AsyncEngine,
    make_reconciler: Callable[..., Harness],
    connect: Callable[[], Awaitable[asyncpg.Connection]],
) -> None:
    run_id, attempt_id = await expired_running(engine)
    holder = await connect()
    await holder.execute("SELECT pg_advisory_lock($1, $2)", RECONCILER_LOCK_NS, STEP_RUNNING)
    h = make_reconciler()
    assert await h.rec.step_recover_running() == []
    assert (await run_row(engine, run_id)).status == "RUNNING"
    # A different step's lock is independent.
    claimed = await claimed_run(engine)
    assert await h.rec.step_recover_claimed() == [claimed]

    await holder.execute("SELECT pg_advisory_unlock($1, $2)", RECONCILER_LOCK_NS, STEP_RUNNING)
    assert await h.rec.step_recover_running() == [attempt_id]


async def test_concurrent_reconcilers_never_double_apply(
    engine: AsyncEngine,
    make_reconciler: Callable[..., Harness],
    redis: Redis,
    settings: Settings,
) -> None:
    claimed = [await claimed_run(engine) for _ in range(8)]
    running = [await expired_running(engine) for _ in range(8)]
    a, b = make_reconciler("w-a"), make_reconciler("w-b")
    await asyncio.gather(a.rec.tick(), b.rec.tick(), a.rec.tick(), b.rec.tick())

    for run_id in claimed:
        row = await run_row(engine, run_id)
        assert (row.status, row.unstarted_claims, row.lease_epoch) == ("QUEUED", 1, 2)
    for run_id, attempt_id in running:
        row = await run_row(engine, run_id)
        assert (row.status, row.lease_epoch) == ("RETRYING", 2)
        assert (await attempt_row(engine, attempt_id)).status == "ABANDONED"
    killed = a.kills + b.kills
    assert sorted(killed) == sorted(str(att) for _, att in running)  # each exactly once
    assert set(await streamed(redis, settings)) == set(claimed)


# -- side effects strictly after commit --------------------------------------------------------


async def test_nothing_is_published_or_killed_when_the_transaction_rolls_back(
    engine: AsyncEngine,
    make_reconciler: Callable[..., Harness],
    redis: Redis,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claimed = await claimed_run(engine)
    run_id, attempt_id = await expired_running(engine)

    real_claims = RecoveryRepository.recover_expired_claims
    real_running = RecoveryRepository.recover_expired_running

    async def claims_then_fail(self: RecoveryRepository, *a: Any, **k: Any) -> Any:
        await real_claims(self, *a, **k)
        raise RuntimeError("boom before commit")

    async def running_then_fail(self: RecoveryRepository, *a: Any, **k: Any) -> Any:
        await real_running(self, *a, **k)
        raise RuntimeError("boom before commit")

    monkeypatch.setattr(RecoveryRepository, "recover_expired_claims", claims_then_fail)
    monkeypatch.setattr(RecoveryRepository, "recover_expired_running", running_then_fail)
    h = make_reconciler()
    with pytest.raises(RuntimeError):
        await h.rec.step_recover_claimed()
    with pytest.raises(RuntimeError):
        await h.rec.step_recover_running()
    await h.rec.tick()  # a failing step is logged, never raised, and never half-applies

    assert (await run_row(engine, claimed)).status == "CLAIMED"
    assert (await run_row(engine, run_id)).status == "RUNNING"
    assert (await attempt_row(engine, attempt_id)).status == "RUNNING"
    assert h.kills == [] and await streamed(redis, settings) == []


async def test_a_failing_step_does_not_stop_the_other_steps(
    engine: AsyncEngine,
    make_reconciler: Callable[..., Harness],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    due = await retrying_run(engine, due=True)

    async def broken(self: RecoveryRepository, *a: Any, **k: Any) -> Any:
        raise RuntimeError("step 1a is broken")

    monkeypatch.setattr(RecoveryRepository, "recover_expired_claims", broken)
    h = make_reconciler()
    await h.rec.tick()
    assert (await run_row(engine, due)).status == "QUEUED"
    assert h.sweeps == []


async def test_run_loops_until_stopped(
    engine: AsyncEngine, make_reconciler: Callable[..., Harness]
) -> None:
    run_id = await claimed_run(engine)
    stop = asyncio.Event()
    task = asyncio.create_task(make_reconciler().rec.run(stop))
    try:
        async with asyncio.timeout(5):
            while (await run_row(engine, run_id)).status != "QUEUED":  # noqa: ASYNC110
                await asyncio.sleep(0.05)
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)


# -- 4: redispatch -----------------------------------------------------------------------------


async def test_redispatch_republishes_old_or_unstamped_queued_runs_only(
    engine: AsyncEngine, make_reconciler: Callable[..., Harness], redis: Redis, settings: Settings
) -> None:
    unstamped = await queued_run(engine, dispatched=None)
    old = await queued_run(engine, dispatched="-2 minutes")
    fresh = await queued_run(engine, dispatched="-5 seconds")
    # Just committed by the submit path, its own XADD not yet stamped: left alone.
    brand_new = await queued_run(engine, dispatched=None, created="-1 second")
    claimed = await claimed_run(engine, expired=False)

    published = await make_reconciler().rec.step_redispatch()
    assert set(published) == {unstamped, old}
    assert (await run_row(engine, brand_new)).dispatched_at is None
    assert set(await streamed(redis, settings)) == {unstamped, old}
    for run_id in (unstamped, old):
        stamp = (await run_row(engine, run_id)).dispatched_at
        assert stamp is not None
    async with engine.connect() as conn:
        age = (
            await conn.execute(
                text("SELECT extract(epoch FROM now() - dispatched_at) FROM runs WHERE id = :i"),
                {"i": old},
            )
        ).scalar_one()
    assert float(age) < 30  # restamped to now
    assert (await run_row(engine, fresh)).dispatched_at is not None
    assert (await run_row(engine, claimed)).status == "CLAIMED"
    # A second pass finds nothing left to do.
    assert await make_reconciler().rec.step_redispatch() == []


async def test_mark_dispatched_is_guarded_by_queued(engine: AsyncEngine, sessionmaker: SM) -> None:
    queued = await queued_run(engine, dispatched=None)
    claimed = await claimed_run(engine, expired=False)
    async with sessionmaker() as s:
        assert await RunRepository(s).mark_dispatched([queued, claimed]) == 1
        await s.commit()
    assert (await run_row(engine, queued)).dispatched_at is not None
    assert (await run_row(engine, claimed)).dispatched_at is None


async def test_submit_stamps_dispatched_at(client: httpx.AsyncClient, engine: AsyncEngine) -> None:
    task = await client.post(
        "/api/v1/tasks",
        json={
            "name": "t",
            "image": "agenteval-sandbox:py312",
            "command": ["true"],
            "files": {},
            "timeout_s": 20,
        },
    )
    resp = await client.post("/api/v1/runs", json={"task_id": task.json()["id"]})
    assert resp.status_code == 202, resp.text
    row = await run_row(engine, uuid.UUID(resp.json()["id"]))
    assert row.dispatched_at is not None


async def test_a_failed_stamp_does_not_fail_the_request(
    client: httpx.AsyncClient, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(self: RunRepository, run_ids: Any) -> int:
        raise RuntimeError("stamp failed")

    monkeypatch.setattr(RunRepository, "mark_dispatched", broken)
    task = await client.post(
        "/api/v1/tasks",
        json={
            "name": "t",
            "image": "agenteval-sandbox:py312",
            "command": ["true"],
            "files": {},
            "timeout_s": 20,
        },
    )
    resp = await client.post("/api/v1/runs", json={"task_id": task.json()["id"]})
    assert resp.status_code == 202, resp.text
    assert (await run_row(engine, uuid.UUID(resp.json()["id"]))).dispatched_at is None


async def test_batch_creation_stamps_every_run(
    client: httpx.AsyncClient, engine: AsyncEngine, sessionmaker: SM
) -> None:
    version = await agent_version(sessionmaker)
    ds, _ = await frozen_dataset(client)
    resp = await create_evaluation(client, ds, version, repetitions=2)
    assert resp.status_code == 202, resp.text
    async with engine.connect() as conn:
        rows = (
            (await conn.execute(text("SELECT dispatched_at IS NOT NULL FROM runs"))).scalars().all()
        )
    assert len(rows) == 6 and all(rows)


# -- 4: XAUTOCLAIM reclaim ---------------------------------------------------------------------


async def pending_count(redis: Redis, settings: Settings) -> int:
    return int((await redis.xpending(settings.dispatch_stream, settings.consumer_group))["pending"])


async def test_an_idle_unacked_entry_is_reclaimed_and_acked(
    make_reconciler: Callable[..., Harness], queue: RunQueue, redis: Redis, settings: Settings
) -> None:
    await queue.publish(uuid.uuid4())
    assert len(await queue.read("crashed-worker", count=1, block_ms=500)) == 1
    assert await pending_count(redis, settings) == 1

    h = make_reconciler(redispatch_after_s=0.05)
    assert await h.rec.step_reclaim_pending() == 0  # not idle long enough yet
    await asyncio.sleep(0.1)
    assert await h.rec.step_reclaim_pending() == 1
    assert await pending_count(redis, settings) == 0


async def test_deleted_pending_entries_are_never_acked(
    make_reconciler: Callable[..., Harness],
    queue: RunQueue,
    redis: Redis,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kept, gone = uuid.uuid4(), uuid.uuid4()
    await queue.publish(kept)
    gone_id = await queue.publish(gone)
    assert len(await queue.read("crashed-worker", count=2, block_ms=500)) == 2
    await redis.xdel(settings.dispatch_stream, gone_id)  # still in the PEL, no longer in the stream
    await asyncio.sleep(0.1)

    acked: list[str] = []
    real_ack = queue.ack

    async def spy(message_id: str) -> None:
        acked.append(message_id)
        await real_ack(message_id)

    monkeypatch.setattr(queue, "ack", spy)
    h = make_reconciler(redispatch_after_s=0.05)
    assert await h.rec.step_reclaim_pending() == 1  # no error on the deleted id
    assert gone_id not in acked and len(acked) == 1
    assert await pending_count(redis, settings) == 0


async def test_autoclaim_reports_deleted_ids_in_its_third_part(
    queue: RunQueue, redis: Redis, settings: Settings
) -> None:
    keep_id = await queue.publish(uuid.uuid4())
    gone_id = await queue.publish(uuid.uuid4())
    await queue.read("crashed-worker", count=2, block_ms=500)
    await redis.xdel(settings.dispatch_stream, gone_id)
    await asyncio.sleep(0.05)

    result = await queue.autoclaim("c2", 10, start_id="0-0", count=10)
    assert result.next_cursor == "0-0"
    assert [m.message_id for m in result.messages] == [keep_id]
    assert result.messages[0].run_id is not None
    assert result.deleted_ids == [gone_id]


# -- 7: stream and registry hygiene ------------------------------------------------------------


async def test_every_publish_passes_an_approximate_maxlen(settings: Settings) -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    class FakeRedis:
        async def xadd(self, *args: Any, **kwargs: Any) -> bytes:
            calls.append((args, kwargs))
            return b"1-0"

    queue = RunQueue(FakeRedis(), "s", "g", maxlen=123)  # type: ignore[arg-type]
    await queue.publish(uuid.uuid4())
    assert calls[0][1] == {"maxlen": 123, "approximate": True}


async def test_the_stream_is_trimmed_approximately(redis: Redis, settings: Settings) -> None:
    queue = RunQueue(redis, settings.dispatch_stream, settings.consumer_group, maxlen=10)
    for _ in range(400):
        await queue.publish(uuid.uuid4())
    assert await redis.xlen(settings.dispatch_stream) < 400


async def test_silent_workers_are_marked_lost_and_the_rest_left_alone(
    engine: AsyncEngine, sessionmaker: SM, make_reconciler: Callable[..., Harness]
) -> None:
    async with sessionmaker() as s:
        repo = WorkerRepository(s)
        for wid in ("silent", "alive", "shut-down"):
            await repo.register(wid, hostname="h", pid=1, version="t", concurrency=1)
        await repo.stop("shut-down")
        await s.commit()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE workers SET last_heartbeat_at = now() - interval '1 hour' "
                "WHERE id IN ('silent', 'shut-down')"
            )
        )

    lost, _ = await make_reconciler().rec.step_workers()
    assert lost == ["silent"]
    async with engine.connect() as conn:
        rows = {
            r.id: r
            for r in (
                await conn.execute(text("SELECT id, stopped_at, stop_reason FROM workers"))
            ).all()
        }
    assert rows["silent"].stop_reason == "lost" and rows["silent"].stopped_at is not None
    assert rows["shut-down"].stop_reason == "shutdown"  # WorkerRepository.stop's reason
    assert (rows["alive"].stopped_at, rows["alive"].stop_reason) == (None, None)


async def test_only_idle_consumers_of_stopped_or_unknown_workers_are_deleted(
    sessionmaker: SM, queue: RunQueue, redis: Redis, settings: Settings,
    make_reconciler: Callable[..., Harness],
) -> None:  # fmt: skip
    async with sessionmaker() as s:
        repo = WorkerRepository(s)
        for wid in ("live", "stopped"):
            await repo.register(wid, hostname="h", pid=1, version="t", concurrency=1)
        await repo.stop("stopped")
        await s.commit()
    stream, group = settings.dispatch_stream, settings.consumer_group
    names = [
        "live",  # a live worker's consumer: never deleted
        "live-reconciler",  # nor its reconciler's
        "stopped",
        "stopped-reconciler",
        "unknown",
        "busy-unknown",  # has a pending entry: kept
    ]
    for name in names:
        await redis.xgroup_createconsumer(stream, group, name)
    await queue.publish(uuid.uuid4())
    await redis.xreadgroup(group, "busy-unknown", {stream: ">"}, count=1)
    await asyncio.sleep(3 * TTL + 0.3)
    async with sessionmaker() as s:  # the live worker's registry heartbeat is fresh
        await WorkerRepository(s).heartbeat("live")
        await s.commit()

    _, deleted = await make_reconciler().rec.step_workers()
    assert sorted(deleted) == ["stopped", "stopped-reconciler", "unknown"]
    left = {c.name for c in await queue.consumers()}
    assert {"live", "live-reconciler", "busy-unknown"} <= left
    assert not left & {"stopped", "stopped-reconciler", "unknown"}


async def test_a_recently_active_consumer_is_kept(
    queue: RunQueue, redis: Redis, settings: Settings, make_reconciler: Callable[..., Harness]
) -> None:
    await redis.xgroup_createconsumer(settings.dispatch_stream, settings.consumer_group, "unknown")
    _, deleted = await make_reconciler().rec.step_workers()  # idle < 3 x TTL
    assert deleted == []


# -- 8: orphan sweep ---------------------------------------------------------------------------


async def test_orphan_sweep_runs_once_per_interval(
    make_reconciler: Callable[..., Harness],
) -> None:
    now = [1000.0]
    h = make_reconciler(sweep_interval_s=300.0, clock=lambda: now[0])
    assert await h.rec.step_orphan_sweep() is None  # main() just swept at startup
    now[0] += 299
    assert await h.rec.step_orphan_sweep() is None
    now[0] += 2
    assert await h.rec.step_orphan_sweep() == 3
    assert await h.rec.step_orphan_sweep() is None  # the interval restarts
    assert await h.rec.step_orphan_sweep(force=True) == 3
    assert len(h.sweeps) == 2


# -- the repository's own guards ---------------------------------------------------------------


async def test_recovery_step_returns_false_when_the_advisory_lock_is_taken(
    sessionmaker: SM, connect: Callable[[], Awaitable[asyncpg.Connection]]
) -> None:
    holder = await connect()
    await holder.execute("SELECT pg_advisory_lock($1, $2)", RECONCILER_LOCK_NS, STEP_CLAIMED)
    async with sessionmaker() as s, s.begin():
        assert await RecoveryRepository(s).begin_step(STEP_CLAIMED) is False
    async with sessionmaker() as s, s.begin():
        assert await RecoveryRepository(s).begin_step(STEP_RUNNING) is True
