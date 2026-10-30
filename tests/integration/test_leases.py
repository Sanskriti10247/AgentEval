"""Phase 5 Step 4: the run lease end to end - claim seeds it, the lease
keeper (a real subprocess, a real asyncpg connection) renews it, and the
worker's own `LeaseManager` decides when to self-stop. FakeProvider only: no
network, no API key. Short timings throughout, as the brief specifies:
ttl 2 s, heartbeat 0.5 s, margin 0.5 s, liveness 1.5 s (tick/reconcile_interval
is unused before Step 5)."""

import asyncio
import os
import signal
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import docker
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agenteval_core.config import Settings
from agenteval_core.models import RunAttempt
from agenteval_core.providers.base import ModelProvider
from agenteval_core.providers.fake import FakeProvider
from agenteval_core.queue.streams import RunQueue
from agenteval_core.reliability.fencing import LeaseTiming
from agenteval_core.repositories.runs import RunRepository
from agenteval_core.repositories.workers import WorkerRepository
from agenteval_worker.agent_runner import AgentRunner
from agenteval_worker.executor import Executor
from agenteval_worker.lease_manager import LeaseManager
from agenteval_worker.sandbox.docker_sandbox import DockerSandbox
from tests.integration.test_agent_executor import (
    leftover_containers,
    load,
    make_agent_run,
    turn,
)

SM = async_sessionmaker[AsyncSession]
pytestmark = pytest.mark.docker

# Short, as the brief specifies.
FAST = LeaseTiming(
    ttl_s=2.0, heartbeat_s=0.5, margin_s=0.5, reconcile_interval_s=0.2, liveness_timeout_s=1.5
)


@pytest.fixture
async def lease_manager(settings: Settings) -> AsyncIterator[LeaseManager]:
    lm = LeaseManager(settings.database_url, FAST, f"w-{uuid.uuid4().hex[:8]}")
    await lm.start()
    # FAST leaves 1.5 s from claim to self-stop: a keeper still starting on a loaded
    # runner (a fresh interpreter, imports, a connection) would eat all of it.
    await lm.wait_ready()
    yield lm
    await lm.stop()


def lease_executor(
    sm: SM,
    queue: RunQueue,
    lease_manager: LeaseManager,
    *,
    providers: dict[str, ModelProvider] | None = None,
    worker_id: str = "w-lease",
    timing: LeaseTiming = FAST,
) -> Executor:
    client = docker.from_env()
    runner = AgentRunner(
        sessionmaker=sm,
        docker_client=client,
        providers={"fake": FakeProvider()} if providers is None else providers,
    )
    sandbox = DockerSandbox(client)
    return Executor(
        sm,
        queue,
        sandbox.run,
        worker_id,
        runner,
        lease_manager=lease_manager,
        lease_ttl_s=timing.ttl_s,
        lease_margin_s=timing.margin_s,
        kill_by_attempt_label=sandbox.kill_attempt,
    )


async def handle_next(ex: Executor, queue: RunQueue) -> None:
    [msg] = await queue.read("test", count=1, block_ms=1000)
    await ex.handle(msg)


async def wait_until(predicate: Any, *, timeout_s: float = 10.0, interval: float = 0.05) -> None:
    async with asyncio.timeout(timeout_s):
        while not await predicate():  # noqa: ASYNC110 - polling Postgres
            await asyncio.sleep(interval)


# -- healthy long run -----------------------------------------------------------------


# Wider than FAST so a loaded machine (the full suite runs Docker-heavy tests
# in parallel) doesn't eat the whole self-stop budget before the first renewal.
HEALTHY = LeaseTiming(
    ttl_s=6.0, heartbeat_s=1.0, margin_s=2.0, reconcile_interval_s=0.5, liveness_timeout_s=3.0
)


