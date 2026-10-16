"""Raw-SQL rows for the Phase 5 schema tests (migration 0004 and the T-terminal
functions). Each helper inserts in its own transaction and returns ids."""

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

TASK = """INSERT INTO tasks (id, name, task_type, image, command, files, timeout_s, memory_mb,
    cpu_limit) VALUES (:id, 'cmd', 'command', 'img', '["true"]', '{}', 30, 256, 1.0)"""


async def insert_task(conn: AsyncConnection) -> uuid.UUID:
    task_id = uuid.uuid4()
    await conn.execute(text(TASK), {"id": task_id})
    return task_id


async def running_run(
    engine: AsyncEngine, *, epoch: int = 1, max_attempts: int = 3, cancel: str | None = None
) -> tuple[uuid.UUID, uuid.UUID]:
    """A RUNNING run at `epoch` with its RUNNING attempt 1 (as R1 + R3 + A1 leave it)."""
    run_id, attempt_id = uuid.uuid4(), uuid.uuid4()
    async with engine.begin() as conn:
        task_id = await insert_task(conn)
        await conn.execute(
            text(
                """INSERT INTO runs (id, task_id, status, attempt_count, max_attempts, worker_id,
                     lease_epoch, lease_expires_at, heartbeat_at, claimed_at, started_at,
                     cancel_requested_at, cancel_reason)
                   VALUES (:id, :task, 'RUNNING', 1, :max, 'w1', :e, now() + interval '30 s',
                     now(), now(), now(),
                     CASE WHEN CAST(:c AS text) IS NULL THEN NULL ELSE now() END, :c)"""
            ),
            {"id": run_id, "task": task_id, "e": epoch, "max": max_attempts, "c": cancel},
        )
        await conn.execute(
            text(
                """INSERT INTO run_attempts (id, run_id, attempt_no, worker_id, status, oom_killed,
                     stdout, stderr, stdout_truncated, stderr_truncated, lease_epoch, started_at)
                   VALUES (:id, :run, 1, 'w1', 'RUNNING', false, '', '', false, false, :e, now())"""
            ),
            {"id": attempt_id, "run": run_id, "e": epoch},
        )
    return run_id, attempt_id


async def add_artifact(
    engine: AsyncEngine, attempt_id: uuid.UUID, *, storage_key: str | None = "k/workspace.tar.gz"
) -> None:
    async with engine.begin() as conn:
        if storage_key is None:
            await conn.execute(
                text(
                    """INSERT INTO attempt_artifacts (id, attempt_id, kind, content, size_bytes,
                         sha256) VALUES (gen_random_uuid(), :a, 'workspace', '\\x01'::bytea, 1,
                         repeat('a', 64))"""
                ),
                {"a": attempt_id},
            )
        else:
            await conn.execute(
                text(
                    """INSERT INTO attempt_artifacts (id, attempt_id, kind, storage_key,
                         size_bytes, sha256) VALUES (gen_random_uuid(), :a, 'workspace', :k, 1,
                         repeat('a', 64))"""
                ),
                {"a": attempt_id, "k": storage_key},
            )
