"""Run persistence. Every state change is a single guarded UPDATE
(`WHERE status = <expected> [AND worker_id = ...]`) so concurrent workers can't
make an illegal transition: the loser simply matches 0 rows.

Repositories flush but never commit; services/executors own transactions.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from agenteval_core.models import Run, RunAttempt
from agenteval_core.models.base import new_id
from agenteval_core.states import AttemptStatus, Outcome, RunStatus, check_transition, status_for


class RunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get(self, run_id: uuid.UUID) -> Run | None:
        return await self._s.get(Run, run_id, populate_existing=True)

    async def get_by_idempotency_key(self, key: str) -> Run | None:
        result = await self._s.execute(select(Run).where(Run.idempotency_key == key))
        return result.scalar_one_or_none()

    async def create_queued(self, task_id: uuid.UUID, idempotency_key: str | None) -> Run | None:
        """Insert a QUEUED run. Returns None if the idempotency key already exists.

        ON CONFLICT DO NOTHING makes concurrent duplicate submissions safe
        without relying on catching IntegrityError.
        """
        stmt = (
            pg_insert(Run)
            .values(
                id=new_id(),
                task_id=task_id,
                status=RunStatus.QUEUED,
                idempotency_key=idempotency_key,
                attempt_count=0,
            )
            .on_conflict_do_nothing(index_elements=[Run.idempotency_key])
            .returning(Run)
        )
        result = await self._s.execute(stmt)
        return result.scalar_one_or_none()

    async def list_attempts(self, run_id: uuid.UUID) -> list[RunAttempt]:
        result = await self._s.execute(
            select(RunAttempt).where(RunAttempt.run_id == run_id).order_by(RunAttempt.attempt_no)
        )
        return list(result.scalars())

    async def claim(self, run_id: uuid.UUID, worker_id: str) -> Run | None:
        """QUEUED -> CLAIMED. Exactly one concurrent caller gets a row back."""
        check_transition(RunStatus.QUEUED, RunStatus.CLAIMED)
        stmt = (
            update(Run)
            .where(Run.id == run_id, Run.status == RunStatus.QUEUED)
            .values(status=RunStatus.CLAIMED, worker_id=worker_id, claimed_at=func.now())
            .returning(Run)
            .execution_options(synchronize_session=False)
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def start_attempt(self, run_id: uuid.UUID, worker_id: str) -> RunAttempt | None:
        """CLAIMED -> RUNNING and insert attempt N (= new attempt_count)."""
        check_transition(RunStatus.CLAIMED, RunStatus.RUNNING)
        stmt = (
            update(Run)
            .where(Run.id == run_id, Run.status == RunStatus.CLAIMED, Run.worker_id == worker_id)
            .values(
                status=RunStatus.RUNNING,
                attempt_count=Run.attempt_count + 1,
                started_at=func.coalesce(Run.started_at, func.now()),
            )
            .returning(Run.attempt_count)
            .execution_options(synchronize_session=False)
        )
        attempt_no = (await self._s.execute(stmt)).scalar_one_or_none()
        if attempt_no is None:
            return None
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
                )
                .returning(RunAttempt)
            )
        ).scalar_one()
        return attempt

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
        finished_at: datetime | None = None,
        steps: int | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        cost_usd: Decimal | None = None,
        final_answer: str | None = None,
    ) -> bool:
        """Finish attempt N and move the run RUNNING -> status_for(outcome).

        Fenced on (worker_id, attempt_no): a worker that no longer owns the
        run can't overwrite it. Returns False (caller rolls back) if fenced.
        The agent aggregates default to None (NULL), which is what command
        tasks write.
        """
        final = status_for(outcome)
        check_transition(RunStatus.RUNNING, final)
        done_at = finished_at if finished_at is not None else func.now()
        attempt_rows = await self._s.execute(
            update(RunAttempt)
            .where(RunAttempt.id == attempt.id, RunAttempt.status == AttemptStatus.RUNNING)
            .values(
                status=AttemptStatus.FINISHED,
                outcome=outcome,
                exit_code=exit_code,
                oom_killed=oom_killed,
                stdout=stdout,
                stderr=stderr,
                stdout_truncated=stdout_truncated,
                stderr_truncated=stderr_truncated,
                duration_ms=duration_ms,
                error_message=error_message,
                finished_at=done_at,
                steps=steps,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=cost_usd,
                final_answer=final_answer,
            )
            .returning(RunAttempt.id)
            .execution_options(synchronize_session=False)
        )
        if attempt_rows.scalar_one_or_none() is None:
            return False
        run_rows = await self._s.execute(
            update(Run)
            .where(
                Run.id == attempt.run_id,
                Run.status == RunStatus.RUNNING,
                Run.worker_id == attempt.worker_id,
                Run.attempt_count == attempt.attempt_no,
            )
            .values(status=final, outcome=outcome, error_message=error_message, finished_at=done_at)
            .returning(Run.id)
            .execution_options(synchronize_session=False)
        )
        return run_rows.scalar_one_or_none() is not None

    async def fail_claimed(self, run_id: uuid.UUID, worker_id: str, error_message: str) -> bool:
        """CLAIMED -> FAILED(INTERNAL_ERROR) when a worker can't even start an attempt."""
        check_transition(RunStatus.CLAIMED, RunStatus.FAILED)
        stmt = (
            update(Run)
            .where(Run.id == run_id, Run.status == RunStatus.CLAIMED, Run.worker_id == worker_id)
            .values(
                status=RunStatus.FAILED,
                outcome=Outcome.INTERNAL_ERROR,
                error_message=error_message,
                finished_at=func.now(),
            )
            .returning(Run.id)
            .execution_options(synchronize_session=False)
        )
        return (await self._s.execute(stmt)).scalar_one_or_none() is not None
