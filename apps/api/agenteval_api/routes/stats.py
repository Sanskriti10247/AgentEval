from typing import Annotated

from fastapi import APIRouter, Query

from agenteval_api.deps import ReadServiceDep
from agenteval_core.schemas.read import StatsOverview

router = APIRouter(prefix="/stats", tags=["stats"])


@router.get("/overview", response_model=StatsOverview)
async def overview(
    read: ReadServiceDep,
    window_h: Annotated[int, Query(ge=1, le=720)] = 24,
) -> StatsOverview:
    """Dashboard KPIs. Counts and queue depth are current; success rate, run
    latency and usage cover the last `window_h` hours (1-720, default 24)."""
    return await read.overview(window_h)
