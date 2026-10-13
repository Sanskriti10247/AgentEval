"""Executes one dispatched run: claim -> start attempt -> ack -> execute -> persist.

execute is the S1 sandbox command for command tasks, or the AgentRunner for
agent tasks. Everything around it (claim, ack timing, fencing, persist) is
shared.

Phase 4: once an agent attempt is durably finished (committed), the
EvaluationRunner grades it in separate transactions (D1, A1). Evaluation can
never change or roll back the attempt; its failures end in its own record.

Postgres decides ownership. The Redis message is acked only after the attempt
is durably RUNNING in Postgres; from then on Postgres (not the stream's
pending list) is what future recovery (Phase 5 leases) works from.
"""

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import fields
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agenteval_core.agent.context import CancelToken
from agenteval_core.logging import get_logger
from agenteval_core.models import AgentVersion, RunAttempt, Task
from agenteval_core.queue.streams import DispatchMessage, RunQueue
from agenteval_core.repositories.agents import AgentVersionRepository
from agenteval_core.repositories.runs import RunRepository
from agenteval_core.repositories.tasks import TaskRepository
from agenteval_core.states import Outcome, status_for
from agenteval_worker.sandbox.docker_sandbox import ExecutionResult, SandboxSpec

if TYPE_CHECKING:
    from agenteval_worker.agent_runner import AgentAttemptResult, AgentRunner
    from agenteval_worker.evaluation_runner import EvaluationRunner

log = get_logger(__name__)

# Extra time on top of the task timeout before the worker gives up waiting on
# the sandbox thread (the sandbox's own watchdog should always fire first).
SANDBOX_GRACE_S = 60

SandboxRunner = Callable[[SandboxSpec], ExecutionResult]


