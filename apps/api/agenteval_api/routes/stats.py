from typing import Annotated

from fastapi import APIRouter, Query

from agenteval_api.auth import READ
from agenteval_api.deps import ReadServiceDep
from agenteval_core.schemas.read import StatsActivity, StatsOverview

router = APIRouter(prefix="/stats", tags=["stats"])


@router.get("/overview", response_model=StatsOverview, dependencies=[READ])
async def overview(
    read: ReadServiceDep,
    window_h: Annotated[int, Query(ge=1, le=720)] = 24,
) -> StatsOverview:
    """Dashboard KPIs. Counts and queue depth are current; success rate, run
    latency and usage cover the last `window_h` hours (1-720, default 24)."""
    return await read.overview(window_h)


@router.get("/activity", response_model=StatsActivity, dependencies=[READ])
async def activity(
    read: ReadServiceDep,
    window_h: Annotated[int, Query(ge=1, le=720)] = 24,
) -> StatsActivity:
    """Finished runs per time bucket (hourly up to 48 h, six-hourly up to 7 days, daily
    beyond), oldest first and zero-filled, and the evaluations created in the window."""
    return await read.activity(window_h)
