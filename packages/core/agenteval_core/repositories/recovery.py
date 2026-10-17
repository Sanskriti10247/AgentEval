"""The reconciler's SQL (Step 5; PROGRESS.md section 17, ADR-008 H4).

Each method is one recovery step run inside a transaction the *caller* owns
(the repository flushes, never commits). The shape is always the same:

1. `begin_step` sets short timeouts and takes the step's transaction-level
   advisory lock; if another reconciler holds it the step is skipped.
2. A `SELECT ... FOR UPDATE SKIP LOCKED LIMIT 50` picks expired rows. Recovery
   never waits: a row an owner (or a trajectory append, or a finish) holds is
   simply left for the next tick. It never touches artifacts.
3. `reliability.policy` decides each row's transition; this module only
   applies the answer, with a guarded UPDATE that re-checks `status` and
   `lease_epoch`.
4. Every recovery of a leased run bumps `lease_epoch` and clears
   `lease_expires_at` (ck_runs_lease), which fences the previous owner: its
   heartbeat, trajectory append, artifact save and p5_finish all match 0 rows.

What the caller must do after commit (publish, kill containers) is returned,
never done here: external side effects happen strictly after the commit.
"""

import uuid
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import func, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from agenteval_core.logging import get_logger
from agenteval_core.models import Run, RunAttempt
from agenteval_core.reliability.backoff import backoff_delay
from agenteval_core.reliability.policy import (
    RECONCILER_LOCK_NS,
    after_claim_expired,
    after_running_expired,
)
from agenteval_core.reliability.taxonomy import FailureReason, is_retryable
from agenteval_core.reliability.transitions import RunState
from agenteval_core.states import AttemptStatus, Outcome

log = get_logger(__name__)

BATCH_SIZE = 50
REDISPATCH_BATCH_SIZE = 200
STATEMENT_TIMEOUT = "10s"
IDLE_IN_TRANSACTION_TIMEOUT = "5s"

# pg_try_advisory_xact_lock(RECONCILER_LOCK_NS, step). Step 1 has two scans, each
# its own transaction and its own lock. The numbers 2, 5 and 6 are reserved for
# evaluation recovery, the artifact sweeper and evaluation dispatch (Steps 6-7).
STEP_CLAIMED = 11  # 1a
STEP_RUNNING = 12  # 1b
STEP_RETRIES_DUE = 3
STEP_REDISPATCH = 4
STEP_WORKERS = 7


@dataclass(frozen=True)
class RecoveredRun:
    run_id: uuid.UUID
    code: str  # the transition: R4a, R4b, R5, R9, R10, R11, R12


@dataclass
class ClaimRecovery:
    """Step 1a. After commit: publish `to_publish` (the R4a runs)."""

    recovered: list[RecoveredRun] = field(default_factory=list)

    @property
    def to_publish(self) -> list[uuid.UUID]:
        return [r.run_id for r in self.recovered if r.code == "R4a"]


@dataclass
class RunningRecovery:
    """Step 1b. After commit: kill the containers of `attempt_ids`."""

    recovered: list[RecoveredRun] = field(default_factory=list)
    attempt_ids: list[uuid.UUID] = field(default_factory=list)


class RecoveryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def begin_step(self, step: int) -> bool:
        """Must be the transaction's first statements. The timeouts make a
        stalled reconciler release its row locks (a late keeper renewal then
        re-checks the epoch after the lock and is fenced). Returns False when
        another reconciler holds this step's advisory lock: skip the step."""
        await self._s.execute(text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'"))
        await self._s.execute(
            text(f"SET LOCAL idle_in_transaction_session_timeout = '{IDLE_IN_TRANSACTION_TIMEOUT}'")
        )
        got: bool = (
            await self._s.execute(
                text("SELECT pg_try_advisory_xact_lock(:ns, :step)"),
                {"ns": RECONCILER_LOCK_NS, "step": step},
            )
        ).scalar_one()
        return bool(got)

    # -- 1a: CLAIMED runs whose claim lease expired (R4a / R4b / R5) -------------------------

    async def recover_expired_claims(self, limit: int = BATCH_SIZE) -> ClaimRecovery:
        """`runs` only: no attempt exists before R3."""
        rows = (
            await self._s.execute(
                text(
                    "SELECT id, lease_epoch, unstarted_claims,"
                    " cancel_requested_at IS NOT NULL AS cancel"
                    " FROM runs WHERE status = 'CLAIMED' AND lease_expires_at < now()"
                    " ORDER BY lease_expires_at LIMIT :n FOR UPDATE SKIP LOCKED"
                ),
                {"n": limit},
            )
        ).all()
        result = ClaimRecovery()
        for row in rows:
            decision = after_claim_expired(
                unstarted_claims=row.unstarted_claims, cancel_requested=row.cancel
            )
            values: dict[str, object] = {
                "status": decision.status.value,
                "lease_epoch": Run.lease_epoch + 1,
                "lease_expires_at": None,
            }
            if decision.code == "R5":
                values["finished_at"] = func.now()
            elif decision.code == "R4a":
                values.update(
                    unstarted_claims=Run.unstarted_claims + 1, worker_id=None, dispatched_at=None
                )
            else:  # R4b: never an attempt, so only the run records why
                assert decision.dead_letter_reason is not None
                assert decision.failure_reason is not None
                values.update(
                    unstarted_claims=Run.unstarted_claims + 1,
                    outcome=Outcome.INTERNAL_ERROR.value,
                    error_message=(
                        f"the run's claim expired {row.unstarted_claims + 1} times"
                        " before any attempt started"
                    ),
                    failure_reason=decision.failure_reason.value,
                    dead_letter_reason=decision.dead_letter_reason.value,
                    finished_at=func.now(),
                )
            applied = await self._s.execute(
                update(Run)
                .where(
                    Run.id == row.id,
                    Run.status == RunState.CLAIMED.value,
                    Run.lease_epoch == row.lease_epoch,
                )
                .values(**values)
                .returning(Run.id)
                .execution_options(synchronize_session=False)
            )
            if applied.scalar_one_or_none() is not None:
                result.recovered.append(RecoveredRun(row.id, decision.code))
                log.info(
                    "run_recovered", run_id=str(row.id), transition=decision.code, scan="claimed"
                )
        return result

    # -- 1b: RUNNING runs whose lease expired (A3 + R9 / R10 / R11) --------------------------

    async def recover_expired_running(self, limit: int = BATCH_SIZE) -> RunningRecovery:
        """One SELECT ... FOR UPDATE OF r, a SKIP LOCKED over the inner join
        (ADR-008: recovery takes the run and its attempt together and never
        waits). The join's epoch equality is invariant I1: a RUNNING run at
        epoch e has its RUNNING attempt at epoch e."""
        rows = (
            await self._s.execute(
                text(
                    "SELECT r.id AS run_id, a.id AS attempt_id, a.attempt_no,"
                    " r.lease_epoch, r.max_attempts,"
                    " r.cancel_requested_at IS NOT NULL AS cancel"
                    " FROM runs r JOIN run_attempts a"
                    "   ON a.run_id = r.id AND a.status = 'RUNNING'"
                    "  AND a.lease_epoch = r.lease_epoch"
                    " WHERE r.status = 'RUNNING' AND r.lease_expires_at < now()"
                    " ORDER BY r.lease_expires_at LIMIT :n FOR UPDATE OF r, a SKIP LOCKED"
                ),
                {"n": limit},
            )
        ).all()
        result = RunningRecovery()
        for row in rows:
            decision = after_running_expired(
                attempt_count=row.attempt_no,
                max_attempts=row.max_attempts,
                cancel_requested=row.cancel,
            )
            # A3: the attempt is abandoned whatever the run becomes. Guarded like
            # every recovery write: a stale epoch or a changed status matches 0 rows.
            abandoned = await self._s.execute(
                update(RunAttempt)
                .where(
                    RunAttempt.id == row.attempt_id,
                    RunAttempt.status == AttemptStatus.RUNNING.value,
                    RunAttempt.lease_epoch == row.lease_epoch,
                )
                .values(
                    status=AttemptStatus.ABANDONED.value,
                    outcome=Outcome.INTERNAL_ERROR.value,
                    failure_reason=FailureReason.LEASE_EXPIRED.value,
                    retryable=is_retryable(FailureReason.LEASE_EXPIRED),
                    finished_at=func.now(),
                )
                .returning(RunAttempt.id)
                .execution_options(synchronize_session=False)
            )
            if abandoned.scalar_one_or_none() is None:
                continue
            values: dict[str, object] = {
                "status": decision.status.value,
                "lease_epoch": Run.lease_epoch + 1,
                "lease_expires_at": None,
            }
            if decision.code == "R9":
                delay_s = backoff_delay(row.attempt_no)
                values["next_attempt_at"] = func.now() + timedelta(seconds=delay_s)
            elif decision.code == "R10":
                assert decision.dead_letter_reason is not None
                values.update(
                    outcome=Outcome.INTERNAL_ERROR.value,
                    error_message=(f"attempt {row.attempt_no} lost its lease and no retry is left"),
                    dead_letter_reason=decision.dead_letter_reason.value,
                    finished_at=func.now(),
                )
            else:  # R11: the cancel flag stays on the CANCELLED run (G11)
                values["finished_at"] = func.now()
            applied_run = await self._s.execute(
                update(Run)
                .where(
                    Run.id == row.run_id,
                    Run.status == RunState.RUNNING.value,
                    Run.lease_epoch == row.lease_epoch,
                )
                .values(**values)
                .returning(Run.id)
                .execution_options(synchronize_session=False)
            )
            if applied_run.scalar_one_or_none() is None:
                # Cannot happen while we hold both locks; if it ever does, the
                # whole step's transaction must not commit a half-applied pair.
                raise RuntimeError(f"run {row.run_id} changed under its own recovery lock")
            result.recovered.append(RecoveredRun(row.run_id, decision.code))
            result.attempt_ids.append(row.attempt_id)
            log.info(
                "run_recovered",
                run_id=str(row.run_id),
                attempt_id=str(row.attempt_id),
                transition=decision.code,
                scan="running",
            )
        return result

    # -- 3: RETRYING runs whose backoff elapsed (R12) -----------------------------------------

    async def release_due_retries(self, limit: int = BATCH_SIZE) -> list[uuid.UUID]:
        """RETRYING -> QUEUED. No lease is involved, so the epoch is only
        re-checked, not bumped (the next claim bumps it). `dispatched_at` is
        cleared; the caller publishes after commit and stamps it."""
        rows = (
            await self._s.execute(
                text(
                    "SELECT id, lease_epoch FROM runs"
                    " WHERE status = 'RETRYING' AND next_attempt_at <= now()"
                    " ORDER BY next_attempt_at LIMIT :n FOR UPDATE SKIP LOCKED"
                ),
                {"n": limit},
            )
        ).all()
        released: list[uuid.UUID] = []
        for row in rows:
            applied = await self._s.execute(
                update(Run)
                .where(
                    Run.id == row.id,
                    Run.status == RunState.RETRYING.value,
                    Run.lease_epoch == row.lease_epoch,
                )
                .values(status=RunState.QUEUED.value, next_attempt_at=None, dispatched_at=None)
                .returning(Run.id)
                .execution_options(synchronize_session=False)
            )
            if applied.scalar_one_or_none() is not None:
                released.append(row.id)
                log.info("run_recovered", run_id=str(row.id), transition="R12", scan="retries")
        return released

    # -- 4: QUEUED runs that were probably lost in transit ------------------------------------

    async def redispatch_candidates(
        self, older_than_s: float, limit: int = REDISPATCH_BATCH_SIZE
    ) -> list[uuid.UUID]:
        """QUEUED with a stale dispatch stamp, or with none although the run is
        older than the threshold (its XADD failed, or the stamp was lost). A
        brand-new run is unstamped only between its commit and its own XADD,
        so it is left alone instead of being published twice. Read-only: the
        caller publishes after commit and then stamps
        (`RunRepository.mark_dispatched`). A duplicate message from a
        concurrent reconciler is harmless."""
        rows = (
            await self._s.execute(
                text(
                    "SELECT id FROM runs WHERE status = 'QUEUED'"
                    " AND COALESCE(dispatched_at, created_at)"
                    "     < now() - make_interval(secs => :age)"
                    " ORDER BY dispatched_at NULLS FIRST, created_at LIMIT :n"
                ),
                {"age": older_than_s, "n": limit},
            )
        ).all()
        return [row.id for row in rows]

    # -- 7: the worker registry ---------------------------------------------------------------

    async def mark_workers_lost(self, older_than_s: float) -> list[str]:
        """A worker still marked running whose registry heartbeat is older than
        `older_than_s` (3 x lease ttl) is lost (G3). Visibility only: fencing
        never depends on this table."""
        rows = (
            await self._s.execute(
                text(
                    "UPDATE workers SET stopped_at = now(), stop_reason = 'lost'"
                    " WHERE stopped_at IS NULL"
                    "   AND last_heartbeat_at < now() - make_interval(secs => :age)"
                    " RETURNING id"
                ),
                {"age": older_than_s},
            )
        ).all()
        return [row.id for row in rows]

    async def stopped_or_unknown(self, worker_ids: list[str]) -> set[str]:
        """Of `worker_ids`, those that are stopped or not in the registry at all."""
        if not worker_ids:
            return set()
        live = (
            await self._s.execute(
                text("SELECT id FROM workers WHERE id = ANY(:ids) AND stopped_at IS NULL"),
                {"ids": worker_ids},
            )
        ).all()
        return set(worker_ids) - {row.id for row in live}
