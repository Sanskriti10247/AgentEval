"""Caps on what a public demo's visitor may launch (AGENTEVAL_PUBLIC_DEMO).

auth.py decides which routes the visitor reaches at all; this module bounds the work those
routes queue, so one visitor can't fill a small single-worker host:

- an evaluation asks for at most `demo_max_repetitions` repetitions (422);
- queued + running runs across the whole deployment stay at or under
  `demo_max_active_runs`, counting the runs the request would add (429 + Retry-After).

The active-run count is read before the insert, so two requests racing for the last slots
can overshoot the cap by one batch. That is acceptable for a demo; it is not a quota.
Outside a public demo every function here returns at once.
"""

import uuid

from fastapi import Request
from sqlalchemy import func, select

from agenteval_api.errors import ApiError
from agenteval_core.config import Settings
from agenteval_core.models import Run, Task
from agenteval_core.states import RunStatus

ACTIVE = (RunStatus.QUEUED, RunStatus.CLAIMED, RunStatus.RUNNING, RunStatus.RETRYING)
RETRY_AFTER_S = 30


def limits(settings: Settings) -> dict[str, int] | None:
    """What /health reports as `demo_limits`; None when this is not a public demo.
    `max_active_runs` caps queued + running runs; `concurrent_runs` is how many of them
    execute at once (the worker's concurrency, which the demo image sets for both
    processes from the same environment)."""
    if not is_public_demo(settings):
        return None
    return {
        "max_repetitions": settings.demo_max_repetitions,
        "max_active_runs": settings.demo_max_active_runs,
        "concurrent_runs": settings.worker_concurrency,
    }


def is_public_demo(settings: Settings) -> bool:
    return settings.public_demo and not settings.read_only


async def check_evaluation(request: Request, dataset_id: uuid.UUID, repetitions: int) -> None:
    settings: Settings = request.app.state.settings
    if not is_public_demo(settings):
        return
    if repetitions > settings.demo_max_repetitions:
        raise ApiError(
            422,
            "Invalid Evaluation",
            f"This demo runs at most {settings.demo_max_repetitions} repetitions per evaluation",
        )
    async with request.app.state.sessionmaker() as s:
        tasks = await s.scalar(
            select(func.count()).select_from(Task).where(Task.dataset_id == dataset_id)
        )
    await check_capacity(request, int(tasks or 0) * repetitions)


async def check_capacity(request: Request, new_runs: int) -> None:
    settings: Settings = request.app.state.settings
    if not is_public_demo(settings):
        return
    async with request.app.state.sessionmaker() as s:
        active = int(
            await s.scalar(select(func.count()).select_from(Run).where(Run.status.in_(ACTIVE))) or 0
        )
    cap = settings.demo_max_active_runs
    if active + new_runs > cap:
        raise ApiError(
            429,
            "Too Many Requests",
            f"The demo is busy: {active} runs are queued or running, at most {cap} are "
            f"allowed and this request adds {new_runs}. Try again in a minute",
            headers={"Retry-After": str(RETRY_AFTER_S)},
        )
