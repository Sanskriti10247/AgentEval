"""Run persistence. Every state change is a single guarded UPDATE
(`WHERE status = <expected> [AND worker_id = ...]`) so concurrent workers can't
make an illegal transition: the loser simply matches 0 rows.

T-finish (Step 3) is the exception: it is the p5_finish/p5_cancel SQL
functions from migration 0004, called through SQLAlchemy `text()`. They do
their own fencing (lease_epoch, not worker_id) and their own locking (the
attempt row, then the run row); see ADR-008 and reliability/fencing.py §6.

Repositories flush but never commit; services/executors own transactions.
"""

import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import case, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from agenteval_core.logging import get_logger
from agenteval_core.models import Run, RunAttempt
from agenteval_core.models.base import new_id
from agenteval_core.reliability.backoff import backoff_delay
from agenteval_core.reliability.policy import (
    DeadLetterReason,
    after_completed_attempt,
    after_failed_attempt,
)
from agenteval_core.reliability.taxonomy import (
    CancelReason,
    FailureReason,
    WorkspaceCapture,
    is_retryable,
)
from agenteval_core.reliability.transitions import RunState
from agenteval_core.states import (
    COMPLETED_OUTCOMES,
    AttemptStatus,
    Outcome,
    RunStatus,
    check_transition,
)

log = get_logger(__name__)

# The run_attempts columns p5_finish/p5_cancel accept in their JSONB payload
# (migration 0004's ATTEMPT_RESULT_KEYS). Every key is always sent, even as
# JSON null, so a command task's agent-only fields (steps, tokens, ...) are
# written as NULL exactly as finish_attempt always did before Step 3.
_ATTEMPT_RESULT_KEYS = (
    "exit_code",
    "oom_killed",
    "stdout",
    "stderr",
    "stdout_truncated",
    "stderr_truncated",
    "duration_ms",
    "error_message",
    "steps",
    "tokens_in",
    "tokens_out",
    "cost_usd",
    "final_answer",
    "sandbox_image_id",
)

_FINISH_SQL = text(
    "SELECT p5_finish(:run, :attempt_id, :epoch, :target, :outcome, :failure_reason, "
    ":retryable, :workspace_capture, :next_attempt_at, :dead_letter_reason, :error_message, "
    "CAST(:attempt AS jsonb))"
)
_CANCEL_SQL = text(
    "SELECT result, storage_keys FROM p5_cancel(:run, :attempt_id, :epoch, CAST(:attempt AS jsonb))"
)


# The longest a cancel statement waits for a row lock. The cancel API only ever
# touches `runs` (never the attempt row or artifact work, ADR-008), so any
# holder is a short transaction; this is the ceiling, not the expected wait.
CANCEL_LOCK_TIMEOUT = text("SET LOCAL lock_timeout = '20s'")

# The states a cancel request can still act on (R2, R13 cancel directly; R3/R5
# and R11 are the owner's or the reconciler's for CLAIMED/RUNNING).
CANCELLABLE_STATUSES = (
    RunStatus.QUEUED,
    RunStatus.RETRYING,
    RunStatus.CLAIMED,
    RunStatus.RUNNING,
)
_CANCEL_DIRECTLY = (RunStatus.QUEUED, RunStatus.RETRYING)


class CancelDisposition(StrEnum):
    """What a cancel request did, decided by the state that actually won."""

    CANCELLED = "cancelled"  # QUEUED/RETRYING went straight to CANCELLED (R2, R13)
    FLAGGED = "flagged"  # CLAIMED/RUNNING got the flag; the owner or reconciler finishes it
    ALREADY = "already"  # CANCELLED or already flagged: idempotent repeat, nothing written
    FINISHED = "finished"  # COMPLETED/FAILED: the terminal commit won (G7), nothing written
    NOT_FOUND = "not_found"


