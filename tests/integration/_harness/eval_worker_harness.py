"""Standalone worker process for test_evaluation_reliability.py.

A real OS process so the test can SIGKILL it mid-grading: no cleanup runs, the
evaluation row stays RUNNING with its lease and the evaluation container keeps
running, exactly as a crashed worker leaves them. Only lease expiry plus the
reconciler can recover the evaluation.

    python eval_worker_harness.py <database_url> <redis_url> <worker_id> \
        <ttl_s> <heartbeat_s> <margin_s> <artifact_dir>

Handles exactly one message from the evaluations stream, then exits.
"""

import asyncio
import sys
from pathlib import Path

import docker
from redis.asyncio import Redis

from agenteval_core.artifacts import LocalArtifactStore
from agenteval_core.db import create_engine, create_sessionmaker
from agenteval_core.queue.streams import EVALUATION_FIELD, RunQueue
from agenteval_core.reliability.fencing import LeaseTiming
from agenteval_worker.evaluation_runner import EvaluationRunner
from agenteval_worker.executor import Executor
from agenteval_worker.lease_manager import LeaseManager
from agenteval_worker.sandbox.docker_sandbox import DockerSandbox


async def main() -> None:
    database_url, redis_url, worker_id, ttl_s, heartbeat_s, margin_s, artifact_dir = sys.argv[1:8]
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
    runs = RunQueue(redis, "runs.dispatch", "workers")
    evaluations = RunQueue(redis, "evaluations.dispatch", "workers", field=EVALUATION_FIELD)
    await evaluations.ensure_group()
    client = docker.from_env()
    sandbox = DockerSandbox(client)
    lease_manager = LeaseManager(database_url, timing, worker_id)
    await lease_manager.start()
    runner = EvaluationRunner(
        sessionmaker=sessionmaker,
        docker_client=client,
        artifact_store=LocalArtifactStore(Path(artifact_dir)),
        worker_id=worker_id,
        eval_max_attempts=3,
        lease_manager=lease_manager,
        lease_ttl_s=timing.ttl_s,
        kill_by_attempt_label=sandbox.kill_attempt,
    )
    executor = Executor(
        sessionmaker, runs, sandbox.run, worker_id, None, runner, evaluation_queue=evaluations
    )
    messages = []
    while not messages:
        messages = await evaluations.read(worker_id, count=1, block_ms=500)
    await executor.handle_evaluation(messages[0])
    await lease_manager.stop()


if __name__ == "__main__":
    asyncio.run(main())
