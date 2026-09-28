"""Worker process entrypoint.

SIGTERM/SIGINT: stop reading new messages, let in-flight runs finish (up to
worker_drain_timeout_s), then exit.
"""

import asyncio
import functools
import os
import signal
import socket

import docker

from agenteval_core.config import get_settings
from agenteval_core.db import create_engine, create_sessionmaker
from agenteval_core.logging import configure_logging, get_logger
from agenteval_core.providers.base import ModelProvider
from agenteval_core.providers.fake import FakeProvider
from agenteval_core.queue.streams import RunQueue, redis_for_blocking_reads
from agenteval_worker.agent_runner import AgentRunner
from agenteval_worker.consumer import Consumer
from agenteval_worker.executor import Executor
from agenteval_worker.sandbox.docker_sandbox import DockerSandbox

log = get_logger(__name__)


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    worker_id = settings.worker_id or f"{socket.gethostname()}-{os.getpid()}"

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
    queue = RunQueue(redis, settings.dispatch_stream, settings.consumer_group)
    sessionmaker = create_sessionmaker(engine)
    # FakeProvider is the default; real providers are opt-in (none wired yet).
    providers: dict[str, ModelProvider] = {}
    if settings.enable_fake_provider:
        providers["fake"] = FakeProvider()
    agent_runner = AgentRunner(
        sessionmaker=sessionmaker,
        docker_client=docker_client,
        providers=providers,
        max_output_bytes=settings.max_output_bytes,
        workspace_tmpfs_mb=settings.workspace_tmpfs_mb,
        pids_limit=settings.pids_limit,
    )
    executor = Executor(sessionmaker, queue, sandbox.run, worker_id, agent_runner)
    consumer = Consumer(
        queue,
        executor,
        concurrency=settings.worker_concurrency,
        block_ms=settings.worker_block_ms,
        drain_timeout_s=settings.worker_drain_timeout_s,
        cancel_grace_s=settings.worker_cancel_grace_s,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, functools.partial(_request_stop, stop, sig))

    log.info(
        "worker_started",
        worker_id=worker_id,
        concurrency=settings.worker_concurrency,
        orphans_swept=swept,
        providers=sorted(providers),
    )
    try:
        await consumer.run(stop)
    finally:
        await redis.aclose()
        await engine.dispose()
        log.info("worker_stopped", worker_id=worker_id)


def _request_stop(stop: asyncio.Event, sig: signal.Signals) -> None:
    log.info("shutdown_requested", signal=sig.name)
    stop.set()


if __name__ == "__main__":
    asyncio.run(main())
