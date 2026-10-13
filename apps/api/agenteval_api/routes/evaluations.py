"""Evaluation batches. D11: every evaluation result in these responses is a
PublicEvaluation built by public_view(); see services/evaluations.py."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Query, Response, status

from agenteval_api.deps import EvaluationServiceDep
from agenteval_core.schemas.evaluation import (
    Compare,
    EvaluationCreate,
    EvaluationRead,
    ResultPage,
    Summary,
)
from agenteval_core.services.errors import InvalidEvaluation

router = APIRouter(prefix="/evaluations", tags=["evaluations"])


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=EvaluationRead,
    responses={200: {"description": "Replay of an earlier request with the same Idempotency-Key"}},
)
async def create_evaluation(
    body: EvaluationCreate,
    evaluations: EvaluationServiceDep,
    response: Response,
    idempotency_key: Annotated[str | None, Header(min_length=1, max_length=255)] = None,
) -> EvaluationRead:
    """Run an agent version on every task of a FROZEN dataset, `repetitions`
    times. 202 for a new batch, 200 when the key is replayed (runs still
    QUEUED are re-dispatched)."""
    result = await evaluations.create(body, idempotency_key)
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return result.evaluation


# Declared before /{evaluation_id} so "compare" isn't parsed as an id.
@router.get("/compare", response_model=Compare)
async def compare_evaluations(
    evaluations: EvaluationServiceDep,
    ids: Annotated[str, Query(min_length=1, max_length=400)],
) -> Compare:
    """Comma-separated ids; 422 unless they share a dataset and scoring config."""
    try:
        parsed = [uuid.UUID(part) for part in ids.split(",")]
    except ValueError:
        raise InvalidEvaluation("ids must be comma-separated evaluation ids") from None
    return await evaluations.compare(parsed)


@router.get("/{evaluation_id}", response_model=EvaluationRead)
async def get_evaluation(
    evaluation_id: uuid.UUID, evaluations: EvaluationServiceDep
) -> EvaluationRead:
    return await evaluations.get(evaluation_id)


@router.get("/{evaluation_id}/results", response_model=ResultPage)
async def list_results(
    evaluation_id: uuid.UUID,
    evaluations: EvaluationServiceDep,
    after: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> ResultPage:
    """One row per run, ordered by (task, repetition), paged by `after`."""
    return await evaluations.results(evaluation_id, after, limit)


@router.get("/{evaluation_id}/summary", response_model=Summary)
async def get_summary(evaluation_id: uuid.UUID, evaluations: EvaluationServiceDep) -> Summary:
    return await evaluations.summary(evaluation_id)
