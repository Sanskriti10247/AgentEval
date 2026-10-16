"""Executes one dispatched run: claim -> start attempt -> ack -> execute -> persist.

execute is the S1 sandbox command for command tasks, or the AgentRunner for
agent tasks. Everything around it (claim, ack timing, fencing, persist) is
shared.

Phase 4: once an agent attempt is durably finished (committed), the
EvaluationRunner grades it in separate transactions (D1, A1). Evaluation can
never change or roll back the attempt; its failures end in its own record.

Postgres decides ownership. The Redis message is acked only after the attempt
is durably RUNNING in Postgres; from then on Postgres (not the stream's
pending list) is what future recovery works from.

Step 4 (ADR-008 D): the run's lease is held from the moment it's claimed
(`t_sent` seeds its self-stop deadline) until its T-finish call returns,
through the worker's `LeaseManager`. A `fenced` or expired lease cancels the
one `CancelToken` shared by this attempt with reason `lease_lost`; the
executor then makes no further writes at all (T-finish is skipped - the
attempt's own epoch fencing would refuse it anyway, but this avoids the
wasted call and the "finish_fenced" noise). A heartbeat reporting a pending
cancel cancels the same token with the run's own reason ('user' or 'batch').
"""

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import fields
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agenteval_core.agent.context import CancelToken
from agenteval_core.logging import get_logger
from agenteval_core.models import AgentVersion, Run, RunAttempt, Task
from agenteval_core.queue.streams import DispatchMessage, RunQueue
from agenteval_core.reliability.taxonomy import FailureReason
from agenteval_core.repositories.agents import AgentVersionRepository
from agenteval_core.repositories.runs import FinishOutcome, RunRepository, StartOutcome
from agenteval_core.repositories.tasks import TaskRepository
from agenteval_core.states import Outcome, status_for
from agenteval_worker.sandbox.docker_sandbox import ExecutionResult, SandboxSpec

if TYPE_CHECKING:
    from agenteval_worker.agent_runner import AgentAttemptResult, AgentRunner
    from agenteval_worker.evaluation_runner import EvaluationRunner
    from agenteval_worker.lease_manager import LeaseManager

log = get_logger(__name__)

# Extra time on top of the task timeout before the worker gives up waiting on
# the sandbox thread (the sandbox's own watchdog should always fire first).
SANDBOX_GRACE_S = 60

# T-finish persist retry (Step 3): a transient DB error (connection or
# operational - never a CHECK violation) is retried with this backoff before
# giving up and leaving the run for Phase 5 lease recovery.
TRANSIENT_DB_ERRORS = (OperationalError, InterfaceError, OSError, TimeoutError)
FINISH_RETRY_BACKOFF_S = (0.2, 0.5, 1.0, 2.0)

DEFAULT_LEASE_TTL_S = 30.0
DEFAULT_LEASE_MARGIN_S = 10.0

SandboxRunner = Callable[[SandboxSpec], ExecutionResult]


class _LeaseSlot:
    """Per-attempt state the lease callbacks (running on the event loop, not
    in `handle`'s call stack) need: the shared cancel token, and the attempt
    id once it exists (there is none yet while the run is still CLAIMED)."""

    __slots__ = ("token", "attempt_id", "released")

    def __init__(self, token: CancelToken) -> None:
        self.token = token
        self.released = False
        self.attempt_id: uuid.UUID | None = None


