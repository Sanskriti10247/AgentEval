"""Agent tasks through the real executor: Redis dispatch, Postgres claim and
fencing, a real Docker sandbox, the runtime with FakeProvider, trajectory
rows and attempt aggregates. No network, no API key."""

import asyncio
import uuid
from decimal import Decimal
from typing import Any

import docker
import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agenteval_core.agent.events import EventType, TrajectoryEventData
from agenteval_core.agent.retry import RetryPolicy
from agenteval_core.models import Agent, AgentVersion, Run, RunAttempt, Task, TrajectoryEvent
from agenteval_core.providers.base import ModelProvider
from agenteval_core.providers.fake import FakeProvider
from agenteval_core.queue.streams import RunQueue
from agenteval_core.repositories import trajectory as trajectory_repo
from agenteval_core.repositories.runs import RunRepository
from agenteval_core.repositories.trajectory import TrajectoryRepository
from agenteval_worker.agent_runner import AgentRunner
from agenteval_worker.consumer import Consumer
from agenteval_worker.executor import Executor
from agenteval_worker.sandbox.docker_sandbox import DockerSandbox
from agenteval_worker.trajectory import TrajectoryFenced, TrajectoryWriter
from tests.integration.conftest import SANDBOX_IMAGE

pytestmark = pytest.mark.docker

SM = async_sessionmaker[AsyncSession]
FAST_RETRY = RetryPolicy(max_retries=3, base_s=0.01, cap_s=0.05, jitter_s=0)

BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
TESTS = (
    "import unittest\nfrom calc import add\n\n"
    "class T(unittest.TestCase):\n"
    "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n"
)


def turn(text: str = "", **kw: Any) -> dict[str, Any]:
    return {"type": "turn", "text": text, **kw}


def call(name: str, **input: Any) -> dict[str, Any]:
    return {"name": name, "input": input}


FIX_THE_BUG = [
    turn("Running the tests first.", tool_calls=[call("run_tests")]),
    turn("add() subtracts; fixing.", tool_calls=[call("write_file", path="calc.py", content=FIXED)],
         expect_tool_results_contain=["Tests failed", "-1 != 5"]),
    turn(tool_calls=[call("run_tests")], expect_tool_results_contain=["Wrote"]),
    turn("Fixed the sign in add().", expect_tool_results_contain=["Tests passed."]),
]  # fmt: skip


async def make_agent_run(
    sm: SM,
    queue: RunQueue,
    script: list[dict[str, Any]],
    *,
    limits: dict[str, Any] | None = None,
    image: str = SANDBOX_IMAGE,
    provider: str = "fake",
    with_version: bool = True,
) -> uuid.UUID:
    async with sm() as s:
        agent = Agent(name=f"agent-{uuid.uuid4().hex[:8]}")
        s.add(agent)
        await s.flush()
        version = AgentVersion(
            agent_id=agent.id,
            version=1,
            model_provider=provider,
            model="fake-model",
            system_prompt="You fix bugs in small Python projects.",
            tools=["read_file", "write_file", "list_dir", "search", "shell", "run_tests"],
            shell_allowlist=["python"],
            limits=limits or {"max_steps": 10},
            model_settings={"script": script},
            config_hash=uuid.uuid4().hex * 2,
        )
        task = Task(
            name="fix-add",
            task_type="agent",
            image=image,
            command=None,
            instructions="The tests in test_calc.py fail. Fix calc.py so they pass.",
            test_command=["python", "-m", "unittest", "-q"],
            files={"calc.py": BUGGY, "test_calc.py": TESTS},
            timeout_s=120,
            memory_mb=256,
            cpu_limit=1.0,
        )
        s.add_all([version, task])
        await s.flush()
        run = Run(
            task_id=task.id,
            agent_version_id=version.id if with_version else None,
            status="QUEUED",
            attempt_count=0,
        )
        s.add(run)
        await s.commit()
        run_id = run.id
    await queue.publish(run_id)
    return run_id


def executor(
    sm: SM, queue: RunQueue, *, providers: dict[str, ModelProvider] | None = None,
    worker_id: str = "w-agent", with_runner: bool = True,
) -> Executor:  # fmt: skip
    client = docker.from_env()
    runner = AgentRunner(
        sessionmaker=sm,
        docker_client=client,
        providers={"fake": FakeProvider()} if providers is None else providers,
        retry_policy=FAST_RETRY,
    )
    return Executor(sm, queue, DockerSandbox(client).run, worker_id,
                    runner if with_runner else None)  # fmt: skip


