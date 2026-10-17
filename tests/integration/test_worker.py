import asyncio
import threading
import uuid

import docker
import httpx
import pytest
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agenteval_core.models import Run, RunAttempt
from agenteval_core.queue.streams import DispatchMessage, RunQueue
from agenteval_core.reliability.taxonomy import FailureReason
from agenteval_core.repositories.runs import FinishOutcome, RunRepository, StartOutcome
from agenteval_core.states import Outcome, RunStatus
from agenteval_worker.consumer import Consumer
from agenteval_worker.executor import Executor
from agenteval_worker.sandbox.docker_sandbox import DockerSandbox, ExecutionResult, SandboxSpec
from tests.integration.conftest import create_task

SM = async_sessionmaker[AsyncSession]


def fake_result(
    outcome: Outcome = Outcome.SUCCESS,
    exit_code: int | None = 0,
    *,
    failure_reason: FailureReason | None = None,
) -> ExecutionResult:
    return ExecutionResult(
        outcome=outcome,
        exit_code=exit_code,
        oom_killed=False,
        stdout="out",
        stderr="",
        stdout_truncated=False,
        stderr_truncated=False,
        duration_ms=5,
        failure_reason=failure_reason,
    )


class FakeSandbox:
    def __init__(self, result: ExecutionResult | Exception | None = None) -> None:
        self.result = result or fake_result()
        self.calls: list[SandboxSpec] = []
        self._lock = threading.Lock()

    def __call__(self, spec: SandboxSpec) -> ExecutionResult:
        with self._lock:
            self.calls.append(spec)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


async def submit(client: httpx.AsyncClient, **task: object) -> str:
    # max_attempts=1: these tests assert the single attempt's final status, and the API
    # default budget (3) would turn a retryable failure into RETRYING (Step 9).
    task_id = await create_task(client, **task)
    resp = await client.post("/api/v1/runs", json={"task_id": task_id, "max_attempts": 1})
    assert resp.status_code == 202
    return resp.json()["id"]


async def read_one(queue: RunQueue, consumer: str = "test") -> DispatchMessage:
    msgs = await queue.read(consumer, count=1, block_ms=1000)
    assert len(msgs) == 1
    return msgs[0]


async def pending_count(queue: RunQueue) -> int:
    info = await queue._r.xpending(queue.stream, queue.group)
    return int(info["pending"])


async def test_concurrent_claims_have_exactly_one_winner(
    client: httpx.AsyncClient, sessionmaker: SM
) -> None:
    run_id = uuid.UUID(await submit(client))

    async def claim(worker: str) -> bool:
        async with sessionmaker() as s:
            won = await RunRepository(s).claim(run_id, worker) is not None
            await s.commit()
            return won

    results = await asyncio.gather(*[claim(f"w{i}") for i in range(10)])
    assert sum(results) == 1


async def test_executor_happy_path(
    client: httpx.AsyncClient, sessionmaker: SM, queue: RunQueue
) -> None:
    run_id = await submit(client)
    sandbox = FakeSandbox(fake_result(Outcome.NON_ZERO_EXIT, exit_code=3))
    await Executor(sessionmaker, queue, sandbox, "w1").handle(await read_one(queue))

    run = (await client.get(f"/api/v1/runs/{run_id}")).json()
    assert (run["status"], run["outcome"], run["worker_id"], run["attempt_count"]) == (
        "COMPLETED",
        "NON_ZERO_EXIT",
        "w1",
        1,
    )
    assert run["claimed_at"] and run["started_at"] and run["finished_at"]
    [attempt] = (await client.get(f"/api/v1/runs/{run_id}/attempts")).json()
    assert (attempt["attempt_no"], attempt["status"], attempt["exit_code"]) == (1, "FINISHED", 3)
    assert await pending_count(queue) == 0  # acked
    spec = sandbox.calls[0]
    assert spec.labels["agenteval.run_id"] == run_id
    assert spec.files["main.py"].startswith("print(")


async def test_duplicate_message_executes_once(
    client: httpx.AsyncClient, sessionmaker: SM, queue: RunQueue
) -> None:
    run_id = uuid.UUID(await submit(client))
    await queue.publish(run_id)  # duplicate delivery
    m1, m2 = await read_one(queue, "a"), await read_one(queue, "b")
    sandbox = FakeSandbox()
    await asyncio.gather(
        Executor(sessionmaker, queue, sandbox, "wa").handle(m1),
        Executor(sessionmaker, queue, sandbox, "wb").handle(m2),
    )
    assert len(sandbox.calls) == 1
    attempts = (await client.get(f"/api/v1/runs/{run_id}/attempts")).json()
    assert len(attempts) == 1
    assert await pending_count(queue) == 0  # the loser acked too


