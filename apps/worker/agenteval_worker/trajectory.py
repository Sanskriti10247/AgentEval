"""TrajectoryWriter: the runtime's EventSink, backed by Postgres.

One short transaction per batch. Transient database errors are retried a
few times with a small backoff (inserts are idempotent on attempt_id+seq,
so a retry after an ambiguous commit is safe). Losing ownership is never
retried: TrajectoryFenced makes the runtime stop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence

from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agenteval_core.agent.events import TrajectoryEventData
from agenteval_core.logging import get_logger
from agenteval_core.models import RunAttempt
from agenteval_core.repositories.trajectory import TrajectoryRepository

log = get_logger(__name__)

TRANSIENT_DB_ERRORS = (OperationalError, InterfaceError, OSError, TimeoutError)
DEFAULT_BACKOFF_S = (0.2, 0.5, 1.0)


class TrajectoryFenced(Exception):
    """This worker no longer owns the attempt; its events must not be written."""


class TrajectoryWriter:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        attempt: RunAttempt,
        *,
        backoff_s: Sequence[float] = DEFAULT_BACKOFF_S,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._sm = sessionmaker
        self._attempt = attempt
        self._backoff_s = tuple(backoff_s)
        self._sleep = sleep

    async def append(self, events: Sequence[TrajectoryEventData]) -> None:
        for try_no in range(len(self._backoff_s) + 1):
            try:
                async with self._sm() as s:
                    if not await TrajectoryRepository(s).append_fenced(self._attempt, events):
                        await s.rollback()
                        raise TrajectoryFenced(
                            f"attempt {self._attempt.id} is no longer owned by "
                            f"{self._attempt.worker_id}"
                        )
                    await s.commit()
                return
            except TRANSIENT_DB_ERRORS as exc:
                if try_no == len(self._backoff_s):
                    raise
                delay = self._backoff_s[try_no]
                log.warning("trajectory_write_retry", try_no=try_no, delay_s=delay, error=str(exc))
                await self._sleep(delay)