class Executor:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        queue: RunQueue,
        run_sandbox: SandboxRunner,
        worker_id: str,
        agent_runner: "AgentRunner | None" = None,
        evaluation_runner: "EvaluationRunner | None" = None,
    ) -> None:
        self._sm = sessionmaker
        self._queue = queue
        self._run_sandbox = run_sandbox
        self.worker_id = worker_id
        self._agent_runner = agent_runner
        self._evaluation_runner = evaluation_runner
        # In-flight agent runs, so shutdown can cancel them cooperatively.
        self._active: dict[uuid.UUID, CancelToken] = {}

    def cancel_all(self, reason: str) -> int:
        """Cancel every in-flight agent run. Returns how many were cancelled."""
        for token in self._active.values():
            token.cancel(reason)
        return len(self._active)

    async def handle(self, msg: DispatchMessage) -> None:
        if msg.run_id is None:
            log.warning("malformed_message", message_id=msg.message_id)
            await self._queue.ack(msg.message_id)
            return
        with structlog.contextvars.bound_contextvars(
            run_id=str(msg.run_id), worker_id=self.worker_id
        ):
            started = await self._claim_and_start(msg)
            if started is None:
                return
            task, attempt, version = started
            with structlog.contextvars.bound_contextvars(
                task_id=str(task.id), attempt_no=attempt.attempt_no, task_type=task.task_type
            ):
                result: ExecutionResult | AgentAttemptResult
                if task.task_type == "agent":
                    assert version is not None
                    result = await self._execute_agent(task, attempt, version)
                else:
                    result = await self._execute(task, attempt)
                persisted = await self._persist(attempt, result)
                if persisted and task.task_type == "agent":
                    await self._evaluate(task, attempt, result)

    async def _claim_and_start(
        self, msg: DispatchMessage
    ) -> tuple[Task, RunAttempt, AgentVersion | None] | None:
        assert msg.run_id is not None
        async with self._sm() as s:
            runs = RunRepository(s)
            run = await runs.claim(msg.run_id, self.worker_id)
            if run is None:
                # Another worker won, or the run isn't QUEUED any more: the
                # message is a duplicate/stale. Harmless: ack and move on.
                await s.rollback()
                await self._queue.ack(msg.message_id)
                log.info("claim_skipped")
                return None
            await s.commit()
            log.info("run_claimed")

        try:
            async with self._sm() as s:
                runs = RunRepository(s)
                task = await TaskRepository(s).get(run.task_id)
                if task is None:
                    raise RuntimeError("run or task changed underneath the claim")
                version = None
                if task.task_type == "agent":
                    if run.agent_version_id is not None:
                        version = await AgentVersionRepository(s).get(run.agent_version_id)
                    if version is None:
                        raise RuntimeError("agent task run has no agent version")
                attempt = await runs.start_attempt(run.id, self.worker_id)
                if attempt is None:
                    raise RuntimeError("run or task changed underneath the claim")
                await s.commit()
        except Exception as exc:
            log.exception("start_attempt_failed")
            await self._fail_claimed(run.id, f"could not start attempt: {exc}")
            await self._queue.ack(msg.message_id)
            return None

        await self._queue.ack(msg.message_id)
        log.info("attempt_started", attempt_no=attempt.attempt_no)
        return task, attempt, version

    async def _execute(self, task: Task, attempt: RunAttempt) -> ExecutionResult:
        # Command tasks always have a command (ck_tasks_command).
        assert task.command is not None
        spec = SandboxSpec(
            image=task.image,
            command=list(task.command),
            files=dict(task.files),
            timeout_s=task.timeout_s,
            memory_mb=task.memory_mb,
            cpu_limit=task.cpu_limit,
            labels={
                "agenteval.run_id": str(attempt.run_id),
                "agenteval.attempt_id": str(attempt.id),
            },
        )
        try:
            async with asyncio.timeout(task.timeout_s + SANDBOX_GRACE_S):
                return await asyncio.to_thread(self._run_sandbox, spec)
        except TimeoutError:
            return _internal_error("sandbox did not return within timeout + grace")
        except Exception as exc:
            log.exception("sandbox_crashed")
            return _internal_error(f"{type(exc).__name__}: {exc}")

    async def _execute_agent(
        self, task: Task, attempt: RunAttempt, version: AgentVersion
    ) -> "ExecutionResult | AgentAttemptResult":
        if self._agent_runner is None:
            return _internal_error("agent tasks are not enabled on this worker")
        token = CancelToken()
        self._active[attempt.run_id] = token
        try:
            # Backstop only: the runtime enforces task.timeout_s itself.
            async with asyncio.timeout(task.timeout_s + SANDBOX_GRACE_S):
                return await self._agent_runner.run(task, attempt, version, token)
        except TimeoutError:
            return _internal_error("agent run did not return within timeout + grace")
        except Exception as exc:
            log.exception("agent_run_crashed")
            return _internal_error(f"{type(exc).__name__}: {exc}")
        finally:
            self._active.pop(attempt.run_id, None)

    async def _persist(
        self, attempt: RunAttempt, result: "ExecutionResult | AgentAttemptResult"
    ) -> bool:
        """True once the attempt's outcome is committed."""
        try:
            async with self._sm() as s:
                ok = await RunRepository(s).finish_attempt(attempt, **_finish_fields(result))
                if not ok:
                    await s.rollback()
                    log.warning("finish_fenced", outcome=result.outcome)
                    return False
                await s.commit()
        except Exception:
            # Result is lost and the run stays RUNNING until Phase 5 lease
            # recovery. Log what we had so it's at least observable.
            log.exception("persist_failed", outcome=result.outcome, exit_code=result.exit_code)
            return False
        log.info(
            "run_finished",
            status=status_for(result.outcome),
            outcome=result.outcome,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            oom_killed=result.oom_killed,
        )
        return True

    async def _evaluate(
        self, task: Task, attempt: RunAttempt, result: "ExecutionResult | AgentAttemptResult"
    ) -> None:
        """Never raises: the attempt is already durable, and nothing about
        evaluation may affect it. The runner records its own failures."""
        if self._evaluation_runner is None:
            return
        try:
            await self._evaluation_runner.evaluate(
                task, attempt, getattr(result, "workspace", None)
            )
        except Exception as exc:
            log.exception("evaluation_crashed", error_type=type(exc).__name__)

    async def _fail_claimed(self, run_id: uuid.UUID, error: str) -> None:
        try:
            async with self._sm() as s:
                await RunRepository(s).fail_claimed(run_id, self.worker_id, error)
                await s.commit()
        except Exception:
            log.exception("fail_claimed_failed")


def _finish_fields(result: "ExecutionResult | AgentAttemptResult") -> dict[str, Any]:
    """The finish_attempt columns: every field except the evaluator's
    workspace capture (shallow, so a snapshot's bytes are never copied)."""
    return {f.name: getattr(result, f.name) for f in fields(result) if f.name != "workspace"}


def _internal_error(message: str) -> ExecutionResult:
    return ExecutionResult(
        outcome=Outcome.INTERNAL_ERROR,
        exit_code=None,
        oom_killed=False,
        stdout="",
        stderr="",
        stdout_truncated=False,
        stderr_truncated=False,
        duration_ms=0,
        error_message=message,
    )
