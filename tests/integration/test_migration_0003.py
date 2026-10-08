"""Migration 0003 (evaluation schema): upgrade with existing S1 + Phase 3 data,
and the downgrade guard. Schema drift is covered by
test_migrations.test_migrations_match_models_exactly (it compares the
migrated head with the models, including every 0003 constraint)."""

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tests.integration.test_migrations import HEAD, migrate

Ids = dict[str, uuid.UUID]

PHASE3_ROWS = [
    # S1 command task, run, attempt
    """INSERT INTO tasks (id, name, task_type, image, command, files, timeout_s, memory_mb,
         cpu_limit) VALUES (:cmd_task, 'cmd', 'command', 'img', '["true"]', '{}', 30, 256, 1.0)""",
    """INSERT INTO runs (id, task_id, status, outcome, attempt_count)
       VALUES (:cmd_run, :cmd_task, 'COMPLETED', 'NON_ZERO_EXIT', 1)""",
    """INSERT INTO run_attempts (id, run_id, attempt_no, worker_id, status, outcome, exit_code,
         oom_killed, stdout, stderr, stdout_truncated, stderr_truncated, started_at)
       VALUES (:cmd_attempt, :cmd_run, 1, 'w1', 'FINISHED', 'NON_ZERO_EXIT', 3, false, 'hi', '',
         false, false, now())""",
    # Phase 3 agent, version, agent task, run, attempt with aggregates, trajectory
    "INSERT INTO agents (id, name) VALUES (:agent, 'fixer')",
    """INSERT INTO agent_versions (id, agent_id, version, model_provider, model, system_prompt,
         tools, shell_allowlist, limits, model_settings, config_hash)
       VALUES (:version, :agent, 1, 'fake', 'fake-model', 's', '["read_file"]', '[]', '{}',
         '{"script": []}', repeat('a', 64))""",
    """INSERT INTO tasks (id, name, task_type, image, instructions, test_command, files,
         timeout_s, memory_mb, cpu_limit)
       VALUES (:agent_task, 'fix', 'agent', 'img', 'fix it', '["python","-m","unittest"]', '{}',
         600, 256, 1.0)""",
    """INSERT INTO runs (id, task_id, agent_version_id, status, outcome, attempt_count)
       VALUES (:agent_run, :agent_task, :version, 'COMPLETED', 'AGENT_FINISHED', 1)""",
    """INSERT INTO run_attempts (id, run_id, attempt_no, worker_id, status, outcome, oom_killed,
         stdout, stderr, stdout_truncated, stderr_truncated, steps, tokens_in, tokens_out,
         cost_usd, final_answer, started_at)
       VALUES (:agent_attempt, :agent_run, 1, 'w1', 'FINISHED', 'AGENT_FINISHED', false, '', '',
         false, false, 4, 400, 80, 0.0008, 'Fixed.', now())""",
    """INSERT INTO trajectory_events (id, attempt_id, seq, step, event_type)
       VALUES (gen_random_uuid(), :agent_attempt, 0, 0, 'run_started')""",
]


async def seed_phase3(engine: AsyncEngine) -> Ids:
    ids = {
        k: uuid.uuid4()
        for k in (
            "cmd_task",
            "cmd_run",
            "cmd_attempt",
            "agent",
            "version",
            "agent_task",
            "agent_run",
            "agent_attempt",
        )
    }
    async with engine.begin() as conn:
        for sql in PHASE3_ROWS:
            params = {k: v for k, v in ids.items() if f":{k}" in sql}
            await conn.execute(text(sql), params)
    return ids


async def scalar(engine: AsyncEngine, sql: str, **params: Any) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params)).scalar_one()


async def version_of(engine: AsyncEngine) -> str:
    return str(await scalar(engine, "SELECT version_num FROM alembic_version"))


@pytest.fixture
async def at_0002(make_db: Callable[[], Awaitable[str]]) -> AsyncIterator[tuple[str, AsyncEngine]]:
    url = await make_db()
    await migrate(url, "upgrade", "0002")
    engine = create_async_engine(url)
    yield url, engine
    await engine.dispose()


async def test_upgrade_preserves_s1_and_phase3_data(at_0002: tuple[str, AsyncEngine]) -> None:
    url, engine = at_0002
    ids = await seed_phase3(engine)
    await migrate(url, "upgrade", "0003")
    assert await version_of(engine) == "0003" == HEAD

    async with engine.connect() as conn:
        tasks = (
            await conn.execute(
                text(
                    "SELECT id, task_type, dataset_id, difficulty, evaluation FROM tasks "
                    "ORDER BY task_type"
                )
            )
        ).all()
        runs = (
            await conn.execute(
                text(
                    "SELECT id, status, outcome, agent_version_id, evaluation_id, repetition "
                    "FROM runs"
                )
            )
        ).all()
        attempts = (
            await conn.execute(
                text(
                    "SELECT id, outcome, steps, tokens_in, cost_usd, final_answer, "
                    "sandbox_image_id "
                    "FROM run_attempts ORDER BY outcome"
                )
            )
        ).all()
        events = (await conn.execute(text("SELECT count(*) FROM trajectory_events"))).scalar_one()

    assert [(t.task_type, t.dataset_id, t.difficulty, t.evaluation) for t in tasks] == [
        ("agent", None, None, None),
        ("command", None, None, None),
    ]
    by_id = {r.id: r for r in runs}
    assert by_id[ids["agent_run"]].agent_version_id == ids["version"]
    assert {(r.status, r.outcome, r.evaluation_id, r.repetition) for r in runs} == {
        ("COMPLETED", "NON_ZERO_EXIT", None, None),
        ("COMPLETED", "AGENT_FINISHED", None, None),
    }
    agent_attempt = next(a for a in attempts if a.outcome == "AGENT_FINISHED")
    assert (agent_attempt.steps, agent_attempt.tokens_in, agent_attempt.final_answer) == (
        4,
        400,
        "Fixed.",
    )
    assert all(a.sandbox_image_id is None for a in attempts)
    assert events == 1
    for table in ("datasets", "evaluations", "attempt_artifacts", "attempt_evaluations", "scores"):
        assert await scalar(engine, f"SELECT count(*) FROM {table}") == 0