async def test_healthy_long_run_completes_and_the_lease_is_renewed(
    sessionmaker: SM, queue: RunQueue, settings: Settings
) -> None:
    """An agent run longer than the ttl (a scripted model delay) still
    completes: the keeper renews the lease every heartbeat_s."""
    lease_manager = LeaseManager(settings.database_url, HEALTHY, f"w-{uuid.uuid4().hex[:8]}")
    await lease_manager.start()
    run_id = await make_agent_run(
        sessionmaker, queue, [turn("slow but healthy", delay_s=HEALTHY.ttl_s * 1.5)]
    )
    async with sessionmaker() as s:
        await WorkerRepository(s).register(
            "w-lease", hostname="h", pid=os.getpid(), version="test", concurrency=1
        )
        await s.commit()

    heartbeats: list[Any] = []

    async def terminal() -> bool:
        async with sessionmaker() as s:
            run = await RunRepository(s).get(run_id)
            assert run is not None
            if run.heartbeat_at is not None:
                heartbeats.append(run.heartbeat_at)
            return run.status in ("COMPLETED", "FAILED", "CANCELLED")

    handle = asyncio.create_task(
        handle_next(lease_executor(sessionmaker, queue, lease_manager, timing=HEALTHY), queue)
    )
    try:
        await wait_until(terminal, timeout_s=HEALTHY.ttl_s * 5, interval=0.1)
        await handle
    finally:
        await lease_manager.stop()

    run, [attempt], _ = await load(sessionmaker, run_id)
    assert (run.status, run.outcome) == ("COMPLETED", "AGENT_FINISHED")
    assert attempt.status == "FINISHED"
    assert len({h for h in heartbeats}) >= 2  # heartbeat_at actually advanced
    async with sessionmaker() as s:
        worker = await WorkerRepository(s).get("w-lease")
        assert worker is not None and worker.last_heartbeat_at is not None
    assert leftover_containers(run_id) == []


# -- cancellation reported over the heartbeat ----------------------------------------


async def test_heartbeat_reported_cancel_ends_the_run_cancelled(
    sessionmaker: SM, queue: RunQueue, lease_manager: LeaseManager
) -> None:
    run_id = await make_agent_run(
        sessionmaker, queue, [turn("slow", delay_s=FAST.ttl_s), turn("done")]
    )
    handle = asyncio.create_task(
        handle_next(lease_executor(sessionmaker, queue, lease_manager), queue)
    )

    async def is_running() -> bool:
        async with sessionmaker() as s:
            run = await RunRepository(s).get(run_id)
            return run is not None and run.status == "RUNNING"

    await wait_until(is_running, timeout_s=10)
    async with sessionmaker() as s:
        await s.execute(
            text(
                "UPDATE runs SET cancel_requested_at = now(), cancel_reason = 'user' WHERE id = :id"
            ),
            {"id": run_id},
        )
        await s.commit()
    await asyncio.wait_for(handle, 10)

    run, [attempt], _ = await load(sessionmaker, run_id)
    assert (run.status, run.outcome) == ("CANCELLED", None)
    assert (attempt.status, attempt.failure_reason) == ("CANCELLED", "cancelled_by_user")
    async with sessionmaker() as s:
        evaluations = await s.execute(
            text("SELECT count(*) FROM attempt_evaluations WHERE attempt_id = :a"),
            {"a": attempt.id},
        )
        assert evaluations.scalar_one() == 0
    assert leftover_containers(run_id) == []


# -- stale completion: hand-run recovery fences the stale owner ------------------------


