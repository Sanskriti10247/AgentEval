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

from sqlalchemy import and_, func, insert, select, tuple_
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
        evaluation), over each run's latest attempt."""
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
            .where(Run.evaluation_id == evaluation_id)
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
