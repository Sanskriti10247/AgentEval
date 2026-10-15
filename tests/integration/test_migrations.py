"""Migration tests, each on its own throwaway database in the Postgres container.

The drift test is the important one: `alembic check` compares tables/columns
but not CHECK-constraint text, and 0002 is mostly CHECK changes.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agenteval_core.models import Base

ROOT = Path(__file__).resolve().parents[2]


def alembic_cfg(url: str) -> Config:
    cfg = Config(str(ROOT / "packages/core/alembic.ini"))
    cfg.attributes["database_url"] = url
    cfg.attributes["configure_logger"] = False
    return cfg


HEAD = ScriptDirectory.from_config(alembic_cfg("unused")).get_current_head()


async def migrate(url: str, fn: str, rev: str) -> None:
    # env.py calls asyncio.run(); run it off the test's event loop.
    await asyncio.to_thread(getattr(command, fn), alembic_cfg(url), rev)


@pytest.fixture
async def fresh_db(make_db: Callable[[], Awaitable[str]]) -> str:
    return await make_db()


async def schema_snapshot(engine: AsyncEngine) -> dict[str, set[tuple[str, ...]]]:
    queries = {
        "columns": """
            SELECT table_name, column_name, data_type, is_nullable,
                   coalesce(column_default, ''), coalesce(character_maximum_length::text, ''),
                   coalesce(numeric_precision::text, ''), coalesce(numeric_scale::text, '')
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name <> 'alembic_version'""",
        "constraints": """
            SELECT rel.relname, con.conname, con.contype::text, pg_get_constraintdef(con.oid)
            FROM pg_constraint con JOIN pg_class rel ON rel.oid = con.conrelid
            JOIN pg_namespace ns ON ns.oid = rel.relnamespace
            WHERE ns.nspname = 'public' AND rel.relname <> 'alembic_version'""",
        "indexes": """
            SELECT tablename, indexname, indexdef FROM pg_indexes
            WHERE schemaname = 'public' AND tablename <> 'alembic_version'""",
    }
    snap: dict[str, set[tuple[str, ...]]] = {}
    async with engine.connect() as conn:
        for key, sql in queries.items():
            rows = (await conn.execute(text(sql))).all()
            snap[key] = {tuple(str(v) for v in r) for r in rows}
    return snap


async def test_migrations_match_models_exactly(make_db: Callable[[], Awaitable[str]]) -> None:
    migrated_url, models_url = await make_db(), await make_db()
    await migrate(migrated_url, "upgrade", "head")
    migrated = create_async_engine(migrated_url)
    from_models = create_async_engine(models_url)
    try:
        async with from_models.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        a, b = await schema_snapshot(migrated), await schema_snapshot(from_models)
        for key in ("columns", "constraints", "indexes"):
            assert a[key] == b[key], (
                f"{key} drift:\n only in migrations: {sorted(a[key] - b[key])}"
                f"\n only in models: {sorted(b[key] - a[key])}"
            )
        # Sanity: the snapshot actually covers the Phase 3 tables.
        tables = {row[0] for row in a["columns"]}
        assert {"agents", "agent_versions", "trajectory_events"} <= tables
    finally:
        await migrated.dispose()
        await from_models.dispose()


S1_TASK = """INSERT INTO tasks (id, name, task_type, image, command, files, timeout_s, memory_mb,
    cpu_limit) VALUES (:id, 's1', 'command', 'agenteval-sandbox:py312', '["python","main.py"]',
    '{}', 300, 256, 1.0)"""
S1_RUN = """INSERT INTO runs (id, task_id, status, outcome, attempt_count)
    VALUES (:id, :task_id, 'COMPLETED', 'NON_ZERO_EXIT', 1)"""
S1_ATTEMPT = """INSERT INTO run_attempts (id, run_id, attempt_no, worker_id, status, outcome,
    exit_code, oom_killed, stdout, stderr, stdout_truncated, stderr_truncated, started_at)
    VALUES (:id, :run_id, 1, 'w1', 'FINISHED', 'NON_ZERO_EXIT', 3, false, 'hi', '', false,
    false, now())"""


async def test_upgrade_preserves_s1_data(fresh_db: str) -> None:
    ids = {"task": uuid.uuid4(), "run": uuid.uuid4(), "attempt": uuid.uuid4()}
    await migrate(fresh_db, "upgrade", "0001")
    engine = create_async_engine(fresh_db)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(S1_TASK), {"id": ids["task"]})
            await conn.execute(text(S1_RUN), {"id": ids["run"], "task_id": ids["task"]})
            await conn.execute(text(S1_ATTEMPT), {"id": ids["attempt"], "run_id": ids["run"]})
        await migrate(fresh_db, "upgrade", "head")
        async with engine.connect() as conn:
            task = (
                await conn.execute(
                    text("SELECT task_type, command, instructions FROM tasks WHERE id = :id"),
                    {"id": ids["task"]},
                )
            ).one()
            run = (
                await conn.execute(
                    text("SELECT status, outcome, agent_version_id FROM runs WHERE id = :id"),
                    {"id": ids["run"]},
                )
            ).one()
            attempt = (
                await conn.execute(
                    text(
                        "SELECT exit_code, stdout, steps, cost_usd FROM run_attempts WHERE id=:id"
                    ),
                    {"id": ids["attempt"]},
                )
            ).one()
        assert tuple(task) == ("command", ["python", "main.py"], None)
        assert tuple(run) == ("COMPLETED", "NON_ZERO_EXIT", None)
        assert tuple(attempt) == (3, "hi", None, None)
    finally:
        await engine.dispose()


async def _version(engine: AsyncEngine) -> str:
    async with engine.connect() as conn:
        return (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()


async def test_downgrade_refuses_while_agent_data_exists(fresh_db: str) -> None:
    await migrate(fresh_db, "upgrade", "head")
    engine = create_async_engine(fresh_db)
    task_id = uuid.uuid4()
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """INSERT INTO tasks (id, name, task_type, image, instructions, files,
                    timeout_s, memory_mb, cpu_limit) VALUES (:id, 'agent', 'agent',
                    'agenteval-sandbox:py312', 'fix the bug', '{}', 1200, 256, 1.0)"""
                ),
                {"id": task_id},
            )
        with pytest.raises((IntegrityError, DBAPIError)):
            await migrate(fresh_db, "downgrade", "0001")
        # Transactional DDL: the whole multi-step downgrade rolled back, so the
        # schema is still at head.
        assert await _version(engine) == HEAD
        async with engine.connect() as conn:
            kept = await conn.execute(text("SELECT count(*) FROM trajectory_events"))
            assert kept.scalar_one() == 0  # table still exists

        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM tasks WHERE id = :id"), {"id": task_id})
        await migrate(fresh_db, "downgrade", "0001")
        assert await _version(engine) == "0001"
        await migrate(fresh_db, "upgrade", "head")
        assert await _version(engine) == HEAD
    finally:
        await engine.dispose()
