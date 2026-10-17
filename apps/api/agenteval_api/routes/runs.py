import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Query, Response, status

from agenteval_api.deps import EvaluationServiceDep, RunServiceDep
from agenteval_core.schemas.agent import TrajectoryEventRead, TrajectoryPage
from agenteval_core.schemas.evaluation import AttemptEvaluationRead
from agenteval_core.schemas.run import RunAttemptRead, RunCreate, RunRead

router = APIRouter(prefix="/runs", tags=["runs"])


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RunRead,
    responses={200: {"description": "Replay of an earlier request with the same Idempotency-Key"}},
)
async def submit_run(
    body: RunCreate,
    runs: RunServiceDep,
    response: Response,
    idempotency_key: Annotated[str | None, Header(min_length=1, max_length=255)] = None,
) -> RunRead:
    """Queue a run of a task. 202 for a new run, 200 when the key is replayed.

    Agent tasks need `agent_version_id`; command tasks must not have one.
    """
    result = await runs.submit(body.task_id, idempotency_key, body.agent_version_id)
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return RunRead.model_validate(result.run)


@router.get("/{run_id}", response_model=RunRead)
async def get_run(run_id: uuid.UUID, runs: RunServiceDep) -> RunRead:
    return RunRead.model_validate(await runs.get(run_id))


@router.post(
    "/{run_id}/cancel",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RunRead,
    responses={
        200: {"description": "Already cancelled or already flagged: nothing changed"},
        409: {"description": "The run already COMPLETED or FAILED; nothing was written"},
    },
)
async def cancel_run(run_id: uuid.UUID, runs: RunServiceDep, response: Response) -> RunRead:
    """Request cancellation. A QUEUED or RETRYING run is CANCELLED at once
    (202); a CLAIMED or RUNNING one is flagged and its worker stops it
    (202, the run is still CLAIMED/RUNNING with `cancel_requested_at` set).
    Repeats return 200; a finished run returns 409."""
    result = await runs.cancel(run_id)
    if not result.accepted:
        response.status_code = status.HTTP_200_OK
    return RunRead.model_validate(result.run)


@router.get("/{run_id}/attempts", response_model=list[RunAttemptRead])
async def list_attempts(run_id: uuid.UUID, runs: RunServiceDep) -> list[RunAttemptRead]:
    return [RunAttemptRead.model_validate(a) for a in await runs.list_attempts(run_id)]


@router.get("/{run_id}/attempts/{attempt_no}/events", response_model=TrajectoryPage)
async def list_events(
    run_id: uuid.UUID,
    attempt_no: int,
    runs: RunServiceDep,
    after_seq: Annotated[int, Query(ge=-1)] = -1,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> TrajectoryPage:
    """An attempt's trajectory in seq order, paged by `after_seq`."""
    events, next_after = await runs.list_events(
        run_id, attempt_no, after_seq=after_seq, limit=limit
    )
    return TrajectoryPage(
        events=[TrajectoryEventRead.model_validate(e) for e in events], next_after_seq=next_after
    )


@router.get("/{run_id}/attempts/{attempt_no}/evaluation", response_model=AttemptEvaluationRead)
async def get_attempt_evaluation(
    run_id: uuid.UUID, attempt_no: int, evaluations: EvaluationServiceDep
) -> AttemptEvaluationRead:
    """The attempt's evaluation as its public projection only (D11). 404 if
    none is recorded (command tasks, or not yet evaluated)."""
    return await evaluations.attempt_evaluation(run_id, attempt_no)
