from typing import Annotated

from fastapi import APIRouter, Query

from agenteval_api.deps import ReadServiceDep
from agenteval_core.schemas.read import WorkerPage

router = APIRouter(prefix="/workers", tags=["workers"])


@router.get("", response_model=WorkerPage)
async def list_workers(
    read: ReadServiceDep,
    after: Annotated[str | None, Query(max_length=150)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> WorkerPage:
    """Registered workers, live first then newest started first, keyset-paged.
    `alive` = not stopped and a heartbeat within 3 x AGENTEVAL_LEASE_TTL_S."""
    return await read.workers(after, limit)
