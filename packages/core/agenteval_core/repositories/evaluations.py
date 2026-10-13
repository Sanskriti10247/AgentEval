"""Evaluation persistence (Phase 4 step 5). Like RunRepository: every write is
idempotent or a single guarded UPDATE, so duplicate or concurrent evaluators
can't double-write. Never touches runs, run_attempts or trajectory_events:
the attempt outcome is authoritative and independent of evaluation (D1).

    insert (PENDING | SKIPPED | ERROR)   ON CONFLICT (attempt_id) DO NOTHING
    PENDING -> RUNNING                   claim(): exactly one winner
    RUNNING -> SUCCEEDED | ERROR         finish(): exactly once
    scores                               ON CONFLICT (evaluation, config hash) DO NOTHING

Repositories flush but never commit; the runner owns transactions.
"""

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from agenteval_core.evaluation import EVALUATOR_VERSION
from agenteval_core.evaluation.taxonomy import ArtifactKind, EvaluationStatus
from agenteval_core.models import AttemptArtifact, AttemptEvaluation, Score
from agenteval_core.models.base import new_id

_CREATABLE = {EvaluationStatus.PENDING, EvaluationStatus.SKIPPED, EvaluationStatus.ERROR}
_FINISHABLE = {EvaluationStatus.SUCCEEDED, EvaluationStatus.ERROR}


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
        self, attempt_id: uuid.UUID, status: EvaluationStatus, **fields: Any
    ) -> uuid.UUID | None:
        """Insert the attempt's one evaluation. Returns None if it already
        exists: a duplicate evaluator must stop. SKIPPED and ERROR rows are
        created finished."""
        if status not in _CREATABLE:
            raise ValueError(f"an evaluation can't be created as {status}")
        terminal = status is not EvaluationStatus.PENDING
        stmt = (
            pg_insert(AttemptEvaluation)
            .values(
                id=new_id(),
                attempt_id=attempt_id,
                status=status,
                evaluator_version=EVALUATOR_VERSION,
                started_at=func.now() if terminal else None,
                finished_at=func.now() if terminal else None,
                **fields,
            )
            .on_conflict_do_nothing(index_elements=["attempt_id"])
            .returning(AttemptEvaluation.id)
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def claim(self, evaluation_id: uuid.UUID) -> bool:
        """PENDING -> RUNNING. Exactly one concurrent caller gets True."""
        stmt = (
            update(AttemptEvaluation)
            .where(
                AttemptEvaluation.id == evaluation_id,
                AttemptEvaluation.status == EvaluationStatus.PENDING,
            )
            .values(status=EvaluationStatus.RUNNING, started_at=func.now())
            .returning(AttemptEvaluation.id)
            .execution_options(synchronize_session=False)
        )
        return (await self._s.execute(stmt)).scalar_one_or_none() is not None

    async def finish(
        self, evaluation_id: uuid.UUID, status: EvaluationStatus, **fields: Any
    ) -> bool:
        """RUNNING -> SUCCEEDED | ERROR, once. False if it wasn't RUNNING."""
        if status not in _FINISHABLE:
            raise ValueError(f"an evaluation can't finish as {status}")
        stmt = (
            update(AttemptEvaluation)
            .where(
                AttemptEvaluation.id == evaluation_id,
                AttemptEvaluation.status == EvaluationStatus.RUNNING,
            )
            .values(status=status, finished_at=func.now(), **fields)
            .returning(AttemptEvaluation.id)
            .execution_options(synchronize_session=False)
        )
        return (await self._s.execute(stmt)).scalar_one_or_none() is not None

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
