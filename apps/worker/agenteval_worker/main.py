"""Worker process entrypoint.

SIGTERM/SIGINT: stop reading new messages, let in-flight runs finish (up to
worker_drain_timeout_s), then exit.
"""

import asyncio
import functools
import os
import secrets
import signal
import socket

import docker
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agenteval_core.config import get_settings
from agenteval_core.db import create_engine, create_sessionmaker
from agenteval_core.logging import configure_logging, get_logger
from agenteval_core.queue.streams import RunQueue, redis_for_blocking_reads
from agenteval_core.repositories.workers import WorkerRepository
from agenteval_worker.agent_runner import AgentRunner
from agenteval_worker.consumer import Consumer
from agenteval_worker.evaluation_runner import EvaluationRunner
from agenteval_worker.executor import Executor
from agenteval_worker.lease_manager import LeaseManager
from agenteval_worker.providers import build_providers
from agenteval_worker.reconciler import Reconciler
from agenteval_worker.sandbox.docker_sandbox import DockerSandbox

log = get_logger(__name__)

WORKER_VERSION = "phase5-step5"
REGISTRY_HEARTBEAT_S = 10.0


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    # F12: unique per process, so a restarted worker never reuses an old id.
    worker_id = settings.worker_id or f"{socket.gethostname()}-{os.getpid()}-{secrets.token_hex(4)}"
    # Startup refuses an invalid timing combination (F7): a heartbeat too
    # close to the ttl, a margin too small for the reconciler tick, etc.
    timing = settings.lease_timing().validate()

    engine = create_engine(settings.database_url)
    redis = redis_for_blocking_reads(settings.redis_url, settings.worker_block_ms)
    docker_client = docker.from_env()
    sandbox = DockerSandbox(
        docker_client,
        max_output_bytes=settings.max_output_bytes,
        workspace_tmpfs_mb=settings.workspace_tmpfs_mb,
        pids_limit=settings.pids_limit,
    )
    swept = await asyncio.to_thread(sandbox.sweep_orphans)
    queue = RunQueue(
        redis, settings.dispatch_stream, settings.consumer_group, maxlen=settings.stream_maxlen
    )
    sessionmaker = create_sessionmaker(engine)
    providers = build_providers(settings)
    agent_runner = AgentRunner(
        sessionmaker=sessionmaker,
        docker_client=docker_client,
        providers=providers,
        max_output_bytes=settings.max_output_bytes,
        workspace_tmpfs_mb=settings.workspace_tmpfs_mb,
        pids_limit=settings.pids_limit,
    )
    evaluation_runner = EvaluationRunner(
        sessionmaker=sessionmaker,
        docker_client=docker_client,
        max_output_bytes=settings.max_output_bytes,
        workspace_tmpfs_mb=settings.workspace_tmpfs_mb,
        pids_limit=settings.pids_limit,
    )

    async with sessionmaker() as s:
        await WorkerRepository(s).register(
            worker_id,
            hostname=socket.gethostname(),
            pid=os.getpid(),
            version=WORKER_VERSION,
            concurrency=settings.worker_concurrency,
        )
        await s.commit()

    lease_manager = LeaseManager(settings.database_url, timing, worker_id)
    await lease_manager.start()

    executor = Executor(
        sessionmaker,
        queue,
        sandbox.run,
        worker_id,
        agent_runner,
        evaluation_runner,
        lease_manager=lease_manager,
        lease_ttl_s=timing.ttl_s,
        lease_margin_s=timing.margin_s,
        kill_by_attempt_label=sandbox.kill_attempt,
    )
    consumer = Consumer(
        queue,
        executor,
        concurrency=settings.worker_concurrency,
        block_ms=settings.worker_block_ms,
        drain_timeout_s=settings.worker_drain_timeout_s,
        cancel_grace_s=settings.worker_cancel_grace_s,
    )

    reconciler = Reconciler(
        sessionmaker,
        queue,
        worker_id,
        interval_s=timing.reconcile_interval_s,
        lease_ttl_s=timing.ttl_s,
        redispatch_after_s=settings.redispatch_after_s,
        orphan_sweep_interval_s=settings.orphan_sweep_interval_s,
        kill_attempt=sandbox.kill_attempt,
        sweep_orphans=sandbox.sweep_orphans,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, functools.partial(_request_stop, stop, sig))

    registry_task = asyncio.create_task(_registry_heartbeat(sessionmaker, worker_id, stop))
    # The consumer group must exist before the first XAUTOCLAIM/XINFO: Consumer.run
    # creates it lazily, so make sure here (idempotent) and only then start ticking.
    await queue.ensure_group()
    reconciler_task = asyncio.create_task(reconciler.run(stop))

    log.info(
        "worker_started",
        worker_id=worker_id,
        concurrency=settings.worker_concurrency,
        orphans_swept=swept,
        providers=sorted(providers),
        lease_ttl_s=timing.ttl_s,
    )
    try:
        await consumer.run(stop)
    finally:
        # The reconciler stops with the worker: another worker's reconciler (or
        # this one after a restart) covers anything left unrecovered.
        stop.set()
        for task in (reconciler_task, registry_task):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await lease_manager.stop()
        async with sessionmaker() as s:
            await WorkerRepository(s).stop(worker_id)
            await s.commit()
        await redis.aclose()
        await engine.dispose()
        log.info("worker_stopped", worker_id=worker_id)


async def _registry_heartbeat(
    sessionmaker: async_sessionmaker[AsyncSession], worker_id: str, stop: asyncio.Event
) -> None:
    """Keeps the `workers` row's last_heartbeat_at fresh (F12, visibility
    only - fencing never depends on it)."""
    while not stop.is_set():
        try:
            async with sessionmaker() as s:
                await WorkerRepository(s).heartbeat(worker_id)
                await s.commit()
        except Exception:
            log.exception("registry_heartbeat_failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=REGISTRY_HEARTBEAT_S)
        except TimeoutError:
            pass


def _request_stop(stop: asyncio.Event, sig: signal.Signals) -> None:
    log.info("shutdown_requested", signal=sig.name)
    stop.set()


if __name__ == "__main__":
    asyncio.run(main())