async def test_stale_completion_is_fenced_by_hand_run_recovery(
    sessionmaker: SM, queue: RunQueue, settings: Settings
) -> None:
    """The heartbeat is effectively suppressed (heartbeat_s far longer than
    the test), so the lease is never renewed before ttl elapses; the test
    hand-runs the design's step-1 recovery SQL (A3 + R9/R10 + epoch bump)
    while the run is still in flight. The resumed runtime makes no further
    model call, its eventual finish is fenced, and its trajectory append is
    fenced too."""
    # A generous ttl/margin here: real Docker container setup for the agent's
    # sandbox happens before the first model call, and must comfortably fit
    # before fence_at so at least one model call is actually in flight when
    # the self-fence trips.
    timing = LeaseTiming(ttl_s=4.0, heartbeat_s=1000.0, margin_s=0.5, reconcile_interval_s=0.2)
    lm = LeaseManager(settings.database_url, timing, "w-stale")
    await lm.start()
    try:
        provider = FakeProvider()
        run_id = await make_agent_run(
            sessionmaker, queue, [turn("stuck", delay_s=15.0), turn("second call")]
        )
        ex = lease_executor(sessionmaker, queue, lm, providers={"fake": provider}, timing=timing)
        handle = asyncio.create_task(handle_next(ex, queue))

        async def attempt_running() -> RunAttempt | None:
            async with sessionmaker() as s:
                rows = await RunRepository(s).list_attempts(run_id)
                return rows[0] if rows else None

        async def has_attempt() -> bool:
            return await attempt_running() is not None

        await wait_until(has_attempt, timeout_s=10)
        attempt = await attempt_running()
        assert attempt is not None
        run_epoch = attempt.lease_epoch

        # Hand-run the reconciler's step-1 recovery: another worker's view,
        # bumping the epoch and marking the stale attempt ABANDONED.
        async with sessionmaker() as s:
            await s.execute(
                text(
                    "UPDATE run_attempts SET status = 'ABANDONED', outcome = 'INTERNAL_ERROR', "
                    "failure_reason = 'lease_expired', retryable = true, finished_at = now() "
                    "WHERE run_id = :r AND attempt_no = 1 AND status = 'RUNNING'"
                ),
                {"r": run_id},
            )
            await s.execute(
                text(
                    "UPDATE runs SET status = 'RETRYING', lease_epoch = lease_epoch + 1, "
                    "lease_expires_at = NULL, next_attempt_at = now() + interval '5 s' "
                    "WHERE id = :r AND lease_epoch = :e"
                ),
                {"r": run_id, "e": run_epoch},
            )
            await s.commit()

        await asyncio.wait_for(handle, 10)

        # No *further* model call: the (now stale) runtime self-stopped
        # before ever issuing the script's second one. (Real Docker sandbox
        # setup happens before the first call, so whether that first call
        # itself got far enough to register depends on container timing;
        # either way, never two.)
        if provider.sessions:
            [session] = provider.sessions
            assert len(session.requests) <= 1

        run, [attempt], events = await load(sessionmaker, run_id)
        assert (run.status, attempt.status) == ("RETRYING", "ABANDONED")  # recovery's write stands
        assert all(e.event_type != "run_finished" for e in events)  # the stale finish never landed
        assert leftover_containers(run_id) == []
    finally:
        await lm.stop()


# -- DB outage: the worker self-stops on its own, writing nothing ----------------------


async def test_db_outage_self_stops_before_the_fence_and_writes_nothing(
    sessionmaker: SM, queue: RunQueue, settings: Settings
) -> None:
    """The keeper can never reach Postgres (a bad DSN stands in for the
    database being down); its process exits immediately and is never alive.
    The worker still self-stops at its own fence_at - a local deadline,
    independent of the keeper - and makes no further writes."""
    bad_dsn = "postgresql+asyncpg://nope:nope@127.0.0.1:1/nonexistent"
    timing = LeaseTiming(ttl_s=1.5, heartbeat_s=0.3, margin_s=1.0, reconcile_interval_s=0.2)
    lm = LeaseManager(bad_dsn, timing, "w-outage")
    await lm.start()
    try:
        provider = FakeProvider()
        run_id = await make_agent_run(sessionmaker, queue, [turn("slow", delay_s=5.0)])
        ex = lease_executor(sessionmaker, queue, lm, providers={"fake": provider}, timing=timing)
        before = asyncio.get_event_loop().time()
        await asyncio.wait_for(handle_next(ex, queue), 10)
        elapsed = asyncio.get_event_loop().time() - before
        # Stopped at (roughly) fence_at = ttl - margin = 0.5 s, not at the
        # model's 5 s delay.
        assert elapsed < 3.0

        # No *further* model call (see the note in the stale-completion test
        # above about real Docker sandbox setup timing).
        if provider.sessions:
            [session] = provider.sessions
            assert len(session.requests) <= 1

        run, attempts, _ = await load(sessionmaker, run_id)
        assert run.status == "RUNNING"  # untouched: T-finish was skipped entirely
        assert attempts[0].status == "RUNNING"
        assert leftover_containers(run_id) == []
    finally:
        await lm.stop()


