"""EvaluationRepository against Postgres: idempotent inserts, exactly-once
guarded transitions, and terminal shapes that satisfy the 0003 constraints."""

import asyncio
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agenteval_core.evaluation import EVALUATOR_VERSION
from agenteval_core.evaluation.taxonomy import EvaluationStatus, FailureCategory
from agenteval_core.models import AttemptArtifact, AttemptEvaluation, Score
from agenteval_core.repositories.evaluations import EvaluationRepository
from tests.integration.test_evaluation_constraints import attempt

SM = async_sessionmaker[AsyncSession]
HASH = "b" * 64


async def create(
    sm: SM, attempt_id: uuid.UUID, status: EvaluationStatus, **kw: object
) -> uuid.UUID | None:
    async with sm() as s:
        eid = await EvaluationRepository(s).create(attempt_id, status, **kw)
        await s.commit()
    return eid


async def claim(sm: SM, eid: uuid.UUID) -> bool:
    async with sm() as s:
        won = await EvaluationRepository(s).claim(eid, "w-test")
        await s.commit()
    return won is not None


async def finish(sm: SM, eid: uuid.UUID, status: EvaluationStatus, **kw: object) -> bool:
    async with sm() as s:
        epoch = (await s.get(AttemptEvaluation, eid, populate_existing=True)).lease_epoch  # type: ignore[union-attr]
        done = await EvaluationRepository(s).finish(eid, epoch, status, **kw)
        await s.commit()
    return done


async def count(sm: SM, model: type, **where: object) -> int:
    async with sm() as s:
        stmt = select(func.count()).select_from(model)
        for k, v in where.items():
            stmt = stmt.where(getattr(model, k) == v)
        return (await s.execute(stmt)).scalar_one()


async def test_an_attempt_has_exactly_one_evaluation(sessionmaker: SM) -> None:
    a = await attempt(sessionmaker)
    first = await create(sessionmaker, a, EvaluationStatus.PENDING)
    assert first is not None
    assert await create(sessionmaker, a, EvaluationStatus.PENDING) is None
    async with sessionmaker() as s:
        row = await EvaluationRepository(s).get_for_attempt(a)
    assert row is not None and row.id == first
    assert row.evaluator_version == EVALUATOR_VERSION


async def test_concurrent_creates_and_claims_have_one_winner(sessionmaker: SM) -> None:
    a = await attempt(sessionmaker)
    created = await asyncio.gather(
        *(create(sessionmaker, a, EvaluationStatus.PENDING) for _ in range(5))
    )
    [eid] = [e for e in created if e is not None]
    claims = await asyncio.gather(*(claim(sessionmaker, eid) for _ in range(5)))
    assert claims.count(True) == 1
    assert await count(sessionmaker, AttemptEvaluation, attempt_id=a) == 1


async def test_finish_happens_once_and_only_from_running(sessionmaker: SM) -> None:
    eid = await create(sessionmaker, await attempt(sessionmaker), EvaluationStatus.PENDING)
    assert eid is not None
    graded = {
        "task_success": True,
        "failure_category": FailureCategory.PASSED,
        "failure_tags": [],
        "failure_detail": "all required checks passed",
        "duration_ms": 10,
    }
    assert not await finish(sessionmaker, eid, EvaluationStatus.SUCCEEDED, **graded)  # PENDING
    assert await claim(sessionmaker, eid)
    assert await finish(sessionmaker, eid, EvaluationStatus.SUCCEEDED, **graded)
    error = {
        "failure_category": FailureCategory.EVALUATION_ERROR,
        "error_message": "late",
        "error_reason": "check_error",
    }
    assert not await finish(sessionmaker, eid, EvaluationStatus.ERROR, **error)
    async with sessionmaker() as s:
        row = await s.get(AttemptEvaluation, eid)
    assert row is not None
    assert (row.status, row.task_success, row.error_message) == ("SUCCEEDED", True, None)
    assert row.started_at is not None and row.finished_at is not None


@pytest.mark.parametrize(
    ("status", "fields"),
    [
        (EvaluationStatus.SKIPPED, {"failure_category": FailureCategory.PLATFORM_SANDBOX}),
        (EvaluationStatus.SKIPPED, {}),  # no spec, no limit hit: no category
        (
            EvaluationStatus.ERROR,
            {
                "failure_category": FailureCategory.EVALUATION_ERROR,
                "error_message": "x",
                "error_reason": "check_error",
            },
        ),
    ],
    ids=["skipped-platform", "skipped-no-spec", "error"],
)
async def test_terminal_rows_created_finished_satisfy_the_constraints(
    sessionmaker: SM, status: EvaluationStatus, fields: dict[str, object]
) -> None:
    eid = await create(sessionmaker, await attempt(sessionmaker), status, **fields)
    assert eid is not None
    assert not await claim(sessionmaker, eid)  # terminal rows can't be claimed


async def test_invalid_statuses_are_refused_before_sql(sessionmaker: SM) -> None:
    a = await attempt(sessionmaker)
    with pytest.raises(ValueError):
        await create(sessionmaker, a, EvaluationStatus.SUCCEEDED)
    eid = await create(sessionmaker, a, EvaluationStatus.PENDING)
    assert eid is not None
    with pytest.raises(ValueError):
        await finish(sessionmaker, eid, EvaluationStatus.SKIPPED)


async def test_artifact_and_score_inserts_are_idempotent(sessionmaker: SM) -> None:
    a = await attempt(sessionmaker)
    eid = await create(sessionmaker, a, EvaluationStatus.PENDING)
    assert eid is not None
    async with sessionmaker() as s:
        repo = EvaluationRepository(s)
        assert await repo.add_artifact(a, b"snapshot", "c" * 64)
        assert not await repo.add_artifact(a, b"other", "d" * 64)
        score = {
            "scoring_config_hash": HASH,
            "scoring_config": {"version": "v1"},
            "components": {"task_success": "1"},
            "overall": Decimal("0.5"),
        }
        assert await repo.add_score(eid, **score)
        assert not await repo.add_score(eid, **{**score, "overall": Decimal("0.9")})
        assert await repo.add_score(eid, **{**score, "scoring_config_hash": "e" * 64})
        await s.commit()
    async with sessionmaker() as s:
        artifact = await EvaluationRepository(s).get_artifact(a)
        scores = await EvaluationRepository(s).list_scores(eid)
    assert artifact is not None and artifact.content == b"snapshot"
    # The same config never overwrites: both rows keep the first overall.
    assert sorted(sc.overall for sc in scores) == [Decimal("0.5"), Decimal("0.5")]
    assert {sc.scoring_config_hash for sc in scores} == {HASH, "e" * 64}
    assert await count(sessionmaker, AttemptArtifact, attempt_id=a) == 1
    assert await count(sessionmaker, Score, attempt_evaluation_id=eid) == 2
