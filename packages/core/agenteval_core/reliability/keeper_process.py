"""The lease keeper's asyncpg backend and its subprocess entry point
(ADR-008 D). `run_keeper_process` is what the worker's `spawn`ed
`multiprocessing.Process` actually runs: its own event loop, its own asyncpg
connection, talking back to the worker only over the duplex pipe.

Kept separate from `keeper.py` so the keeper's message handling and batching
logic can be unit-tested with a fake backend and no asyncpg import at all.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING

import asyncpg

from agenteval_core.logging import configure_logging, get_logger
from agenteval_core.reliability.fencing import LeaseTiming
from agenteval_core.reliability.keeper import LeaseKeeper
from agenteval_core.reliability.keeper_protocol import LeaseKind

if TYPE_CHECKING:
    from multiprocessing.connection import Connection

log = get_logger(__name__)

# (table, status IN-list, whether the table has a cancel_requested_at column
# the worker should react to). The IN-list is parenthesised for the SQL below.
_TABLES: dict[LeaseKind, tuple[str, str, bool]] = {
    "run": ("runs", "('CLAIMED','RUNNING')", True),
    "evaluation": ("attempt_evaluations", "('RUNNING')", False),
}


class AsyncpgLeaseBackend:
    """One batched UPDATE per table per tick, guarded by (id, lease_epoch)
    and status (ADR-008 D)."""

    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def renew(
        self, kind: LeaseKind, items: list[tuple[str, int]], ttl_s: float
    ) -> Mapping[str, str | None]:
        if not items:
            return {}
        table, status_in, has_cancel_flag = _TABLES[kind]
        ids = [uuid.UUID(i) for i, _ in items]
        epochs = [e for _, e in items]
        cancel_col = "cancel_reason" if has_cancel_flag else "NULL::varchar(16)"
        rows = await self._conn.fetch(
            f"UPDATE {table} SET lease_expires_at = now() + make_interval(secs => $1), "
            "heartbeat_at = now() "
            f"WHERE (id, lease_epoch) IN (SELECT * FROM unnest($2::uuid[], $3::bigint[])) "
            f"AND status IN {status_in} "
            f"RETURNING id, {cancel_col} AS cancel_reason",
            float(ttl_s),
            ids,
            epochs,
        )
        return {str(r["id"]): r["cancel_reason"] for r in rows}


def run_keeper_process(
    conn: Connection, database_url: str, timing: LeaseTiming, worker_id: str
) -> None:
    """The subprocess entry point (the `spawn` start method, so no event-loop
    state is inherited - ADR-008 D). Picklable and import-safe at module
    level, as `multiprocessing` requires."""
    configure_logging("INFO", True)
    asyncio.run(_run(conn, database_url, timing, worker_id))


async def _run(conn: Connection, database_url: str, timing: LeaseTiming, worker_id: str) -> None:
    dsn = database_url.replace("postgresql+asyncpg://", "postgresql://")
    app_name = f"agenteval-keeper-{worker_id}"
    pg = await asyncpg.connect(dsn, server_settings={"application_name": app_name})
    try:
        keeper = LeaseKeeper(AsyncpgLeaseBackend(pg), conn, timing)
        await keeper.run()
    finally:
        await pg.close()