async def handle_next(ex: Executor, queue: RunQueue) -> None:
    [msg] = await queue.read("test", count=1, block_ms=1000)
    await ex.handle(msg)


async def load(sm: SM, run_id: uuid.UUID) -> tuple[Run, list[RunAttempt], list[TrajectoryEvent]]:
    async with sm() as s:
        repo = RunRepository(s)
        run = await repo.get(run_id)
        assert run is not None
        attempts = await repo.list_attempts(run_id)
        events: list[TrajectoryEvent] = []
        for a in attempts:
            events += await TrajectoryRepository(s).list_for_attempt(a.id)
        return run, attempts, events


def leftover_containers(run_id: uuid.UUID) -> list[Any]:
    return docker.from_env().containers.list(
        all=True, filters={"label": f"agenteval.run_id={run_id}"}
    )


# -- end to end ----------------------------------------------------------------------------


async def test_agent_fixes_the_bug_end_to_end(sessionmaker: SM, queue: RunQueue) -> None:
    run_id = await make_agent_run(sessionmaker, queue, FIX_THE_BUG)
    await handle_next(executor(sessionmaker, queue), queue)

    run, [attempt], events = await load(sessionmaker, run_id)
    assert (run.status, run.outcome, run.error_message) == ("COMPLETED", "AGENT_FINISHED", None)
    assert (attempt.status, attempt.outcome, attempt.steps) == ("FINISHED", "AGENT_FINISHED", 4)
    assert (attempt.tokens_in, attempt.tokens_out) == (400, 80)  # 4 calls x fake defaults
    assert attempt.cost_usd == Decimal("0.000800")
    assert attempt.final_answer == "Fixed the sign in add()."
    assert (attempt.stdout, attempt.exit_code) == ("", None)

    assert [e.event_type for e in events] == [
        "run_started", "model_call", "tool_call", "model_call", "tool_call",
        "model_call", "tool_call", "model_call", "run_finished",
    ]  # fmt: skip
    assert [e.seq for e in events] == list(range(9))
    tools = [(e.tool, e.status) for e in events if e.event_type == "tool_call"]
    assert tools == [("run_tests", "ok"), ("write_file", "ok"), ("run_tests", "ok")]
    run_tests = [e for e in events if e.tool == "run_tests"]
    assert run_tests[0].output["content"].startswith("Tests failed")
    assert run_tests[1].output["content"].startswith("Tests passed.")
    calls = [e for e in events if e.event_type == "model_call"]
    assert sum(e.cost_usd for e in calls) == attempt.cost_usd
    assert events[0].input["config"]["model_provider"] == "fake"
    assert events[-1].output["outcome"] == "AGENT_FINISHED"
    assert leftover_containers(run_id) == []


@pytest.mark.parametrize(
    ("script", "limits", "status", "outcome"),
    [
        ([turn(tool_calls=[call("list_dir")]) for _ in range(5)], {"max_steps": 2},
         "COMPLETED", "MAX_STEPS"),
        ([turn(stop="refusal", stop_detail="cyber")], None, "COMPLETED", "MODEL_REFUSAL"),
        ([{"type": "error", "kind": "overloaded"} for _ in range(4)], None,
         "FAILED", "MODEL_ERROR"),
    ],
)  # fmt: skip
async def test_agent_outcomes_are_persisted(
    sessionmaker: SM,
    queue: RunQueue,
    script: list[dict[str, Any]],
    limits: dict[str, Any] | None,
    status: str,
    outcome: str,
) -> None:
    run_id = await make_agent_run(sessionmaker, queue, script, limits=limits)
    await handle_next(executor(sessionmaker, queue), queue)
    run, [attempt], events = await load(sessionmaker, run_id)
    assert (run.status, run.outcome, attempt.outcome) == (status, outcome, outcome)
    assert events[-1].event_type == "run_finished"
    if status == "FAILED":
        assert "retries_exhausted" in (run.error_message or "")
        assert [e.error_class for e in events if e.error_class] == ["overloaded"] * 4
    else:
        assert run.error_message is None
    assert leftover_containers(run_id) == []


async def test_missing_image_is_a_sandbox_error(sessionmaker: SM, queue: RunQueue) -> None:
    run_id = await make_agent_run(sessionmaker, queue, FIX_THE_BUG,
                                  image="agenteval-missing-image:none")  # fmt: skip
    await handle_next(executor(sessionmaker, queue), queue)
    run, [attempt], events = await load(sessionmaker, run_id)
    assert (run.status, run.outcome) == ("FAILED", "SANDBOX_ERROR")
    assert "image not found" in (attempt.error_message or "")
    assert events == []  # nothing ran


