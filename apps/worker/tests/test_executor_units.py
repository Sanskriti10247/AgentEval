"""Pure parts of the executor (Step 3): the _internal_error -> failure_reason
mapping, and the per-process limit on real-provider agent runs. No Docker, no database."""

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any

from agenteval_core.agent.context import CancelToken
from agenteval_core.reliability.taxonomy import FailureReason
from agenteval_core.states import Outcome
from agenteval_worker.executor import Executor, _internal_error


def test_backstop_timeouts_are_worker_backstop() -> None:
    for message in (
        "sandbox did not return within timeout + grace",
        "agent run did not return within timeout + grace",
    ):
        r = _internal_error(message, FailureReason.WORKER_BACKSTOP)
        assert r.outcome is Outcome.INTERNAL_ERROR
        assert r.failure_reason is FailureReason.WORKER_BACKSTOP
        assert r.error_message == message


def test_agent_tasks_not_enabled_is_worker_capability() -> None:
    r = _internal_error(
        "agent tasks are not enabled on this worker", FailureReason.WORKER_CAPABILITY
    )
    assert (r.outcome, r.failure_reason) == (
        Outcome.INTERNAL_ERROR,
        FailureReason.WORKER_CAPABILITY,
    )


def test_an_unexpected_exception_is_internal_unexpected() -> None:
    r = _internal_error("RuntimeError: boom", FailureReason.INTERNAL_UNEXPECTED)
    assert (r.outcome, r.failure_reason) == (
        Outcome.INTERNAL_ERROR,
        FailureReason.INTERNAL_UNEXPECTED,
    )


# -- real-provider concurrency (AGENTEVAL_REAL_PROVIDER_CONCURRENCY) ---------------------------


class _CountingRunner:
    """Stands in for AgentRunner: records how many runs overlap."""

    def __init__(self) -> None:
        self.active = 0
        self.peak = 0

    async def run(self, task: Any, attempt: Any, version: Any, token: Any) -> str:
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        return "done"


async def _peak(provider: str, runs: int, concurrency: int) -> int:
    runner = _CountingRunner()
    ex = Executor(
        None,  # type: ignore[arg-type]  # unused by _execute_agent
        None,  # type: ignore[arg-type]
        lambda spec: None,  # type: ignore[arg-type,return-value]
        "w-unit",
        runner,  # type: ignore[arg-type]
        real_provider_concurrency=concurrency,
    )
    task = SimpleNamespace(timeout_s=10)
    version = SimpleNamespace(model_provider=provider)
    results = await asyncio.gather(
        *(
            ex._execute_agent(
                task,  # type: ignore[arg-type]
                SimpleNamespace(run_id=uuid.uuid4()),  # type: ignore[arg-type]
                version,  # type: ignore[arg-type]
                CancelToken(),
            )
            for _ in range(runs)
        )
    )
    assert results == ["done"] * runs
    return runner.peak


async def test_real_provider_runs_are_limited_per_process_and_fake_ones_are_not() -> None:
    assert await _peak("anthropic", 4, 1) == 1
    assert await _peak("anthropic", 4, 2) == 2
    assert await _peak("fake", 4, 1) == 4
