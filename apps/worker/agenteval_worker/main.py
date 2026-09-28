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
from agenteval_core.queue.streams import RunQueue, redis_for_blocking_reads
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
    sandbox = DockerSandbox(
        docker.from_env(),
        max_output_bytes=settings.max_output_bytes,
        workspace_tmpfs_mb=settings.workspace_tmpfs_mb,
        pids_limit=settings.pids_limit,
    )
    swept = await asyncio.to_thread(sandbox.sweep_orphans)
    queue = RunQueue(redis, settings.dispatch_stream, settings.consumer_group)
    executor = Executor(create_sessionmaker(engine), queue, sandbox.run, worker_id)
    consumer = Consumer(
        queue,
        executor,
        concurrency=settings.worker_concurrency,
        block_ms=settings.worker_block_ms,
        drain_timeout_s=settings.worker_drain_timeout_s,
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