async def test_disabled_provider_is_an_internal_error(sessionmaker: SM, queue: RunQueue) -> None:
    run_id = await make_agent_run(sessionmaker, queue, FIX_THE_BUG)
    await handle_next(executor(sessionmaker, queue, providers={}), queue)
    run, _, _ = await load(sessionmaker, run_id)
    assert (run.status, run.outcome) == ("FAILED", "INTERNAL_ERROR")
    assert "'fake' is not enabled" in (run.error_message or "")


async def test_agent_run_without_a_version_fails_before_an_attempt(
    sessionmaker: SM, queue: RunQueue
) -> None:
    run_id = await make_agent_run(sessionmaker, queue, FIX_THE_BUG, with_version=False)
    await handle_next(executor(sessionmaker, queue), queue)
    run, attempts, _ = await load(sessionmaker, run_id)
    assert (run.status, run.outcome, attempts) == ("FAILED", "INTERNAL_ERROR", [])
    assert "no agent version" in (run.error_message or "")


async def test_worker_without_agent_runner_fails_agent_tasks(
    sessionmaker: SM, queue: RunQueue
) -> None:
    run_id = await make_agent_run(sessionmaker, queue, FIX_THE_BUG)
    await handle_next(executor(sessionmaker, queue, with_runner=False), queue)
    run, _, _ = await load(sessionmaker, run_id)
    assert (run.outcome, run.error_message) == (
        "INTERNAL_ERROR", "agent tasks are not enabled on this worker",
    )  # fmt: skip


async def test_command_tasks_are_unaffected_by_the_agent_runner(
    sessionmaker: SM, queue: RunQueue
) -> None:
    async with sessionmaker() as s:
        task = Task(name="cmd", task_type="command", image=SANDBOX_IMAGE,
                    command=["sh", "-c", "echo hi; exit 3"], files={}, timeout_s=20,
                    memory_mb=128, cpu_limit=1.0)  # fmt: skip
        s.add(task)
        await s.flush()
        run = Run(task_id=task.id, status="QUEUED", attempt_count=0)
        s.add(run)
        await s.commit()
        run_id = run.id
    await queue.publish(run_id)
    await handle_next(executor(sessionmaker, queue), queue)
    run, [attempt], events = await load(sessionmaker, run_id)
    assert (run.status, run.outcome, attempt.exit_code, attempt.stdout) == (
        "COMPLETED", "NON_ZERO_EXIT", 3, "hi\n",
    )  # fmt: skip
    assert (attempt.steps, attempt.tokens_in, attempt.cost_usd, attempt.final_answer) == (
        None, None, None, None,
    )  # fmt: skip
    assert events == []


# -- trajectory writer ---------------------------------------------------------------------------


async def running_attempt(sm: SM, queue: RunQueue, worker_id: str = "w1") -> RunAttempt:
    run_id = await make_agent_run(sm, queue, FIX_THE_BUG)
    async with sm() as s:
        repo = RunRepository(s)
        await repo.claim(run_id, worker_id)
        attempt = await repo.start_attempt(run_id, worker_id)
        assert attempt is not None
        await s.commit()
    return attempt


def batch(*seqs: int) -> list[TrajectoryEventData]:
    return [TrajectoryEventData(seq=n, step=0, event_type=EventType.RUN_STARTED) for n in seqs]


async def stored_seqs(sm: SM, attempt: RunAttempt) -> list[int]:
    async with sm() as s:
        return [e.seq for e in await TrajectoryRepository(s).list_for_attempt(attempt.id)]


async def test_writer_is_idempotent_on_retries(sessionmaker: SM, queue: RunQueue) -> None:
    attempt = await running_attempt(sessionmaker, queue)
    writer = TrajectoryWriter(sessionmaker, attempt)
    await writer.append(batch(0, 1))
    await writer.append(batch(0, 1))  # a retry of an ambiguous commit
    await writer.append(batch(2))
    assert await stored_seqs(sessionmaker, attempt) == [0, 1, 2]


async def test_writer_is_fenced_to_the_owning_worker(sessionmaker: SM, queue: RunQueue) -> None:
    attempt = await running_attempt(sessionmaker, queue)
    async with sessionmaker() as s:
        await s.execute(update(Run).where(Run.id == attempt.run_id).values(worker_id="w2"))
        await s.commit()
    with pytest.raises(TrajectoryFenced):
        await TrajectoryWriter(sessionmaker, attempt).append(batch(0))
    assert await stored_seqs(sessionmaker, attempt) == []


