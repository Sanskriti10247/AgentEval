"""Empty the data tables between scenarios (ADR-011).

Refuses unless AGENTEVAL_BENCH_ALLOW_RESET=1: it deletes every run, task, agent
and evaluation in the database it points at. It never touches the schema
(alembic_version stays) nor the `workers` registry, because the workers keep
running between scenarios and re-register only at startup.

    AGENTEVAL_BENCH_ALLOW_RESET=1 uv run python -m benchmarks.reset
"""

from __future__ import annotations

import asyncio
import os
import sys

import asyncpg

ALLOW_ENV = "AGENTEVAL_BENCH_ALLOW_RESET"

# Children before parents, so the order alone would satisfy the foreign keys
# even without CASCADE (which is deliberately not used).
DATA_TABLES = (
    "scores",
    "attempt_evaluations",
    "attempt_artifacts",
    "trajectory_events",
    "run_replays",
    "run_attempts",
    "runs",
    "evaluations",
    "tasks",
    "datasets",
    "agent_versions",
    "agents",
)


def database_url() -> str:
    """The asyncpg DSN: AGENTEVAL_DATABASE_URL (SQLAlchemy style accepted), else the
    host port from .env.example."""
    url = os.environ.get(
        "AGENTEVAL_DATABASE_URL",
        "postgresql+asyncpg://agenteval:agenteval@localhost:55432/agenteval",
    )
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


def check_allowed() -> None:
    if os.environ.get(ALLOW_ENV) != "1":
        raise SystemExit(f"refusing to reset: this deletes all data; set {ALLOW_ENV}=1 to allow")


async def reset(conn: asyncpg.Connection) -> None:
    check_allowed()
    await conn.execute("TRUNCATE " + ", ".join(DATA_TABLES))


async def _main() -> None:
    check_allowed()
    conn = await asyncpg.connect(database_url())
    try:
        await reset(conn)
    finally:
        await conn.close()
    print(f"truncated {len(DATA_TABLES)} data tables")


if __name__ == "__main__":
    asyncio.run(_main())
    sys.exit(0)
