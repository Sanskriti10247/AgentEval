import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Header, Query, Request, Response, status

from agenteval_api import demo
from agenteval_api.auth import DEMO_WRITE, READ
from agenteval_api.deps import EvaluationServiceDep, RunServiceDep
from agenteval_core.schemas.agent import TrajectoryEventRead, TrajectoryPage
from agenteval_core.schemas.evaluation import AttemptEvaluationRead
from agenteval_core.schemas.run import ReplayCreate, RunAttemptRead, RunCreate, RunPage, RunRead
from agenteval_core.states import RunStatus

router = APIRouter(prefix="/runs", tags=["runs"])


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RunRead,
    responses={200: {"description": "Replay of an earlier request with the same Idempotency-Key"}},
    dependencies=[DEMO_WRITE],
)
async def submit_run(
    body: RunCreate,
    runs: RunServiceDep,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str | None, Header(min_length=1, max_length=255)] = None,
) -> RunRead:
    """Queue a run of a task. 202 for a new run, 200 when the key is replayed.

    Agent tasks need `agent_version_id`; command tasks must not have one. 422 when the
    version's provider is not enabled here. In a public demo, 403 for a version that is
    not fake-model and 429 while the demo's cap on queued + running runs is reached.
    """
    if body.agent_version_id is not None:
        await demo.check_agent_version(request, body.agent_version_id)
    await demo.check_capacity(request, 1)
    result = await runs.submit(
        body.task_id, idempotency_key, body.agent_version_id, body.max_attempts
    )
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return RunRead.model_validate(result.run)


@router.get("", response_model=RunPage, dependencies=[READ])
async def list_runs(
    runs: RunServiceDep,
    status: Annotated[RunStatus | None, Query()] = None,
    dead_letter: Annotated[bool | None, Query()] = None,
    evaluation_id: Annotated[uuid.UUID | None, Query()] = None,
    task_id: Annotated[uuid.UUID | None, Query()] = None,
    after: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> RunPage:
    """Runs newest first, keyset-paged on (created_at, id). `dead_letter=true`
    lists FAILED runs that carry a dead-letter reason (the DLQ view). Pass the
    previous page's `next_after` as `after`; it is opaque."""
    page, next_after = await runs.list_runs(
        status=status,
        dead_letter=dead_letter,
        evaluation_id=evaluation_id,
        task_id=task_id,
        after=after,
        limit=limit,
    )
    return RunPage(runs=[RunRead.model_validate(r) for r in page], next_after=next_after)


@router.get("/{run_id}", response_model=RunRead, dependencies=[READ])
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
    dependencies=[DEMO_WRITE],
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


@router.post(
    "/{run_id}/replay",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RunRead,
    responses={
        200: {"description": "Replay of an earlier request with the same Idempotency-Key"},
        409: {"description": "The run is not FAILED, or the key was used for something else"},
        422: {"description": "The run would exceed 10 attempts in total"},
    },
    dependencies=[DEMO_WRITE],
)
async def replay_run(
    run_id: uuid.UUID,
    runs: RunServiceDep,
    request: Request,
    response: Response,
    body: Annotated[ReplayCreate | None, Body()] = None,
    idempotency_key: Annotated[str | None, Header(min_length=1, max_length=255)] = None,
) -> RunRead:
    """Give a FAILED run `additional_attempts` (1-9, default 1) more attempts and
    queue it again. Attempt numbers continue; earlier attempts are kept. 202 when
    replayed, 200 when the same Idempotency-Key repeats (nothing changes). In a public
    demo, 429 while the demo's cap on queued + running runs is reached."""
    await demo.check_capacity(request, 1)
    additional = (body or ReplayCreate()).additional_attempts
    result = await runs.replay(run_id, additional, idempotency_key)
    if not result.accepted:
        response.status_code = status.HTTP_200_OK
    return RunRead.model_validate(result.run)


@router.get("/{run_id}/attempts", response_model=list[RunAttemptRead], dependencies=[READ])
async def list_attempts(run_id: uuid.UUID, runs: RunServiceDep) -> list[RunAttemptRead]:
    return [RunAttemptRead.model_validate(a) for a in await runs.list_attempts(run_id)]


@router.get(
    "/{run_id}/attempts/{attempt_no}/events", response_model=TrajectoryPage, dependencies=[READ]
)
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


@router.get(
    "/{run_id}/attempts/{attempt_no}/evaluation",
    response_model=AttemptEvaluationRead,
    dependencies=[READ],
)
async def get_attempt_evaluation(
    run_id: uuid.UUID, attempt_no: int, evaluations: EvaluationServiceDep
) -> AttemptEvaluationRead:
    """The attempt's evaluation as its public projection only (D11). 404 if
    none is recorded (command tasks, or not yet evaluated)."""
    return await evaluations.attempt_evaluation(run_id, attempt_no)
