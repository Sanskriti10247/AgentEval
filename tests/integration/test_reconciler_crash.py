"""Phase 5 Step 5, end to end: a worker process is SIGKILLed mid-run; once
its lease expires the reconciler (short timings) abandons the attempt (A3),
schedules the retry (R9), re-queues it (R12), and another executor completes
it as attempt 2. Real Docker, a real worker process, the scripted fake model."""

import asyncio
import os
import signal
import sys
from collections.abc import AsyncIterator
from typing import Any

import docker
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agenteval_core.config import Settings
from agenteval_core.providers.base import ToolSpec
from agenteval_core.providers.fake import FakeProvider, FakeSession
from agenteval_core.queue.streams import RunQueue
from agenteval_core.reliability.fencing import LeaseTiming
from agenteval_worker.agent_runner import AgentRunner
from agenteval_worker.executor import Executor
from agenteval_worker.lease_manager import LeaseManager
from agenteval_worker.reconciler import Reconciler
from agenteval_worker.sandbox.docker_sandbox import DockerSandbox
from tests.integration.test_agent_executor import (
    leftover_containers,
    load,
    make_agent_run,
    turn,
)
from tests.integration.test_leases import wait_until

SM = async_sessionmaker[AsyncSession]
pytestmark = pytest.mark.docker

HARNESS = os.path.join(os.path.dirname(__file__), "_harness", "crash_worker_harness.py")
TIMING = LeaseTiming(
    ttl_s=4.0, heartbeat_s=0.5, margin_s=1.0, reconcile_interval_s=0.2, liveness_timeout_s=1.5
)


class NoDelayFake(FakeProvider):
    """The second attempt's model: the same script without its delays, so the
    retry finishes quickly (the crashed worker's copy still sleeps)."""

    def start(
        self,
        *,
        model: str,
        system: str,
        tools: list[ToolSpec],
        task_prompt: str,
        settings: dict[str, Any],
    ) -> FakeSession:
        fast = {
            "script": [{**step, "delay_s": 0.0} for step in settings["script"]],
        }
        return super().start(
            model=model, system=system, tools=tools, task_prompt=task_prompt, settings=fast
        )


@pytest.fixture
async def worker_process(settings: Settings) -> AsyncIterator[Any]:
    started: list[asyncio.subprocess.Process] = []

    async def start(worker_id: str) -> asyncio.subprocess.Process:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            HARNESS,
            settings.database_url,
            settings.redis_url,
            worker_id,
            str(TIMING.ttl_s),
            str(TIMING.heartbeat_s),
            str(TIMING.margin_s),
        )
        started.append(proc)
        return proc

    yield start
    for proc in started:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def test_a_killed_worker_is_recovered_and_retried_by_another(
    sessionmaker: SM, queue: RunQueue, settings: Settings, worker_process: Any
) -> None:
    # A single model call that would take a minute: the crash lands inside it.
    run_id = await make_agent_run(sessionmaker, queue, [turn("done", delay_s=60.0)], max_attempts=3)
    victim = await worker_process("w-victim")

    async def attempt_running() -> bool:
        async with sessionmaker() as s:
            row = (
                await s.execute(
                    text(
                        "SELECT count(*) FROM run_attempts WHERE run_id = :r AND status = 'RUNNING'"
                    ),
                    {"r": run_id},
                )
            ).scalar_one()
        return bool(row)

    await wait_until(attempt_running, timeout_s=30)
    victim.send_signal(signal.SIGKILL)
    await victim.wait()
    killed_at = asyncio.get_event_loop().time()

    # The recovering side: an executor and a reconciler of a second worker.
    client = docker.from_env()
    sandbox = DockerSandbox(client)
    lease_manager = LeaseManager(settings.database_url, TIMING, "w-rescuer")
    await lease_manager.start()
    executor = Executor(
        sessionmaker,
        queue,
        sandbox.run,
        "w-rescuer",
        AgentRunner(
            sessionmaker=sessionmaker, docker_client=client, providers={"fake": NoDelayFake()}
        ),
        lease_manager=lease_manager,
        lease_ttl_s=TIMING.ttl_s,
        lease_margin_s=TIMING.margin_s,
        kill_by_attempt_label=sandbox.kill_attempt,
    )
    killed: list[str] = []
    reconciler = Reconciler(
        sessionmaker,
        queue,
        "w-rescuer",
        interval_s=TIMING.reconcile_interval_s,
        lease_ttl_s=TIMING.ttl_s,
        redispatch_after_s=60.0,
        orphan_sweep_interval_s=300.0,
        kill_attempt=lambda attempt_id: (
            killed.append(attempt_id) or sandbox.kill_attempt(attempt_id)
        ),
        sweep_orphans=sandbox.sweep_orphans,
    )
    stop = asyncio.Event()
    reconciler_task = asyncio.create_task(reconciler.run(stop))
    consumer_task: asyncio.Task[None] | None = None
    try:
        # Before the lease expires nothing is recovered.
        await asyncio.sleep(1.0)
        async with sessionmaker() as s:
            status = (
                await s.execute(text("SELECT status FROM runs WHERE id = :r"), {"r": run_id})
            ).scalar_one()
        assert status == "RUNNING"

        async def seen_retry_state() -> bool:
            async with sessionmaker() as s:
                row = (
                    await s.execute(
                        text("SELECT status, lease_epoch FROM runs WHERE id = :r"), {"r": run_id}
                    )
                ).one()
            return bool(row.status in ("RETRYING", "QUEUED") and row.lease_epoch == 2)

        await wait_until(seen_retry_state, timeout_s=15)
        assert asyncio.get_event_loop().time() - killed_at >= TIMING.ttl_s - 1.5
        _, attempts, _ = await load(sessionmaker, run_id)
        assert [(a.attempt_no, a.status, a.failure_reason) for a in attempts] == [
            (1, "ABANDONED", "lease_expired")
        ]
        assert killed == [str(attempts[0].id)]

        # The retry becomes due (backoff <= 2.4 s), is re-queued and published; the
        # rescuer's executor picks it up.
        async def consume() -> None:
            while True:
                messages = await queue.read("w-rescuer", count=1, block_ms=500)
                for msg in messages:
                    await executor.handle(msg)
                    return

        consumer_task = asyncio.create_task(consume())
        await asyncio.wait_for(consumer_task, 60)
    finally:
        stop.set()
        await asyncio.wait_for(reconciler_task, 5)
        if consumer_task is not None and not consumer_task.done():
            consumer_task.cancel()
        await lease_manager.stop()

    run, attempts, _ = await load(sessionmaker, run_id)
    assert (run.status, run.outcome) == ("COMPLETED", "AGENT_FINISHED")
    assert [(a.attempt_no, a.status, a.worker_id) for a in attempts] == [
        (1, "ABANDONED", "w-victim"),
        (2, "FINISHED", "w-rescuer"),
    ]
    assert run.attempt_count == 2
    assert leftover_containers(run_id) == []