async def test_writer_retries_transient_errors(
    sessionmaker: SM, queue: RunQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempt = await running_attempt(sessionmaker, queue)
    real = trajectory_repo.TrajectoryRepository.append_fenced
    failures = {"left": 2}

    async def flaky(self: Any, a: RunAttempt, events: Any) -> bool:
        if failures["left"]:
            failures["left"] -= 1
            raise OperationalError("INSERT", {}, ConnectionResetError("connection reset"))
        return await real(self, a, events)

    monkeypatch.setattr(trajectory_repo.TrajectoryRepository, "append_fenced", flaky)
    slept: list[float] = []

    async def no_sleep(s: float) -> None:
        slept.append(s)

    await TrajectoryWriter(sessionmaker, attempt, sleep=no_sleep).append(batch(0))
    assert (await stored_seqs(sessionmaker, attempt), slept) == ([0], [0.2, 0.5])

    failures["left"] = 99
    with pytest.raises(OperationalError):
        await TrajectoryWriter(sessionmaker, attempt, sleep=no_sleep).append(batch(1))


async def test_losing_ownership_mid_run_stops_the_agent(sessionmaker: SM, queue: RunQueue) -> None:
    """The fence in action: once the run belongs to someone else, the next
    trajectory write fails, the agent stops, and finish_attempt is fenced too."""
    slow_second_step = [
        turn(tool_calls=[call("list_dir")]),
        turn("never recorded", delay_s=1.0),
    ]
    run_id = await make_agent_run(sessionmaker, queue, slow_second_step)
    ex = executor(sessionmaker, queue, worker_id="w-agent")
    task = asyncio.create_task(handle_next(ex, queue))

    async def first_tool_call_recorded() -> bool:
        _, attempts, events = await load(sessionmaker, run_id)
        return any(e.event_type == "tool_call" for e in events)

    async with asyncio.timeout(20):
        while not await first_tool_call_recorded():  # noqa: ASYNC110 - polling Postgres
            await asyncio.sleep(0.05)
    async with sessionmaker() as s:  # ownership moves (what Phase 5 recovery will do)
        await s.execute(update(Run).where(Run.id == run_id).values(worker_id="w-other"))
        await s.commit()
    await asyncio.wait_for(task, 20)

    run, [attempt], events = await load(sessionmaker, run_id)
    assert (run.status, run.worker_id, attempt.status) == ("RUNNING", "w-other", "RUNNING")
    assert "never recorded" not in str([e.output for e in events])
    assert events[-1].event_type == "tool_call"  # nothing written after the fence
    assert leftover_containers(run_id) == []


# -- shutdown ------------------------------------------------------------------------------------


async def test_drain_deadline_cancels_agent_runs_and_records_why(
    sessionmaker: SM, queue: RunQueue
) -> None:
    run_id = await make_agent_run(sessionmaker, queue, [turn("thinking forever", delay_s=60)])
    ex = executor(sessionmaker, queue)
    consumer = Consumer(queue, ex, concurrency=1, block_ms=100, drain_timeout_s=1.5,
                        cancel_grace_s=1.0)  # fmt: skip
    stop = asyncio.Event()
    loop = asyncio.create_task(consumer.run(stop))

    async def running() -> bool:
        run, _, _ = await load(sessionmaker, run_id)
        return run.status == "RUNNING"

    async with asyncio.timeout(20):
        while not await running():  # noqa: ASYNC110 - polling Postgres
            await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(loop, 20)

    run, [attempt], events = await load(sessionmaker, run_id)
    assert (run.status, run.outcome) == ("FAILED", "INTERNAL_ERROR")
    assert run.error_message == "cancelled: worker_shutdown"
    assert events[-1].event_type == "run_finished"
    assert events[-1].output["reason"] == "cancelled: worker_shutdown"
    assert leftover_containers(run_id) == []


async def test_list_for_attempt_pages_by_seq(sessionmaker: SM, queue: RunQueue) -> None:
    attempt = await running_attempt(sessionmaker, queue)
    await TrajectoryWriter(sessionmaker, attempt).append(batch(*range(5)))
    async with sessionmaker() as s:
        repo = TrajectoryRepository(s)
        page = await repo.list_for_attempt(attempt.id, after_seq=1, limit=2)
        assert [e.seq for e in page] == [2, 3]
        total = await s.execute(
            select(TrajectoryEvent).where(TrajectoryEvent.attempt_id == attempt.id)
        )
        assert len(list(total.scalars())) == 5