# One case per kind of Phase 4 data the downgrade must not silently destroy.
PHASE4_DATA: dict[str, list[str]] = {
    "datasets": [
        "INSERT INTO datasets (id, name, version, status) "
        "VALUES (gen_random_uuid(), 'd', '1', 'DRAFT')"
    ],
    "tasks with difficulty": ["UPDATE tasks SET difficulty = 'easy' WHERE id = :agent_task"],
    "tasks with evaluation": [
        "UPDATE tasks SET evaluation = '{\"checks\": []}' WHERE id = :agent_task"
    ],
    "attempts with sandbox_image_id": [
        "UPDATE run_attempts SET sandbox_image_id = 'sha256:x' WHERE id = :agent_attempt"
    ],
    "attempt_artifacts": [
        "INSERT INTO attempt_artifacts (id, attempt_id, kind, content, size_bytes, "
        "sha256) VALUES (gen_random_uuid(), :agent_attempt, 'workspace', "
        "'\\x00'::bytea, 1, repeat('b', 64))"
    ],
    "attempt_evaluations + scores": [
        "INSERT INTO attempt_evaluations (id, attempt_id, status, evaluator_version, task_success, "
        "failure_category, finished_at) VALUES ('11111111-1111-1111-1111-111111111111', "
        ":agent_attempt, 'SUCCEEDED', 'v1', true, 'passed', now())",
        "INSERT INTO scores (id, attempt_evaluation_id, scoring_config_hash, scoring_config, "
        "components, overall) VALUES (gen_random_uuid(), '11111111-1111-1111-1111-111111111111', "
        "repeat('c', 64), '{}', '{}', 1)",
    ],
    "evaluations + batch runs": [
        "INSERT INTO datasets (id, name, version, status, frozen_at) "
        "VALUES ('22222222-2222-2222-2222-222222222222', 'd', '1', 'FROZEN', now())",
        "INSERT INTO evaluations (id, name, dataset_id, agent_version_id, repetitions, "
        "scoring_config, scoring_config_hash) VALUES ('33333333-3333-3333-3333-333333333333', "
        "'e', '22222222-2222-2222-2222-222222222222', :version, 1, '{}', repeat('d', 64))",
        "UPDATE runs SET evaluation_id = '33333333-3333-3333-3333-333333333333', repetition = 1 "
        "WHERE id = :agent_run",
    ],
}


@pytest.mark.parametrize("kind", sorted(PHASE4_DATA))
async def test_downgrade_refuses_while_phase4_data_exists(
    at_0002: tuple[str, AsyncEngine], kind: str
) -> None:
    url, engine = at_0002
    ids = await seed_phase3(engine)
    await migrate(url, "upgrade", "0003")
    async with engine.begin() as conn:
        for sql in PHASE4_DATA[kind]:
            await conn.execute(text(sql), {k: v for k, v in ids.items() if f":{k}" in sql})

    with pytest.raises(RuntimeError, match="refusing to downgrade 0003: Phase 4 data exists"):
        await migrate(url, "downgrade", "0002")

    # Rolled back as a whole: still at 0003, every Phase 4 table and column intact.
    assert await version_of(engine) == "0003"
    assert (
        await scalar(
            engine,
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'tasks' AND column_name = 'evaluation'",
        )
        == 1
    )


async def test_the_refusal_names_what_would_be_lost(at_0002: tuple[str, AsyncEngine]) -> None:
    url, engine = at_0002
    ids = await seed_phase3(engine)
    await migrate(url, "upgrade", "0003")
    async with engine.begin() as conn:
        for sql in PHASE4_DATA["datasets"] + PHASE4_DATA["tasks with difficulty"]:
            await conn.execute(text(sql), {k: v for k, v in ids.items() if f":{k}" in sql})
    with pytest.raises(RuntimeError) as info:
        await migrate(url, "downgrade", "0002")
    assert "datasets: 1" in str(info.value)
    assert "tasks with dataset/difficulty/evaluation: 1" in str(info.value)


async def test_clean_downgrade_keeps_phase3_data_and_reupgrades(
    at_0002: tuple[str, AsyncEngine],
) -> None:
    url, engine = at_0002
    ids = await seed_phase3(engine)
    await migrate(url, "upgrade", "0003")
    await migrate(url, "downgrade", "0002")  # no Phase 4 data: allowed
    assert await version_of(engine) == "0002"
    assert (
        await scalar(
            engine,
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_name IN ('datasets','evaluations','scores')",
        )
        == 0
    )
    assert (
        await scalar(
            engine, "SELECT final_answer FROM run_attempts WHERE id = :id", id=ids["agent_attempt"]
        )
        == "Fixed."
    )
    assert await scalar(engine, "SELECT count(*) FROM trajectory_events") == 1
    await migrate(url, "upgrade", "0003")
    assert await version_of(engine) == "0003"
