"""Worker registry persistence (F12). Visibility only: fencing never depends
on this table, so a failure to register or heartbeat never blocks a worker
from claiming or renewing leases on runs or evaluations."""

from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from agenteval_core.models import Worker


class WorkerRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def register(
        self, worker_id: str, *, hostname: str, pid: int, version: str, concurrency: int
    ) -> Worker:
        """INSERT at startup. A restart with the same id (an operator-supplied
        AGENTEVAL_WORKER_ID, or a PID reused after a crash) simply replaces the
        row: this is visibility bookkeeping, not a fencing token."""
        worker = Worker(
            id=worker_id,
            hostname=hostname,
            pid=pid,
            version=version,
            concurrency=concurrency,
        )
        await self._s.merge(worker)
        return worker

    async def heartbeat(self, worker_id: str) -> None:
        await self._s.execute(
            update(Worker).where(Worker.id == worker_id).values(last_heartbeat_at=func.now())
        )

    async def stop(self, worker_id: str) -> None:
        await self._s.execute(
            update(Worker)
            .where(Worker.id == worker_id)
            .values(stopped_at=func.now(), stop_reason="shutdown")
        )

    async def get(self, worker_id: str) -> Worker | None:
        return (
            await self._s.execute(select(Worker).where(Worker.id == worker_id))
        ).scalar_one_or_none()

    async def last_heartbeat(self, worker_id: str) -> datetime | None:
        row = await self.get(worker_id)
        return row.last_heartbeat_at if row is not None else None