async def test_stale_message_for_finished_run_is_skipped(
    client: httpx.AsyncClient, sessionmaker: SM, queue: RunQueue
) -> None:
    run_id = uuid.UUID(await submit(client))
    sandbox = FakeSandbox()
    await Executor(sessionmaker, queue, sandbox, "w1").handle(await read_one(queue))
    await queue.publish(run_id)
    await Executor(sessionmaker, queue, sandbox, "w2").handle(await read_one(queue))
    assert len(sandbox.calls) == 1
    run = (await client.get(f"/api/v1/runs/{run_id}")).json()
    assert run["worker_id"] == "w1"


async def test_malformed_message_is_acked(queue: RunQueue) -> None:
    await queue._r.xadd(queue.stream, {"run_id": "not-a-uuid"})
    msg = await read_one(queue)
    assert msg.run_id is None
    await Executor(None, queue, FakeSandbox(), "w1").handle(msg)  # type: ignore[arg-type]
    assert await pending_count(queue) == 0


@pytest.mark.parametrize(
    ("result", "status", "outcome"),
    [
        (fake_result(Outcome.TIMEOUT, exit_code=137), "COMPLETED", "TIMEOUT"),
        (
            fake_result(
                Outcome.SANDBOX_ERROR,
                exit_code=None,
                failure_reason=FailureReason.SANDBOX_EXEC_FAILED,
            ),
            "FAILED",
            "SANDBOX_ERROR",
        ),
        (RuntimeError("boom"), "FAILED", "INTERNAL_ERROR"),
    ],
)
async def test_outcome_to_status_mapping_is_persisted(
    client: httpx.AsyncClient,
    sessionmaker: SM,
    queue: RunQueue,
    result: ExecutionResult | Exception,
    status: str,
    outcome: str,
) -> None:
    run_id = await submit(client)
    await Executor(sessionmaker, queue, FakeSandbox(result), "w1").handle(await read_one(queue))
    run = (await client.get(f"/api/v1/runs/{run_id}")).json()
    assert (run["status"], run["outcome"]) == (status, outcome)
    [attempt] = (await client.get(f"/api/v1/runs/{run_id}/attempts")).json()
    assert attempt["outcome"] == outcome
    if isinstance(result, Exception):
        assert "boom" in attempt["error_message"] and "boom" in run["error_message"]


async def test_finish_is_fenced_to_the_owning_worker(
    client: httpx.AsyncClient, sessionmaker: SM
) -> None:
    run_id = uuid.UUID(await submit(client))
    async with sessionmaker() as s:
        repo = RunRepository(s)
        await repo.claim(run_id, "w1")
        outcome, attempt = await repo.start_attempt(run_id, "w1")
        assert outcome is StartOutcome.STARTED and attempt is not None
        await s.commit()
    # Simulate ownership moving away (what Phase 5 lease recovery will do):
    # a takeover is an epoch bump, on both the run and its RUNNING attempt.
    async with sessionmaker() as s:
        await s.execute(
            update(Run)
            .where(Run.id == run_id)
            .values(worker_id="w2", lease_epoch=Run.lease_epoch + 1)
        )
        await s.execute(
            update(RunAttempt)
            .where(RunAttempt.id == attempt.id)
            .values(lease_epoch=RunAttempt.lease_epoch + 1)
        )
        await s.commit()
    async with sessionmaker() as s:
        fields = {k: v for k, v in fake_result().__dict__.items()}
        assert await RunRepository(s).finish_attempt(attempt, **fields) is FinishOutcome.FENCED
        await s.rollback()
    async with sessionmaker() as s:
        run = await RunRepository(s).get(run_id)
        assert run is not None and run.status == RunStatus.RUNNING


