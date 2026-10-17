"""Standalone worker process for test_reconciler_crash.py.

A real OS process so the test can SIGKILL it mid-run: no cleanup runs, the
container and the attempt row are left exactly as a crashed worker leaves
them, and only lease expiry plus the reconciler can recover the run.

    python crash_worker_harness.py <database_url> <redis_url> <worker_id> \
        <ttl_s> <heartbeat_s> <margin_s>

Handles exactly one dispatched run (with the scripted fake provider, so the
agent's model call is the slow part), then exits.
"""

import asyncio
import sys

import docker
from redis.asyncio import Redis

from agenteval_core.db import create_engine, create_sessionmaker
from agenteval_core.providers.fake import FakeProvider
from agenteval_core.queue.streams import RunQueue
from agenteval_core.reliability.fencing import LeaseTiming
from agenteval_worker.agent_runner import AgentRunner
from agenteval_worker.executor import Executor
from agenteval_worker.lease_manager import LeaseManager
from agenteval_worker.sandbox.docker_sandbox import DockerSandbox


async def main() -> None:
    database_url, redis_url, worker_id, ttl_s, heartbeat_s, margin_s = sys.argv[1:7]
    timing = LeaseTiming(
        ttl_s=float(ttl_s),
        heartbeat_s=float(heartbeat_s),
        margin_s=float(margin_s),
        reconcile_interval_s=0.2,
        liveness_timeout_s=1.5,
    ).validate()
    engine = create_engine(database_url)
    sessionmaker = create_sessionmaker(engine)
    redis = Redis.from_url(redis_url)
    queue = RunQueue(redis, "runs.dispatch", "workers")
    await queue.ensure_group()
    client = docker.from_env()
    runner = AgentRunner(
        sessionmaker=sessionmaker, docker_client=client, providers={"fake": FakeProvider()}
    )
    sandbox = DockerSandbox(client)
    lease_manager = LeaseManager(database_url, timing, worker_id)
    await lease_manager.start()
    executor = Executor(
        sessionmaker,
        queue,
        sandbox.run,
        worker_id,
        runner,
        lease_manager=lease_manager,
        lease_ttl_s=timing.ttl_s,
        lease_margin_s=timing.margin_s,
        kill_by_attempt_label=sandbox.kill_attempt,
    )
    messages = []
    while not messages:
        messages = await queue.read(worker_id, count=1, block_ms=500)
    await executor.handle(messages[0])
    await lease_manager.stop()


if __name__ == "__main__":
    asyncio.run(main())