# -- the keeper process dies; the worker restarts it -----------------------------------


async def test_keeper_killed_is_restarted_and_the_run_completes(
    sessionmaker: SM, queue: RunQueue, settings: Settings
) -> None:
    """A generous ttl/margin here, not FAST: the watchdog needs
    KEEPER_DEAD_AFTER_S (3 s) just to notice the keeper is gone, plus spawn
    and reconnect time, before the restarted keeper's first renewal can move
    the deadline. The model delay must still exceed the *original* fence_at,
    so the run can only complete because the restart actually worked."""
    timing = LeaseTiming(ttl_s=12.0, heartbeat_s=0.5, margin_s=4.0, reconcile_interval_s=0.2)
    lm = LeaseManager(settings.database_url, timing, f"w-{uuid.uuid4().hex[:8]}")
    await lm.start()
    try:
        run_id = await make_agent_run(
            sessionmaker, queue, [turn("first", delay_s=0.3), turn("slow", delay_s=9.0)]
        )
        handle = asyncio.create_task(
            handle_next(lease_executor(sessionmaker, queue, lm, timing=timing), queue)
        )

        async def holds_a_lease() -> bool:
            return bool(lm._held)

        await wait_until(holds_a_lease, timeout_s=10)
        pid = lm._process.pid  # type: ignore[union-attr]
        assert pid is not None
        os.kill(pid, signal.SIGKILL)

        async def process_restarted() -> bool:
            current = lm._process
            return current is not None and current.pid != pid

        await wait_until(process_restarted, timeout_s=10)
        await asyncio.wait_for(handle, 20)

        run, [attempt], _ = await load(sessionmaker, run_id)
        assert (run.status, run.outcome) == ("COMPLETED", "AGENT_FINISHED")
        assert attempt.status == "FINISHED"
        assert leftover_containers(run_id) == []
    finally:
        await lm.stop()


# -- the worker process itself dies; the keeper notices and exits ----------------------

HARNESS = os.path.join(os.path.dirname(__file__), "_harness", "lease_worker_harness.py")


async def test_worker_process_killed_the_keeper_exits_quickly(
    settings: Settings, tmp_path: Path
) -> None:
    """A real OS process (not an asyncio task), standing in for the worker,
    is SIGKILLed outright. Its lease keeper subprocess has no graceful
    shutdown signal for this - it notices only because os.getppid() changes -
    so this proves that detection actually works, not just the in-process
    'parent_is_gone()' unit test."""
    pid_file = tmp_path / "keeper.pid"
    harness = await asyncio.create_subprocess_exec(
        sys.executable,
        HARNESS,
        settings.database_url,
        "30",
        "10",
        str(pid_file),
    )
    keeper_pid: int | None = None
    try:
        async with asyncio.timeout(10):
            while not pid_file.exists():  # noqa: ASYNC110 - polling a file the harness writes
                await asyncio.sleep(0.05)
        keeper_pid = int(pid_file.read_text())
        assert _alive(keeper_pid)

        harness.kill()  # SIGKILL: no graceful shutdown, no chance to tell the keeper
        await asyncio.wait_for(harness.wait(), 5)

        async with asyncio.timeout(5):
            while _alive(keeper_pid):  # noqa: ASYNC110 - polling a process table
                await asyncio.sleep(0.1)
        # Exited on its own, well under the ~30 s test budget the brief allows.
    finally:
        if harness.returncode is None:
            harness.kill()
            await harness.wait()
        if keeper_pid is not None and _alive(keeper_pid):
            os.kill(keeper_pid, signal.SIGKILL)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True
