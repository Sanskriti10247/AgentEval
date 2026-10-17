"""Evaluation persistence (Phase 4 step 5). Like RunRepository: every write is
idempotent or a single guarded UPDATE, so duplicate or concurrent evaluators
can't double-write. Never touches runs, run_attempts or trajectory_events:
the attempt outcome is authoritative and independent of evaluation (D1).

    insert (PENDING | SKIPPED | ERROR)   ON CONFLICT (attempt_id) DO NOTHING   E1 E2 E3
    PENDING -> RUNNING                   claim(): exactly one winner, epoch + 1    E4
    RUNNING -> SUCCEEDED | ERROR         finish(): once, fenced by epoch           E5 E6 E8b
    RUNNING -> PENDING                   release(): fenced by epoch                E7 E8a
    scores                               ON CONFLICT (evaluation, config hash) DO NOTHING

Repositories flush but never commit; the runner owns transactions.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from agenteval_core.evaluation import EVALUATOR_VERSION
from agenteval_core.evaluation.taxonomy import ArtifactKind, EvaluationStatus
from agenteval_core.models import AttemptArtifact, AttemptEvaluation, Score
from agenteval_core.models.base import new_id
from agenteval_core.reliability.policy import ErrorKind

_CREATABLE = {EvaluationStatus.PENDING, EvaluationStatus.SKIPPED, EvaluationStatus.ERROR}
_FINISHABLE = {EvaluationStatus.SUCCEEDED, EvaluationStatus.ERROR}


@dataclass(frozen=True)
class ClaimedEvaluation:
    """What E4 hands the grader: the fencing epoch it must present on every
    later write, and the budget state the retry decision needs."""

    id: uuid.UUID
    attempt_id: uuid.UUID
    lease_epoch: int
    eval_attempts: int  # including this claim
    max_eval_attempts: int


class EvaluationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add_artifact(
        self,
        attempt_id: uuid.UUID,
        content: bytes,
        sha256: str,
        kind: ArtifactKind = ArtifactKind.WORKSPACE,
    ) -> bool:
        """True if inserted, False if the attempt already has one (idempotent)."""
        stmt = (
            pg_insert(AttemptArtifact)
            .values(
                id=new_id(),
                attempt_id=attempt_id,
                kind=kind,
                content=content,
                size_bytes=len(content),
                sha256=sha256,
            )
            .on_conflict_do_nothing(index_elements=["attempt_id", "kind"])
            .returning(AttemptArtifact.id)
        )
        return (await self._s.execute(stmt)).scalar_one_or_none() is not None

    async def create(
        self,
        attempt_id: uuid.UUID,
        status: EvaluationStatus,
        *,
        max_eval_attempts: int = 1,
        **fields: Any,
    ) -> uuid.UUID | None:
        """Insert the attempt's one evaluation (E1 PENDING, E2 SKIPPED, E3
        ERROR). Returns None if it already exists: a duplicate evaluator must
        stop. SKIPPED and ERROR rows are created finished; an ERROR row is
        always non-retryable and must carry `error_reason` (ck_..._error_reason).
        `max_eval_attempts` is the grading budget pinned on the row now (F9)."""
        if status not in _CREATABLE:
            raise ValueError(f"an evaluation can't be created as {status}")
        terminal = status is not EvaluationStatus.PENDING
        if status is EvaluationStatus.ERROR:
            fields.setdefault("error_kind", ErrorKind.NON_RETRYABLE.value)
        stmt = (
            pg_insert(AttemptEvaluation)
            .values(
                id=new_id(),
                attempt_id=attempt_id,
                status=status,
                evaluator_version=EVALUATOR_VERSION,
                max_eval_attempts=max_eval_attempts,
                started_at=func.now() if terminal else None,
                finished_at=func.now() if terminal else None,
                **fields,
            )
            .on_conflict_do_nothing(index_elements=["attempt_id"])
            .returning(AttemptEvaluation.id)
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def claim(
        self, evaluation_id: uuid.UUID, worker_id: str, *, ttl_s: float = 30.0
    ) -> ClaimedEvaluation | None:
        """E4: PENDING -> RUNNING once its backoff (if any) has elapsed. Exactly
        one concurrent caller gets a result. Bumps `lease_epoch` (the fencing
        token every later write presents), consumes one grading attempt and
        seeds the lease so ck_attempt_evaluations_lease holds at once; the lease
        keeper renews it from here. `started_at` keeps the first claim's time;
        the last error reason, backoff and error kind are cleared (G13)."""
        stmt = (
            update(AttemptEvaluation)
            .where(
                AttemptEvaluation.id == evaluation_id,
                AttemptEvaluation.status == EvaluationStatus.PENDING,
                or_(
                    AttemptEvaluation.next_attempt_at.is_(None),
                    AttemptEvaluation.next_attempt_at <= func.now(),
                ),
            )
            .values(
                status=EvaluationStatus.RUNNING,
                lease_epoch=AttemptEvaluation.lease_epoch + 1,
                eval_attempts=AttemptEvaluation.eval_attempts + 1,
                worker_id=worker_id,
                lease_expires_at=func.now() + timedelta(seconds=ttl_s),
                heartbeat_at=func.now(),
                started_at=func.coalesce(AttemptEvaluation.started_at, func.now()),
                error_reason=None,
                error_kind=None,
                next_attempt_at=None,
            )
            .returning(
                AttemptEvaluation.attempt_id,
                AttemptEvaluation.lease_epoch,
                AttemptEvaluation.eval_attempts,
                AttemptEvaluation.max_eval_attempts,
            )
            .execution_options(synchronize_session=False)
        )
        row = (await self._s.execute(stmt)).one_or_none()
        if row is None:
            return None
        return ClaimedEvaluation(evaluation_id, row[0], row[1], row[2], row[3])

    async def finish(
        self,
        evaluation_id: uuid.UUID,
        epoch: int,
        status: EvaluationStatus,
        *,
        error_reason: str | None = None,
        error_kind: str | None = None,
        **fields: Any,
    ) -> bool:
        """RUNNING -> SUCCEEDED (E5) | ERROR (E6, E8b), fenced by the claim's
        epoch: a grader whose lease was recovered (epoch moved, or the row is
        PENDING again) matches 0 rows and must write nothing else. Clears the
        lease; SUCCEEDED clears `error_reason` (G13), `worker_id` stays."""
        if status not in _FINISHABLE:
            raise ValueError(f"an evaluation can't finish as {status}")
        stmt = (
            update(AttemptEvaluation)
            .where(*self._running_at(evaluation_id, epoch))
            .values(
                status=status,
                finished_at=func.now(),
                lease_expires_at=None,
                error_reason=error_reason,
                error_kind=error_kind,
                **fields,
            )
            .returning(AttemptEvaluation.id)
            .execution_options(synchronize_session=False)
        )
        return (await self._s.execute(stmt)).scalar_one_or_none() is not None

    async def release(
        self,
        evaluation_id: uuid.UUID,
        epoch: int,
        *,
        error_reason: str,
        retry_in_s: float,
    ) -> bool:
        """RUNNING -> PENDING (E7 after a transient failure, E8a at shutdown),
        fenced by epoch. The reason is kept for whoever looks; the lease is
        cleared; the next claim is allowed after `retry_in_s` (the caller decided
        there was budget left, which pending_budget also enforces)."""
        stmt = (
            update(AttemptEvaluation)
            .where(*self._running_at(evaluation_id, epoch))
            .values(
                status=EvaluationStatus.PENDING,
                lease_expires_at=None,
                error_reason=error_reason,
                next_attempt_at=func.now() + timedelta(seconds=retry_in_s),
            )
            .returning(AttemptEvaluation.id)
            .execution_options(synchronize_session=False)
        )
        return (await self._s.execute(stmt)).scalar_one_or_none() is not None

    @staticmethod
    def _running_at(evaluation_id: uuid.UUID, epoch: int) -> tuple[ColumnElement[bool], ...]:
        return (
            AttemptEvaluation.id == evaluation_id,
            AttemptEvaluation.status == EvaluationStatus.RUNNING,
            AttemptEvaluation.lease_epoch == epoch,
        )

    async def mark_dispatched(self, evaluation_ids: Sequence[uuid.UUID]) -> int:
        """Stamp `dispatched_at = now()` on evaluations just published (guarded
        by PENDING: a claimed row needs no stamp). The reconciler's dispatch
        (step 6) reads it to tell a lost message from a fresh one."""
        if not evaluation_ids:
            return 0
        result = await self._s.execute(
            update(AttemptEvaluation)
            .where(
                AttemptEvaluation.id.in_(list(evaluation_ids)),
                AttemptEvaluation.status == EvaluationStatus.PENDING,
            )
            .values(dispatched_at=func.now())
            .execution_options(synchronize_session=False)
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    async def add_score(
        self,
        evaluation_id: uuid.UUID,
        *,
        scoring_config_hash: str,
        scoring_config: dict[str, Any],
        components: dict[str, Any],
        overall: Decimal,
    ) -> bool:
        """Append-only and idempotent per config hash (D8)."""
        stmt = (
            pg_insert(Score)
            .values(
                id=new_id(),
                attempt_evaluation_id=evaluation_id,
                scoring_config_hash=scoring_config_hash,
                scoring_config=scoring_config,
                components=components,
                overall=overall,
            )
            .on_conflict_do_nothing(index_elements=["attempt_evaluation_id", "scoring_config_hash"])
            .returning(Score.id)
        )
        return (await self._s.execute(stmt)).scalar_one_or_none() is not None

    async def get_for_attempt(self, attempt_id: uuid.UUID) -> AttemptEvaluation | None:
        result = await self._s.execute(
            select(AttemptEvaluation)
            .where(AttemptEvaluation.attempt_id == attempt_id)
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def get_artifact(
        self, attempt_id: uuid.UUID, kind: ArtifactKind = ArtifactKind.WORKSPACE
    ) -> AttemptArtifact | None:
        result = await self._s.execute(
            select(AttemptArtifact).where(
                AttemptArtifact.attempt_id == attempt_id, AttemptArtifact.kind == kind
            )
        )
        return result.scalar_one_or_none()

    async def list_scores(self, evaluation_id: uuid.UUID) -> list[Score]:
        result = await self._s.execute(
            select(Score)
            .where(Score.attempt_evaluation_id == evaluation_id)
            .order_by(Score.created_at)
        )
        return list(result.scalars())