async def test_a_lost_acknowledgement_of_our_own_commit_is_classified_as_success(
    client: httpx.AsyncClient, sessionmaker: SM
) -> None:
    """Rule 6 (fencing §6): finish_attempt's SQL call commits, but the caller
    never sees the result (a dropped connection). A retry with the same
    (stale) attempt object sees 'fenced' from p5_finish - because the attempt
    is no longer RUNNING - but the re-read finds FINISHED at our own epoch
    with our own outcome, so it reports success instead of a false loss."""
    run_id = uuid.UUID(await submit(client))
    async with sessionmaker() as s:
        repo = RunRepository(s)
        await repo.claim(run_id, "w1")
        outcome, attempt = await repo.start_attempt(run_id, "w1")
        assert outcome is StartOutcome.STARTED and attempt is not None
        await s.commit()

    fields = {k: v for k, v in fake_result().__dict__.items()}
    async with sessionmaker() as s:
        assert await RunRepository(s).finish_attempt(attempt, **fields) is FinishOutcome.FINISHED
        await s.commit()
    # The retry: same stale `attempt` (unaware its own write already landed).
    async with sessionmaker() as s:
        assert await RunRepository(s).finish_attempt(attempt, **fields) is FinishOutcome.FINISHED
        await s.rollback()  # the retry itself writes nothing new
    async with sessionmaker() as s:
        run = await RunRepository(s).get(run_id)
        assert run is not None and run.status == RunStatus.COMPLETED


async def test_start_attempt_requires_the_claiming_worker(
    client: httpx.AsyncClient, sessionmaker: SM
) -> None:
    run_id = uuid.UUID(await submit(client))
    async with sessionmaker() as s:
        repo = RunRepository(s)
        await repo.claim(run_id, "w1")
        assert (await repo.start_attempt(run_id, "intruder"))[0] is StartOutcome.FENCED
        outcome, attempt = await repo.start_attempt(run_id, "w1")
        assert outcome is StartOutcome.STARTED and attempt is not None
        assert (await repo.start_attempt(run_id, "w1"))[0] is StartOutcome.FENCED  # already RUNNING
        await s.commit()


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        ("COMPLETED", None),  # terminal without outcome
        ("RUNNING", "SUCCESS"),  # outcome before terminal
        ("COMPLETED", "SANDBOX_ERROR"),  # platform fault can't be COMPLETED
        ("FAILED", "NON_ZERO_EXIT"),  # task result can't be FAILED
        ("RETRYING", None),  # Phase 5 status not yet allowed
    ],
)
async def test_db_rejects_inconsistent_status_outcome(
    client: httpx.AsyncClient, sessionmaker: SM, status: str, outcome: str | None
) -> None:
    run_id = uuid.UUID(await submit(client))
    async with sessionmaker() as s:
        with pytest.raises(IntegrityError):
            await s.execute(
                update(Run).where(Run.id == run_id).values(status=status, outcome=outcome)
            )


async def test_consumer_loop_processes_runs_concurrently(
    client: httpx.AsyncClient, sessionmaker: SM, queue: RunQueue
) -> None:
    run_ids = [await submit(client) for _ in range(5)]
    in_flight = 0
    peak = 0
    lock = threading.Lock()

    def slow_sandbox(spec: SandboxSpec) -> ExecutionResult:
        nonlocal in_flight, peak
        with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        threading.Event().wait(0.3)
        with lock:
            in_flight -= 1
        return fake_result()

    executor = Executor(sessionmaker, queue, slow_sandbox, "loop-worker")
    consumer = Consumer(queue, executor, concurrency=2, block_ms=100, drain_timeout_s=10)
    stop = asyncio.Event()
    loop_task = asyncio.create_task(consumer.run(stop))

    async def all_done() -> bool:
        runs = [(await client.get(f"/api/v1/runs/{r}")).json() for r in run_ids]
        return all(r["status"] == "COMPLETED" for r in runs)

    async with asyncio.timeout(15):
        while not await all_done():  # noqa: ASYNC110 - polling external state (Postgres)
            await asyncio.sleep(0.1)
    stop.set()
    await asyncio.wait_for(loop_task, 5)
    assert peak == 2  # semaphore bounds concurrency


@pytest.mark.docker
async def test_executor_with_real_sandbox(
    client: httpx.AsyncClient, sessionmaker: SM, queue: RunQueue
) -> None:
    run_id = await submit(client)
    client_ = docker.from_env()
    sandbox = DockerSandbox(client_)
    await Executor(sessionmaker, queue, sandbox.run, "w-docker").handle(await read_one(queue))

    run = (await client.get(f"/api/v1/runs/{run_id}")).json()
    assert (run["status"], run["outcome"]) == ("COMPLETED", "NON_ZERO_EXIT")
    [attempt] = (await client.get(f"/api/v1/runs/{run_id}/attempts")).json()
    assert attempt["exit_code"] == 3
    assert attempt["stdout"] == "hello from sandbox\n"
    leftovers = client_.containers.list(all=True, filters={"label": f"agenteval.run_id={run_id}"})
    assert leftovers == []