def cancel_values(reason: CancelReason) -> dict[str, Any]:
    """The single guarded UPDATE behind every cancel request (run or batch).

    One statement, so the decision is atomic with the guard: QUEUED/RETRYING
    become CANCELLED with finished_at and no next_attempt_at (ck_runs_retrying,
    ck_runs_cancelled_has_request), CLAIMED/RUNNING only get the flag
    (ck_runs_cancel_states). Callers add the WHERE (status in CANCELLABLE and
    cancel_requested_at IS NULL)."""
    direct = Run.status.in_([s.value for s in _CANCEL_DIRECTLY])
    return {
        "status": case((direct, RunStatus.CANCELLED.value), else_=Run.status),
        "finished_at": case((direct, func.now()), else_=Run.finished_at),
        "next_attempt_at": None,
        "cancel_requested_at": func.now(),
        "cancel_reason": reason.value,
    }


class FinishOutcome(StrEnum):
    """What T-finish did with this call, for the executor to act on."""

    FINISHED = "finished"  # committed as COMPLETED/RETRYING/FAILED: eligible for evaluation
    CANCELLED = "cancelled"  # a pending cancel won instead (restart_cancel -> p5_cancel)
    FENCED = "fenced"  # this worker no longer owns the attempt; nothing was written


class StartOutcome(StrEnum):
    """What start_attempt (R3) did with this call (design §16, 4508 §3.4)."""

    STARTED = "started"  # RUNNING, with attempt A1 inserted
    CANCELLED = "cancelled"  # R3 lost to a pending cancel instead: R5, no attempt created
    FENCED = "fenced"  # this worker no longer owns the claim; nothing was written


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"not JSON serializable: {type(value)!r}")


def _attempt_payload(fields: dict[str, Any]) -> str:
    return json.dumps({k: fields.get(k) for k in _ATTEMPT_RESULT_KEYS}, default=_json_default)


class RunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session
        # Set by finish_attempt when a pending cancel won (p5_cancel): the files
        # of the artifact rows it deleted. The caller unlinks them AFTER it
        # commits (ADR-008 C); a repository never touches the store itself.
        self.cancelled_storage_keys: list[str] = []

    async def get(self, run_id: uuid.UUID) -> Run | None:
        return await self._s.get(Run, run_id, populate_existing=True)

    async def get_by_idempotency_key(self, key: str) -> Run | None:
        result = await self._s.execute(select(Run).where(Run.idempotency_key == key))
        return result.scalar_one_or_none()

    async def create_queued(
        self,
        task_id: uuid.UUID,
        idempotency_key: str | None,
        agent_version_id: uuid.UUID | None = None,
    ) -> Run | None:
        """Insert a QUEUED run. Returns None if the idempotency key already exists.

        ON CONFLICT DO NOTHING makes concurrent duplicate submissions safe
        without relying on catching IntegrityError.
        """
        stmt = (
            pg_insert(Run)
            .values(
                id=new_id(),
                task_id=task_id,
                agent_version_id=agent_version_id,
                status=RunStatus.QUEUED,
                idempotency_key=idempotency_key,
                attempt_count=0,
            )
            .on_conflict_do_nothing(index_elements=[Run.idempotency_key])
            .returning(Run)
        )
        result = await self._s.execute(stmt)
        return result.scalar_one_or_none()

    async def request_cancel(
        self, run_id: uuid.UUID, reason: CancelReason = CancelReason.USER
    ) -> CancelDisposition:
        """Cancel API, one run (F5, F15, G7). R2/R13: QUEUED/RETRYING ->
        CANCELLED directly; CLAIMED/RUNNING: the flag only (the owner sees it
        at R3 -> R5, on a keeper beat, or at T-finish -> restart_cancel; the
        reconciler applies R5/R11 if the lease expires). Touches only `runs`.

        The guard is in SQL, so a race with a finish resolves to exactly one
        winner: a finish that committed first leaves 0 rows here and we answer
        by the state that won (FINISHED -> 409, nothing written); a cancel that
        committed first makes the owner's p5_finish see the flag (G7)."""
        check_transition(RunStatus.QUEUED, RunStatus.CANCELLED)  # R2
        check_transition(RunStatus.RETRYING, RunStatus.CANCELLED)  # R13
        await self._s.execute(CANCEL_LOCK_TIMEOUT)
        # A status that bounces between our UPDATE and the re-read (RETRYING ->
        # QUEUED by the reconciler, say) is simply tried again; 5 rounds is far
        # more than any real interleaving needs.
        for _ in range(5):
            status = (
                await self._s.execute(
                    update(Run)
                    .where(
                        Run.id == run_id,
                        Run.status.in_([s.value for s in CANCELLABLE_STATUSES]),
                        Run.cancel_requested_at.is_(None),
                    )
                    .values(**cancel_values(reason))
                    .returning(Run.status)
                    .execution_options(synchronize_session=False)
                )
            ).scalar_one_or_none()
            if status is not None:
                if status == RunStatus.CANCELLED:
                    return CancelDisposition.CANCELLED
                return CancelDisposition.FLAGGED
            # 0 rows: re-read and answer by the state that actually won.
            row = (
                await self._s.execute(
                    select(Run.status, Run.cancel_requested_at).where(Run.id == run_id)
                )
            ).one_or_none()
            if row is None:
                return CancelDisposition.NOT_FOUND
            if row.status == RunStatus.CANCELLED or row.cancel_requested_at is not None:
                return CancelDisposition.ALREADY
            if row.status in (RunStatus.COMPLETED, RunStatus.FAILED):
                return CancelDisposition.FINISHED
        raise RuntimeError(f"run {run_id} kept changing state under a cancel request")

    async def mark_dispatched(self, run_ids: Sequence[uuid.UUID]) -> int:
        """Stamp `dispatched_at = now()` on runs just published (guarded by
        QUEUED: a run a worker already claimed needs no redispatch stamp). The
        reconciler's redispatch (step 4) treats a QUEUED run whose stamp is NULL
        or older than `redispatch_after_s` as lost in transit. Returns the
        number of rows stamped."""
        if not run_ids:
            return 0
        result = await self._s.execute(
            update(Run)
            .where(Run.id.in_(list(run_ids)), Run.status == RunStatus.QUEUED)
            .values(dispatched_at=func.now())
            .execution_options(synchronize_session=False)
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    async def list_attempts(self, run_id: uuid.UUID) -> list[RunAttempt]:
        result = await self._s.execute(
            select(RunAttempt).where(RunAttempt.run_id == run_id).order_by(RunAttempt.attempt_no)
        )
        return list(result.scalars())

    async def claim(self, run_id: uuid.UUID, worker_id: str, *, ttl_s: float = 30.0) -> Run | None:
        """QUEUED -> CLAIMED (R1). Exactly one concurrent caller gets a row
        back. Bumps lease_epoch: this is the fencing token every later write
        for this claim (start, finish, cancel) must present. Seeds the lease
        (lease_expires_at, heartbeat_at) so ck_runs_lease holds immediately;
        the lease keeper takes over renewing it from here (ADR-008 D)."""
        check_transition(RunStatus.QUEUED, RunStatus.CLAIMED)
        stmt = (
            update(Run)
            .where(Run.id == run_id, Run.status == RunStatus.QUEUED)
            .values(
                status=RunStatus.CLAIMED,
                worker_id=worker_id,
                claimed_at=func.now(),
                lease_epoch=Run.lease_epoch + 1,
                lease_expires_at=func.now() + timedelta(seconds=ttl_s),
                heartbeat_at=func.now(),
            )
            .returning(Run)
            .execution_options(synchronize_session=False)
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def start_attempt(
        self, run_id: uuid.UUID, worker_id: str
    ) -> tuple[StartOutcome, RunAttempt | None]:
        """CLAIMED -> RUNNING and insert attempt N (= new attempt_count), under
        the run's current lease_epoch (A1, R3). The guard excludes a pending
        cancel (G7/3.4): if 0 rows come back and a cancel is pending at our
        epoch, apply R5 (CLAIMED -> CANCELLED, no attempt) instead of treating
        it as a fence (design §16; 4508 §3.4)."""
        check_transition(RunStatus.CLAIMED, RunStatus.RUNNING)
        stmt = (
            update(Run)
            .where(
                Run.id == run_id,
                Run.status == RunStatus.CLAIMED,
                Run.worker_id == worker_id,
                Run.cancel_requested_at.is_(None),
            )
            .values(
                status=RunStatus.RUNNING,
                attempt_count=Run.attempt_count + 1,
                started_at=func.coalesce(Run.started_at, func.now()),
            )
            .returning(Run.attempt_count, Run.lease_epoch)
            .execution_options(synchronize_session=False)
        )
        row = (await self._s.execute(stmt)).one_or_none()
        if row is None:
            return await self._start_attempt_blocked(run_id, worker_id), None
        attempt_no, epoch = row
        attempt = (
            await self._s.execute(
                insert(RunAttempt)
                .values(
                    id=new_id(),
                    run_id=run_id,
                    attempt_no=attempt_no,
                    worker_id=worker_id,
                    status=AttemptStatus.RUNNING,
                    started_at=func.now(),
                    lease_epoch=epoch,
                )
                .returning(RunAttempt)
            )
        ).scalar_one()
        return StartOutcome.STARTED, attempt

    async def _start_attempt_blocked(self, run_id: uuid.UUID, worker_id: str) -> StartOutcome:
        """R3 matched 0 rows: either a cancel beat us to it (R5), or we no
        longer own the claim (fenced). 'Otherwise it was fenced: ack and
        stop' (design §16)."""
        stmt = (
            update(Run)
            .where(
                Run.id == run_id,
                Run.status == RunStatus.CLAIMED,
                Run.worker_id == worker_id,
                Run.cancel_requested_at.is_not(None),
            )
            .values(
                status=RunStatus.CANCELLED,
                lease_epoch=Run.lease_epoch + 1,
                lease_expires_at=None,
                finished_at=func.now(),
            )
            .returning(Run.id)
            .execution_options(synchronize_session=False)
        )
        cancelled = (await self._s.execute(stmt)).scalar_one_or_none()
        return StartOutcome.CANCELLED if cancelled is not None else StartOutcome.FENCED

    async def finish_attempt(
        self,
        attempt: RunAttempt,
        *,
        outcome: Outcome,
        exit_code: int | None,
        oom_killed: bool,
        stdout: str,
        stderr: str,
        stdout_truncated: bool,
        stderr_truncated: bool,
        duration_ms: int,
        error_message: str | None,
        failure_reason: FailureReason | None = None,
        steps: int | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        cost_usd: Decimal | None = None,
        final_answer: str | None = None,
        sandbox_image_id: str | None = None,
        workspace_capture: WorkspaceCapture | None = None,
    ) -> FinishOutcome:
        """T-finish (Step 3): calls p5_finish, with the target (R7/R8/R10)
        decided by reliability.policy. A failed outcome must carry its
        failure_reason; retryable is always taxonomy.is_retryable(reason) -
        never passed in, so it can't disagree with the reason. A completed
        outcome carries neither.

        `workspace_capture` (O3d, G10) is what became of the workspace snapshot
        and is only ever set for a completed outcome (ck_run_attempts_capture):
        the worker saves the snapshot first, while the attempt is still RUNNING
        (repositories/artifacts.py), and passes the result here.

        'restart_cancel' (a cancel beat us to it) runs p5_cancel and reports
        CANCELLED; 'fenced' triggers the ambiguous-commit re-read (design §6
        rule 6) before being trusted, since the caller may be retrying after a
        lost acknowledgement of its own earlier commit.
        """
        retryable = is_retryable(failure_reason) if failure_reason is not None else None
        self.cancelled_storage_keys = []
        fields = {
            "exit_code": exit_code,
            "oom_killed": oom_killed,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "duration_ms": duration_ms,
            "error_message": error_message,
            "steps": steps,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_usd": cost_usd,
            "final_answer": final_answer,
            "sandbox_image_id": sandbox_image_id,
        }
        next_attempt_at: datetime | None = None
        dead_letter_reason: str | None = None
        if outcome in COMPLETED_OUTCOMES:
            decision = after_completed_attempt(cancel_requested=False)
        else:
            assert workspace_capture is None, "only a completed outcome has a workspace capture"
            assert failure_reason is not None, "a failed outcome must carry a failure_reason"
            run = await self._s.get(Run, attempt.run_id)
            assert run is not None
            decision = after_failed_attempt(
                failure_reason,
                attempt_count=attempt.attempt_no,
                max_attempts=run.max_attempts,
                cancel_requested=False,
                by_reconciler=False,
            )
            if decision.status is RunState.RETRYING:
                delay_s = backoff_delay(attempt.attempt_no)
                next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay_s)
            if decision.dead_letter_reason is not None:
                dead_letter_reason = decision.dead_letter_reason.value
        check_transition(RunStatus.RUNNING, RunStatus(decision.status.value))

        result: str = (
            await self._s.execute(
                _FINISH_SQL,
                {
                    "run": attempt.run_id,
                    "attempt_id": attempt.id,
                    "epoch": attempt.lease_epoch,
                    "target": decision.status.value,
                    "outcome": outcome.value,
                    "failure_reason": failure_reason.value if failure_reason else None,
                    "retryable": retryable,
                    "workspace_capture": workspace_capture.value if workspace_capture else None,
                    "next_attempt_at": next_attempt_at,
                    "dead_letter_reason": dead_letter_reason,
                    "error_message": error_message,
                    "attempt": _attempt_payload(fields),
                },
            )
        ).scalar_one()

        if result == "finished":
            return FinishOutcome.FINISHED
        if result == "restart_cancel":
            cancel_result = (
                await self._s.execute(
                    _CANCEL_SQL,
                    {
                        "run": attempt.run_id,
                        "attempt_id": attempt.id,
                        "epoch": attempt.lease_epoch,
                        "attempt": _attempt_payload(fields),
                    },
                )
            ).one()
            if cancel_result.result == "cancelled":
                self.cancelled_storage_keys = list(cancel_result.storage_keys or [])
                return FinishOutcome.CANCELLED
            log.warning("finish_fenced", attempt_id=str(attempt.id), via="restart_cancel")
            return FinishOutcome.FENCED

        assert result == "fenced"
        return await self._ambiguous_commit_recheck(attempt, outcome, failure_reason)

    async def _ambiguous_commit_recheck(
        self, attempt: RunAttempt, outcome: Outcome, failure_reason: FailureReason | None
    ) -> FinishOutcome:
        """Rule 6 (fencing §6): a 'fenced' result is ambiguous if this call
        could be a retry of our own earlier, acknowledgement-losing commit.
        Read the attempt by id: FINISHED at our epoch with our outcome means
        it was us (report success); otherwise we really were fenced."""
        row = (
            await self._s.execute(
                select(RunAttempt.status, RunAttempt.lease_epoch, RunAttempt.outcome).where(
                    RunAttempt.id == attempt.id
                )
            )
        ).one_or_none()
        if (
            row is not None
            and row.status == AttemptStatus.FINISHED
            and row.lease_epoch == attempt.lease_epoch
            and row.outcome == outcome.value
        ):
            return FinishOutcome.FINISHED
        log.warning(
            "finish_fenced",
            attempt_id=str(attempt.id),
            outcome=outcome.value,
            failure_reason=failure_reason.value if failure_reason else None,
        )
        return FinishOutcome.FENCED

    async def fail_claimed(self, run_id: uuid.UUID, worker_id: str, error_message: str) -> bool:
        """CLAIMED -> FAILED(INTERNAL_ERROR) when a worker can't even start an
        attempt (R6): the run or task was inconsistent at claim time. A
        pending cancel wins instead, so the guard excludes it (G7/3.4); no
        attempt exists either way."""
        check_transition(RunStatus.CLAIMED, RunStatus.FAILED)
        stmt = (
            update(Run)
            .where(
                Run.id == run_id,
                Run.status == RunStatus.CLAIMED,
                Run.worker_id == worker_id,
                Run.cancel_requested_at.is_(None),
            )
            .values(
                status=RunStatus.FAILED,
                outcome=Outcome.INTERNAL_ERROR,
                error_message=error_message,
                failure_reason=FailureReason.INTERNAL_INCONSISTENT,
                dead_letter_reason=DeadLetterReason.NON_RETRYABLE,
                finished_at=func.now(),
                # ck_runs_lease: every non-lease exit from CLAIMED/RUNNING clears the lease.
                lease_expires_at=None,
            )
            .returning(Run.id)
            .execution_options(synchronize_session=False)
        )
        return (await self._s.execute(stmt)).scalar_one_or_none() is not None
