"""Evaluation batches (the `evaluations` table) and the read model behind
results, summaries and comparisons.

Named "batches" to keep it apart from Step 5's EvaluationRepository, which
owns attempt_evaluations writes. Everything here is read-only except
create() and insert_runs().

D11: result_records() selects only what public_view() and the aggregates
need. It never selects attempt_artifacts.content, failure_detail,
error_message, or integrity details (only integrity.ok).
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, insert, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from agenteval_core.models import (
    AttemptEvaluation,
    Evaluation,
    Run,
    RunAttempt,
    Score,
    Task,
)
from agenteval_core.models.base import new_id
from agenteval_core.reliability.taxonomy import CancelReason
from agenteval_core.repositories.runs import (
    CANCEL_LOCK_TIMEOUT,
    CANCELLABLE_STATUSES,
    cancel_values,
)
from agenteval_core.states import RunStatus


@dataclass(frozen=True)
class ResultRecord:
    """One batch run with its latest attempt, evaluation and batch score."""

    run_id: uuid.UUID
    task_id: uuid.UUID
    repetition: int
    run_status: str
    run_outcome: str | None
    run_error_message: str | None  # legacy platform_category() fallback ("cancelled:")
    attempt_failure_reason: str | None  # Step 3: drives platform_category() when present
    attempt_no: int | None
    duration_ms: int | None
    steps: int | None
    tokens_in: int | None
    tokens_out: int | None
    cost_usd: Decimal | None
    eval_status: str | None
    task_success: bool | None
    failure_category: str | None
    failure_tags: list[str] | None
    checks: list[dict[str, Any]] | None  # internal: only ever fed to public_view()
    metrics: dict[str, Any] | None
    integrity_ok: bool | None
    score_components: dict[str, Any] | None
    score_overall: Decimal | None


@dataclass(frozen=True)
class TaskMeta:
    task_id: uuid.UUID
    name: str
    difficulty: str | None


def batch_run_key(evaluation_id: uuid.UUID, task_id: uuid.UUID, repetition: int) -> str:
    return f"eval:{evaluation_id}:{task_id}:{repetition}"


class BatchRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def create(self, **values: Any) -> Evaluation | None:
        """Insert a batch. None if its idempotency key already exists."""
        stmt = (
            pg_insert(Evaluation)
            .values(id=new_id(), **values)
            .on_conflict_do_nothing(index_elements=["idempotency_key"])
            .returning(Evaluation)
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def get(self, evaluation_id: uuid.UUID) -> Evaluation | None:
        return await self._s.get(Evaluation, evaluation_id, populate_existing=True)

    async def flag_cancel(self, evaluation_id: uuid.UUID) -> bool:
        """Set the batch's own cancel flag (F15). True if this call set it,
        False if it was already set (or the batch is gone: the caller has
        checked existence). One guarded UPDATE; touches only `evaluations`."""
        await self._s.execute(CANCEL_LOCK_TIMEOUT)
        flagged = await self._s.execute(
            update(Evaluation)
            .where(Evaluation.id == evaluation_id, Evaluation.cancel_requested_at.is_(None))
            .values(cancel_requested_at=func.now(), cancel_reason=CancelReason.USER.value)
            .returning(Evaluation.id)
            .execution_options(synchronize_session=False)
        )
        return flagged.scalar_one_or_none() is not None

    async def cancel_runs_chunk(self, evaluation_id: uuid.UUID, limit: int) -> tuple[int, int]:
        """Cancel up to `limit` of the batch's runs that are not terminal and
        not yet flagged, with reason 'batch': R2/R13 straight to CANCELLED,
        CLAIMED/RUNNING flag only (same single guarded UPDATE as the run
        endpoint). Terminal runs are never selected, and the UPDATE re-checks
        the guard, so a run that finished in between simply matches 0 rows.
        Returns (selected, changed); the caller loops until nothing is
        selected (a run that bounced, e.g. RUNNING -> RETRYING, shows up
        again and is cancelled then). Every changed row is flagged, so it
        leaves the selection and the loop terminates."""
        await self._s.execute(CANCEL_LOCK_TIMEOUT)
        guard = (
            Run.evaluation_id == evaluation_id,
            Run.status.in_([s.value for s in CANCELLABLE_STATUSES]),
            Run.cancel_requested_at.is_(None),
        )
        ids = list(
            (await self._s.execute(select(Run.id).where(*guard).order_by(Run.id).limit(limit)))
            .scalars()
            .all()
        )
        if not ids:
            return 0, 0
        changed = await self._s.execute(
            update(Run)
            .where(Run.id.in_(ids), *guard)
            .values(**cancel_values(CancelReason.BATCH))
            .returning(Run.id)
            .execution_options(synchronize_session=False)
        )
        return len(ids), len(changed.all())

    async def get_by_key(self, key: str) -> Evaluation | None:
        result = await self._s.execute(select(Evaluation).where(Evaluation.idempotency_key == key))
        return result.scalar_one_or_none()

    async def insert_runs(
        self, batch: Evaluation, task_ids: list[uuid.UUID], repetitions: int
    ) -> None:
        """Every (task, repetition) cell as a QUEUED run, in one statement.
        UNIQUE(evaluation_id, task_id, repetition) backs exactly-one-per-cell."""
        rows = [
            {
                "id": new_id(),
                "task_id": task_id,
                "agent_version_id": batch.agent_version_id,
                "evaluation_id": batch.id,
                "repetition": rep,
                "status": RunStatus.QUEUED,
                "idempotency_key": batch_run_key(batch.id, task_id, rep),
                "attempt_count": 0,
                "max_attempts": batch.run_max_attempts,  # F6, G1: the batch's pinned budget
            }
            for task_id in task_ids
            for rep in range(1, repetitions + 1)
        ]
        await self._s.execute(insert(Run).values(rows))

    async def queued_run_ids(self, evaluation_id: uuid.UUID) -> list[uuid.UUID]:
        result = await self._s.execute(
            select(Run.id)
            .where(Run.evaluation_id == evaluation_id, Run.status == RunStatus.QUEUED)
            .order_by(Run.task_id, Run.repetition)
        )
        return list(result.scalars())

    async def task_meta(self, task_ids: list[uuid.UUID]) -> dict[uuid.UUID, TaskMeta]:
        if not task_ids:
            return {}
        result = await self._s.execute(
            select(Task.id, Task.name, Task.difficulty).where(Task.id.in_(task_ids))
        )
        return {r.id: TaskMeta(r.id, r.name, r.difficulty) for r in result}

    async def result_records(
        self,
        batch: Evaluation,
        *,
        after: tuple[uuid.UUID, int] | None = None,
        limit: int | None = None,
    ) -> list[ResultRecord]:
        """One SELECT, so a page (or a whole summary) is a consistent snapshot."""
        stmt = (
            select(
                Run.id.label("run_id"),
                Run.task_id,
                Run.repetition,
                Run.status.label("run_status"),
                Run.outcome.label("run_outcome"),
                Run.error_message.label("run_error_message"),
                RunAttempt.failure_reason.label("attempt_failure_reason"),
                RunAttempt.attempt_no,
                RunAttempt.duration_ms,
                RunAttempt.steps,
                RunAttempt.tokens_in,
                RunAttempt.tokens_out,
                RunAttempt.cost_usd,
                AttemptEvaluation.status.label("eval_status"),
                AttemptEvaluation.task_success,
                AttemptEvaluation.failure_category,
                AttemptEvaluation.failure_tags,
                AttemptEvaluation.checks,
                AttemptEvaluation.metrics,
                AttemptEvaluation.integrity["ok"].as_boolean().label("integrity_ok"),
                Score.components.label("score_components"),
                Score.overall.label("score_overall"),
            )
            .select_from(Run)
            .outerjoin(
                RunAttempt,
                and_(RunAttempt.run_id == Run.id, RunAttempt.attempt_no == Run.attempt_count),
            )
            .outerjoin(AttemptEvaluation, AttemptEvaluation.attempt_id == RunAttempt.id)
            .outerjoin(
                Score,
                and_(
                    Score.attempt_evaluation_id == AttemptEvaluation.id,
                    Score.scoring_config_hash == batch.scoring_config_hash,
                ),
            )
            .where(Run.evaluation_id == batch.id)
            .order_by(Run.task_id, Run.repetition)
        )
        if after is not None:
            stmt = stmt.where(tuple_(Run.task_id, Run.repetition) > tuple_(*after))
        if limit is not None:
            stmt = stmt.limit(limit)
        return [ResultRecord(**row._asdict()) for row in await self._s.execute(stmt)]

    async def attempt_record(
        self, run_id: uuid.UUID, attempt_no: int, scoring_config_hash: str
    ) -> tuple[ResultRecord, datetime, datetime | None] | None:
        """The same projection for one attempt (endpoint 11), with the
        evaluation's timestamps. None if the attempt has no evaluation."""
        stmt = (
            select(
                Run.id.label("run_id"),
                Run.task_id,
                func.coalesce(Run.repetition, 0).label("repetition"),
                Run.status.label("run_status"),
                Run.outcome.label("run_outcome"),
                Run.error_message.label("run_error_message"),
                RunAttempt.failure_reason.label("attempt_failure_reason"),
                RunAttempt.attempt_no,
                RunAttempt.duration_ms,
                RunAttempt.steps,
                RunAttempt.tokens_in,
                RunAttempt.tokens_out,
                RunAttempt.cost_usd,
                AttemptEvaluation.status.label("eval_status"),
                AttemptEvaluation.task_success,
                AttemptEvaluation.failure_category,
                AttemptEvaluation.failure_tags,
                AttemptEvaluation.checks,
                AttemptEvaluation.metrics,
                AttemptEvaluation.integrity["ok"].as_boolean().label("integrity_ok"),
                Score.components.label("score_components"),
                Score.overall.label("score_overall"),
                AttemptEvaluation.created_at.label("_created_at"),
                AttemptEvaluation.finished_at.label("_finished_at"),
            )
            .select_from(Run)
            .join(RunAttempt, RunAttempt.run_id == Run.id)
            .join(AttemptEvaluation, AttemptEvaluation.attempt_id == RunAttempt.id)
            .outerjoin(
                Score,
                and_(
                    Score.attempt_evaluation_id == AttemptEvaluation.id,
                    Score.scoring_config_hash == scoring_config_hash,
                ),
            )
            .where(Run.id == run_id, RunAttempt.attempt_no == attempt_no)
        )
        row = (await self._s.execute(stmt)).one_or_none()
        if row is None:
            return None
        values = row._asdict()
        created, finished = values.pop("_created_at"), values.pop("_finished_at")
        return ResultRecord(**values), created, finished

    async def progress_counts(
        self, evaluation_id: uuid.UUID
    ) -> tuple[dict[str, int], dict[str, int], int]:
        """(runs by status, evaluations by status, finished runs awaiting
        evaluation), over each run's latest attempt. Evaluations are counted only
        for COMPLETED/FAILED runs (F13): a RETRYING, replayed or cancelled run's
        latest attempt is history, not its result."""
        latest = and_(RunAttempt.run_id == Run.id, RunAttempt.attempt_no == Run.attempt_count)
        runs = await self._s.execute(
            select(Run.status, func.count())
            .where(Run.evaluation_id == evaluation_id)
            .group_by(Run.status)
        )
        evals = await self._s.execute(
            select(AttemptEvaluation.status, func.count())
            .select_from(Run)
            .join(RunAttempt, latest)
            .join(AttemptEvaluation, AttemptEvaluation.attempt_id == RunAttempt.id)
            .where(
                Run.evaluation_id == evaluation_id,
                Run.status.in_([RunStatus.COMPLETED, RunStatus.FAILED]),
            )
            .group_by(AttemptEvaluation.status)
        )
        awaiting = await self._s.execute(
            select(func.count())
            .select_from(Run)
            .join(RunAttempt, latest)
            .outerjoin(AttemptEvaluation, AttemptEvaluation.attempt_id == RunAttempt.id)
            .where(
                Run.evaluation_id == evaluation_id,
                Run.status.in_([RunStatus.COMPLETED, RunStatus.FAILED]),
                (AttemptEvaluation.id.is_(None))
                | AttemptEvaluation.status.in_(["PENDING", "RUNNING"]),
            )
        )
        return (
            {str(s): int(n) for s, n in runs.all()},
            {str(s): int(n) for s, n in evals.all()},
            int(awaiting.scalar_one()),
        )
