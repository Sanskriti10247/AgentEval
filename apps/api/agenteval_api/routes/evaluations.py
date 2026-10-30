"""Evaluation batches. D11: every evaluation result in these responses is a
PublicEvaluation built by public_view(); see services/evaluations.py."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Query, Request, Response, status

from agenteval_api import demo
from agenteval_api.auth import DEMO_WRITE, READ
from agenteval_api.deps import EvaluationServiceDep, ReadServiceDep
from agenteval_core.schemas.evaluation import (
    Compare,
    EvaluationCreate,
    EvaluationEstimate,
    EvaluationEstimateRequest,
    EvaluationPage,
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
    dependencies=[DEMO_WRITE],
)
async def create_evaluation(
    body: EvaluationCreate,
    evaluations: EvaluationServiceDep,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str | None, Header(min_length=1, max_length=255)] = None,
) -> EvaluationRead:
    """Run an agent version on every task of a FROZEN dataset, `repetitions`
    times. 202 for a new batch, 200 when the key is replayed (runs still
    QUEUED are re-dispatched). 422 when the version's provider is not enabled here.
    A real-model version without `max_cost_usd` gets AGENTEVAL_REAL_EVAL_MAX_COST_USD.
    In a public demo, 403 for a version that is not fake-model, 422 above the demo's
    repetition cap and 429 while its cap on queued + running runs is reached."""
    await demo.check_agent_version(request, body.agent_version_id)
    await demo.check_evaluation(request, body.dataset_id, body.repetitions)
    result = await evaluations.create(body, idempotency_key)
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return result.evaluation


# READ, not WRITE: it writes nothing, so a read-only key and a public demo's visitor may
# ask too. Declared before /{evaluation_id} routes, like /compare.
@router.post("/estimate", response_model=EvaluationEstimate, dependencies=[READ])
async def estimate_evaluation(
    body: EvaluationEstimateRequest, evaluations: EvaluationServiceDep
) -> EvaluationEstimate:
    """Upper bounds for an evaluation before launching it: runs, model requests and
    cost (from the version's per-attempt cost cap), and whether this server would run
    it. The same 404 / 409 / 422 as POST /evaluations; nothing is written."""
    return await evaluations.estimate(body)


@router.get("", response_model=EvaluationPage, dependencies=[READ])
async def list_evaluations(
    read: ReadServiceDep,
    agent_version_id: Annotated[uuid.UUID | None, Query()] = None,
    dataset_id: Annotated[uuid.UUID | None, Query()] = None,
    after: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> EvaluationPage:
    """Batches newest first, keyset-paged, each with the same `progress` summary
    as GET /evaluations/{id}."""
    return await read.evaluations(
        agent_version_id=agent_version_id, dataset_id=dataset_id, after=after, limit=limit
    )


# Declared before /{evaluation_id} so "compare" isn't parsed as an id.
@router.get("/compare", response_model=Compare, dependencies=[READ])
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


@router.get("/{evaluation_id}", response_model=EvaluationRead, dependencies=[READ])
async def get_evaluation(
    evaluation_id: uuid.UUID, evaluations: EvaluationServiceDep
) -> EvaluationRead:
    return await evaluations.get(evaluation_id)


@router.post(
    "/{evaluation_id}/cancel",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=EvaluationRead,
    responses={200: {"description": "Already cancelled: nothing changed"}},
    dependencies=[DEMO_WRITE],
)
async def cancel_evaluation(
    evaluation_id: uuid.UUID, evaluations: EvaluationServiceDep, response: Response
) -> EvaluationRead:
    """Cancel the batch and every run of it that has not finished. Finished
    runs, their evaluations and scores are untouched. 202 when this call
    changed something, 200 for a repeat."""
    result = await evaluations.cancel(evaluation_id)
    if not result.accepted:
        response.status_code = status.HTTP_200_OK
    return result.evaluation


@router.get("/{evaluation_id}/results", response_model=ResultPage, dependencies=[READ])
async def list_results(
    evaluation_id: uuid.UUID,
    evaluations: EvaluationServiceDep,
    after: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> ResultPage:
    """One row per run, ordered by (task, repetition), paged by `after`."""
    return await evaluations.results(evaluation_id, after, limit)


@router.get("/{evaluation_id}/summary", response_model=Summary, dependencies=[READ])
async def get_summary(evaluation_id: uuid.UUID, evaluations: EvaluationServiceDep) -> Summary:
    return await evaluations.summary(evaluation_id)