class Executor:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        queue: RunQueue,
        run_sandbox: SandboxRunner,
        worker_id: str,
        agent_runner: "AgentRunner | None" = None,
        evaluation_runner: "EvaluationRunner | None" = None,
        *,
        finish_backoff_s: Sequence[float] = FINISH_RETRY_BACKOFF_S,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        lease_manager: "LeaseManager | None" = None,
        lease_ttl_s: float = DEFAULT_LEASE_TTL_S,
        lease_margin_s: float = DEFAULT_LEASE_MARGIN_S,
        kill_by_attempt_label: Callable[[str], int] | None = None,
    ) -> None:
        self._sm = sessionmaker
        self._queue = queue
        self._run_sandbox = run_sandbox
        self.worker_id = worker_id
        self._agent_runner = agent_runner
        self._evaluation_runner = evaluation_runner
        self._finish_backoff_s = tuple(finish_backoff_s)
        self._sleep = sleep
        self._lease_manager = lease_manager
        self._lease_ttl_s = lease_ttl_s
        self._lease_margin_s = lease_margin_s
        self._kill_by_attempt_label = kill_by_attempt_label
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
            claimed = await self._claim(msg)
            if claimed is None:
                return
            run, t_sent = claimed
            slot = _LeaseSlot(CancelToken())
            if self._lease_manager is not None:
                # Seeded immediately from the claim's own send time (design
                # §16): the token self-fences even if every later beat is
                # lost, late, or the keeper itself is gone.
                slot.token.set_deadline(t_sent + self._lease_ttl_s - self._lease_margin_s)
                self._lease_manager.hold(
                    "run",
                    str(run.id),
                    run.lease_epoch,
                    t_sent,
                    on_lease_lost=lambda: self._on_lease_lost(slot),
                    on_cancel_requested=slot.token.cancel,
                    on_renewed=slot.token.set_deadline,
                )
            try:
                started = await self._start(msg, run)
                if started is None:
                    return
                task, attempt, version = started
                slot.attempt_id = attempt.id
                with structlog.contextvars.bound_contextvars(
                    task_id=str(task.id),
                    attempt_no=attempt.attempt_no,
                    task_type=task.task_type,
                ):
                    result: ExecutionResult | AgentAttemptResult
                    if task.task_type == "agent":
                        assert version is not None
                        result = await self._execute_agent(task, attempt, version, slot.token)
                    else:
                        result = await self._execute(task, attempt)
                    if slot.token.reason == "lease_lost":
                        # The lease is gone: no further writes (design §6 rule 3).
                        # Epoch fencing would refuse T-finish anyway; skipping it
                        # avoids the call and the "finish_fenced" noise.
                        log.warning("lease_lost_skip_persist", attempt_id=str(attempt.id))
                        return
                    outcome = await self._persist(attempt, result)
                    # The run is terminal (or RETRYING) now and holds no lease: stop
                    # renewing before grading, or the next renewal would match 0 rows,
                    # look like a lost lease, and kill the evaluation container (it
                    # carries the same agenteval.attempt_id label).
                    self._release(slot, run)
                    if outcome is FinishOutcome.FINISHED and task.task_type == "agent":
                        await self._evaluate(task, attempt, result)
            finally:
                self._release(slot, run)

    def _release(self, slot: _LeaseSlot, run: Run) -> None:
        if self._lease_manager is not None and not slot.released:
            slot.released = True
            self._lease_manager.release("run", str(run.id), run.lease_epoch)

    def _on_lease_lost(self, slot: _LeaseSlot) -> None:
        """Called from the lease manager's own asyncio tasks, not from
        `handle`'s call stack. Cancels the shared token (which, for an agent
        task, makes the runtime stop via its existing mechanism) and, best
        effort, kills any container already running for this attempt (the
        only way to interrupt a command task, which has no cancel token of
        its own to check)."""
        if slot.released:
            return  # a beat that raced the release: the attempt is already finished
        slot.token.cancel("lease_lost")
        if slot.attempt_id is not None and self._kill_by_attempt_label is not None:
            asyncio.create_task(
                asyncio.to_thread(self._kill_by_attempt_label, str(slot.attempt_id))
            )

    async def _claim(self, msg: DispatchMessage) -> tuple[Run, float] | None:
        assert msg.run_id is not None
        t_sent = time.monotonic()  # seeds the lease's self-stop deadline (design §16)
        async with self._sm() as s:
            runs = RunRepository(s)
            run = await runs.claim(msg.run_id, self.worker_id, ttl_s=self._lease_ttl_s)
            if run is None:
                # Another worker won, or the run isn't QUEUED any more: the
                # message is a duplicate/stale. Harmless: ack and move on.
                await s.rollback()
                await self._queue.ack(msg.message_id)
                log.info("claim_skipped")
                return None
            await s.commit()
            log.info("run_claimed")
        return run, t_sent

    async def _start(
        self, msg: DispatchMessage, run: Run
    ) -> tuple[Task, RunAttempt, AgentVersion | None] | None:
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
                outcome, attempt = await runs.start_attempt(run.id, self.worker_id)
                if outcome is StartOutcome.FENCED:
                    # Someone else owns this claim now. "Otherwise it was
                    # fenced: ack and stop" (design §16).
                    await s.rollback()
                    await self._queue.ack(msg.message_id)
                    log.info("start_fenced")
                    return None
                if outcome is StartOutcome.CANCELLED:
                    # R3 lost to a pending cancel; R5 already ran. No attempt exists.
                    await s.commit()
                    await self._queue.ack(msg.message_id)
                    log.info("run_cancelled_before_start")
                    return None
                assert attempt is not None
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
            return _internal_error(
                "sandbox did not return within timeout + grace", FailureReason.WORKER_BACKSTOP
            )
        except Exception as exc:
            log.exception("sandbox_crashed")
            return _internal_error(
                f"{type(exc).__name__}: {exc}", FailureReason.INTERNAL_UNEXPECTED
            )

    async def _execute_agent(
        self, task: Task, attempt: RunAttempt, version: AgentVersion, token: CancelToken
    ) -> "ExecutionResult | AgentAttemptResult":
        if self._agent_runner is None:
            return _internal_error(
                "agent tasks are not enabled on this worker", FailureReason.WORKER_CAPABILITY
            )
        self._active[attempt.run_id] = token
        try:
            # Backstop only: the runtime enforces task.timeout_s itself.
            async with asyncio.timeout(task.timeout_s + SANDBOX_GRACE_S):
                return await self._agent_runner.run(task, attempt, version, token)
        except TimeoutError:
            return _internal_error(
                "agent run did not return within timeout + grace", FailureReason.WORKER_BACKSTOP
            )
        except Exception as exc:
            log.exception("agent_run_crashed")
            return _internal_error(
                f"{type(exc).__name__}: {exc}", FailureReason.INTERNAL_UNEXPECTED
            )
        finally:
            self._active.pop(attempt.run_id, None)

    async def _persist(
        self, attempt: RunAttempt, result: "ExecutionResult | AgentAttemptResult"
    ) -> FinishOutcome:
        """T-finish (Step 3): calls finish_attempt (p5_finish), retrying a
        transient DB error with backoff - a CHECK violation or any other
        unexpected error is not retried, and leaves the run RUNNING for Phase
        5 lease recovery to pick up."""
        for try_no in range(len(self._finish_backoff_s) + 1):
            try:
                async with self._sm() as s:
                    outcome = await RunRepository(s).finish_attempt(
                        attempt, **_finish_fields(result)
                    )
                    if outcome is FinishOutcome.FENCED:
                        await s.rollback()
                        return outcome
                    await s.commit()
                break
            except TRANSIENT_DB_ERRORS as exc:
                if try_no == len(self._finish_backoff_s):
                    log.exception(
                        "persist_failed", outcome=result.outcome, exit_code=result.exit_code
                    )
                    return FinishOutcome.FENCED
                delay = self._finish_backoff_s[try_no]
                log.warning("finish_retry", try_no=try_no, delay_s=delay, error=str(exc))
                await self._sleep(delay)
            except Exception:
                # Result is lost and the run stays RUNNING until Phase 5 lease
                # recovery. Log what we had so it's at least observable.
                log.exception("persist_failed", outcome=result.outcome, exit_code=result.exit_code)
                return FinishOutcome.FENCED
        log.info(
            "run_finished",
            finish_outcome=outcome,
            status=status_for(result.outcome) if outcome is FinishOutcome.FINISHED else None,
            outcome=result.outcome,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            oom_killed=result.oom_killed,
        )
        return outcome

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


def _internal_error(message: str, reason: FailureReason) -> ExecutionResult:
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
        failure_reason=reason,
    )
