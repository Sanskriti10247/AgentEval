"""Append-only trajectory persistence, fenced to the owning worker.

ADR-008 B: the fence locks only the attempt row, `FOR SHARE`
(`id`, `status = 'RUNNING'`, `lease_epoch`), and never locks or reads `runs`.
Invariant I1 makes this equivalent to the old runs-joined check: while an
attempt is RUNNING at epoch e, its run is RUNNING at the same epoch e (R3/A1
create both together, under one epoch; every exit from RUNNING moves the
epoch or the attempt's status first). Moving the lock off `runs` is what lets
heartbeats and cancellation stay fast regardless of how many trajectory
events are being appended (ADR-008's WAL/lock measurements).

Inserts are idempotent on (attempt_id, seq): a retry after an ambiguous
failure (committed, but the acknowledgement was lost) can't duplicate rows.
"""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from agenteval_core.agent.events import TrajectoryEventData
from agenteval_core.models import RunAttempt, TrajectoryEvent
from agenteval_core.models.base import new_id
from agenteval_core.states import AttemptStatus


class TrajectoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def append_fenced(
        self, attempt: RunAttempt, events: Sequence[TrajectoryEventData]
    ) -> bool:
        """Returns False (and writes nothing) if this worker no longer owns
        the attempt. The caller commits."""
        owned = await self._s.execute(
            select(RunAttempt.id)
            .where(
                RunAttempt.id == attempt.id,
                RunAttempt.status == AttemptStatus.RUNNING,
                RunAttempt.lease_epoch == attempt.lease_epoch,
            )
            .with_for_update(read=True)
        )
        if owned.scalar_one_or_none() is None:
            return False
        if events:
            await self._s.execute(
                pg_insert(TrajectoryEvent)
                .values([_row(attempt, e) for e in events])
                .on_conflict_do_nothing(index_elements=["attempt_id", "seq"])
            )
        return True

    async def list_for_attempt(
        self, attempt_id: Any, *, after_seq: int = -1, limit: int = 1000
    ) -> list[TrajectoryEvent]:
        result = await self._s.execute(
            select(TrajectoryEvent)
            .where(TrajectoryEvent.attempt_id == attempt_id, TrajectoryEvent.seq > after_seq)
            .order_by(TrajectoryEvent.seq)
            .limit(limit)
        )
        return list(result.scalars())


def _row(attempt: RunAttempt, e: TrajectoryEventData) -> dict[str, Any]:
    u = e.usage
    return {
        "id": new_id(),
        "attempt_id": attempt.id,
        "seq": e.seq,
        "step": e.step,
        "event_type": e.event_type.value,
        "tool": e.tool,
        "status": e.status,
        "error_class": e.error_class,
        "input": e.input,
        "output": e.output,
        # Raw provider counts, so cost can be recomputed from the trajectory.
        "tokens_in": u.input_tokens if u else None,
        "tokens_out": u.output_tokens if u else None,
        "cache_read_tokens": u.cache_read_tokens if u else None,
        "cache_write_tokens": u.cache_write_tokens if u else None,
        "cost_usd": e.cost_usd,
        "duration_ms": e.duration_ms,
    }
