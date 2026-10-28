"""The worker with the local sandbox backend (AGENTEVAL_SANDBOX_BACKEND=local): the same
executor, agent runner and evaluation runner as with Docker, and the same results for the
same scripts - no container anywhere. With the lease keeper on a thread
(AGENTEVAL_LEASE_KEEPER=thread), as the demo container runs it."""

import asyncio
import sys
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agenteval_core.artifacts import LocalArtifactStore
from agenteval_core.config import Settings
from agenteval_core.providers.fake import FakeProvider
from agenteval_core.queue.streams import RunQueue
from agenteval_core.repositories.runs import RunRepository
from agenteval_worker.agent_runner import AgentRunner
from agenteval_worker.evaluation_runner import EvaluationRunner
from agenteval_worker.executor import Executor
from agenteval_worker.lease_manager import LeaseManager
from agenteval_worker.sandbox.local import LOCAL_PATH, LocalSandbox
from tests.integration.test_agent_executor import FAST_RETRY, FIX_THE_BUG, FIXED, handle_next
from tests.integration.test_evaluation_runner import GIVE_UP, call, make_run, results, turn
from tests.integration.test_leases import HEALTHY, wait_until

SM = async_sessionmaker[AsyncSession]
# The test interpreter's directory first: CI runners have no /usr/bin/python.
PATH = f"{Path(sys.executable).parent}:{LOCAL_PATH}"


@pytest.fixture
def local(tmp_path: Path) -> LocalSandbox:
    return LocalSandbox(tmp_path / "sandbox", path=PATH)


@pytest.fixture
async def thread_keeper(settings: Settings) -> AsyncIterator[LeaseManager]:
    lm = LeaseManager(settings.database_url, HEALTHY, f"w-{uuid.uuid4().hex[:8]}", in_process=True)
    await lm.start()
    await lm.wait_ready()
    yield lm
    await lm.stop()


def build(
    sm: SM,
    queue: RunQueue,
    store: LocalArtifactStore,
    local: LocalSandbox,
    lease_manager: LeaseManager | None = None,
) -> Executor:
    agent_runner = AgentRunner(
        sessionmaker=sm,
        docker_client=None,
        local_sandbox=local,
        providers={"fake": FakeProvider()},
        retry_policy=FAST_RETRY,
    )
    runner = EvaluationRunner(
        sessionmaker=sm, docker_client=None, local_sandbox=local, artifact_store=store
    )
    return Executor(
        sm,
        queue,
        local.run,
        "w-local",
        agent_runner,
        runner,
        artifact_store=store,
        lease_manager=lease_manager,
        lease_ttl_s=HEALTHY.ttl_s,
        lease_margin_s=HEALTHY.margin_s,
        kill_by_attempt_label=local.kill_attempt,
    )


def test_a_runner_takes_exactly_one_backend(
    sessionmaker: SM, local: LocalSandbox, artifact_store: LocalArtifactStore
) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        AgentRunner(sessionmaker=sessionmaker, docker_client=None, providers={})
    with pytest.raises(ValueError, match="exactly one"):
        EvaluationRunner(
            sessionmaker=sessionmaker,
            docker_client=object(),  # type: ignore[arg-type]
            local_sandbox=local,
            artifact_store=artifact_store,
        )


async def test_the_bug_is_fixed_graded_and_scored_as_with_docker(
    sessionmaker: SM,
    queue: RunQueue,
    artifact_store: LocalArtifactStore,
    local: LocalSandbox,
    thread_keeper: LeaseManager,
) -> None:
    run_id = await make_run(sessionmaker, queue, FIX_THE_BUG)
    await handle_next(build(sessionmaker, queue, artifact_store, local, thread_keeper), queue)

    run, attempt, ev, [score], artifact = await results(sessionmaker, run_id)
    assert (run.status, run.outcome) == ("COMPLETED", "AGENT_FINISHED")
    assert attempt.sandbox_image_id == "local"
    assert ev is not None
    assert (ev.status, ev.task_success, ev.failure_category) == ("SUCCEEDED", True, "passed")
    assert ev.checks is not None
    assert (ev.checks[0]["tests_passed"], ev.checks[0]["tests_total"]) == (2, 2)
    assert ev.integrity == {"ok": True, "tampered": {}, "normalized_paths": []}
    assert score.overall == Decimal("0.97996")  # the Docker test's number exactly
    assert artifact is not None and attempt.workspace_capture == "stored"
    assert list(local.root.iterdir()) == []  # every run directory is gone


async def test_giving_up_and_tampering_fail_the_way_they_do_in_docker(
    sessionmaker: SM, queue: RunQueue, artifact_store: LocalArtifactStore, local: LocalSandbox
) -> None:
    cheat = [
        turn(
            tool_calls=[
                call("write_file", path="calc.py", content=FIXED),
                call("write_file", path="test_calc.py", content="import unittest\n"),
            ]
        ),
        turn("Done."),
    ]
    quit_id = await make_run(sessionmaker, queue, GIVE_UP)
    cheat_id = await make_run(sessionmaker, queue, cheat)
    ex = build(sessionmaker, queue, artifact_store, local)
    await handle_next(ex, queue)
    await handle_next(ex, queue)

    _, _, quit_ev, _, _ = await results(sessionmaker, quit_id)
    assert quit_ev is not None and quit_ev.failure_category == "tests_failed"
    _, _, cheat_ev, [score], _ = await results(sessionmaker, cheat_id)
    assert cheat_ev is not None and cheat_ev.failure_category == "tampering"
    assert score.overall == 0


async def test_a_cancel_reported_by_the_thread_keeper_ends_the_run_cancelled(
    sessionmaker: SM,
    queue: RunQueue,
    artifact_store: LocalArtifactStore,
    local: LocalSandbox,
    thread_keeper: LeaseManager,
) -> None:
    slow = [turn("slow", delay_s=HEALTHY.ttl_s * 2), turn("done")]
    run_id = await make_run(sessionmaker, queue, slow)
    handle = asyncio.create_task(
        handle_next(build(sessionmaker, queue, artifact_store, local, thread_keeper), queue)
    )

    async def running() -> bool:
        async with sessionmaker() as s:
            run = await RunRepository(s).get(run_id)
            return run is not None and run.status == "RUNNING"

    await wait_until(running, timeout_s=10)
    async with sessionmaker() as s:
        await RunRepository(s).request_cancel(run_id)
        await s.commit()
    await asyncio.wait_for(handle, 20)

    run, attempt, ev, _, _ = await results(sessionmaker, run_id)
    assert (run.status, run.cancel_reason) == ("CANCELLED", "user")
    assert (attempt.status, attempt.failure_reason) == ("CANCELLED", "cancelled_by_user")
    assert ev is None
    assert list(local.root.iterdir()) == []
