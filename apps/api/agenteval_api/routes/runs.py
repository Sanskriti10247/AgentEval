import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Response, status

from agenteval_api.deps import RunServiceDep
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
    """Queue a run of a task. 202 for a new run, 200 when the key is replayed."""
    result = await runs.submit(body.task_id, idempotency_key)
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return RunRead.model_validate(result.run)


@router.get("/{run_id}", response_model=RunRead)
async def get_run(run_id: uuid.UUID, runs: RunServiceDep) -> RunRead:
    return RunRead.model_validate(await runs.get(run_id))


@router.get("/{run_id}/attempts", response_model=list[RunAttemptRead])
async def list_attempts(run_id: uuid.UUID, runs: RunServiceDep) -> list[RunAttemptRead]:
    return [RunAttemptRead.model_validate(a) for a in await runs.list_attempts(run_id)]
