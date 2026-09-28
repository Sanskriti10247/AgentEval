"""Executes one dispatched run: claim -> start attempt -> ack -> sandbox -> persist.

Postgres decides ownership. The Redis message is acked only after the attempt
is durably RUNNING in Postgres; from then on Postgres (not the stream's
pending list) is what future recovery (Phase 5 leases) works from.
"""

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import asdict

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agenteval_core.logging import get_logger
from agenteval_core.models import RunAttempt, Task
from agenteval_core.queue.streams import DispatchMessage, RunQueue
from agenteval_core.repositories.runs import RunRepository
from agenteval_core.repositories.tasks import TaskRepository
from agenteval_core.states import Outcome, status_for
from agenteval_worker.sandbox.docker_sandbox import ExecutionResult, SandboxSpec

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
    ) -> None:
        self._sm = sessionmaker
        self._queue = queue
        self._run_sandbox = run_sandbox
        self.worker_id = worker_id

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
            task, attempt = started
            with structlog.contextvars.bound_contextvars(
                task_id=str(task.id), attempt_no=attempt.attempt_no
            ):
                result = await self._execute(task, attempt)
                await self._persist(attempt, result)

    async def _claim_and_start(self, msg: DispatchMessage) -> tuple[Task, RunAttempt] | None:
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
                attempt = await runs.start_attempt(run.id, self.worker_id)
                if task is None or attempt is None:
                    raise RuntimeError("run or task changed underneath the claim")
                await s.commit()
        except Exception as exc:
            log.exception("start_attempt_failed")
            await self._fail_claimed(run.id, f"could not start attempt: {exc}")
            await self._queue.ack(msg.message_id)
            return None

        await self._queue.ack(msg.message_id)
        log.info("attempt_started", attempt_no=attempt.attempt_no)
        return task, attempt

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

    async def _persist(self, attempt: RunAttempt, result: ExecutionResult) -> None:
        fields = asdict(result)
        try:
            async with self._sm() as s:
                ok = await RunRepository(s).finish_attempt(attempt, **fields)
                if not ok:
                    await s.rollback()
                    log.warning("finish_fenced", outcome=result.outcome)
                    return
                await s.commit()
        except Exception:
            # Result is lost and the run stays RUNNING until Phase 5 lease
            # recovery. Log what we had so it's at least observable.
            log.exception("persist_failed", outcome=result.outcome, exit_code=result.exit_code)
            return
        log.info(
            "run_finished",
            status=status_for(result.outcome),
            outcome=result.outcome,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            oom_killed=result.oom_killed,
        )

    async def _fail_claimed(self, run_id: uuid.UUID, error: str) -> None:
        try:
            async with self._sm() as s:
                await RunRepository(s).fail_claimed(run_id, self.worker_id, error)
                await s.commit()
        except Exception:
            log.exception("fail_claimed_failed")


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
