"""The 0002 CHECK/UNIQUE rules, exercised against the migrated test database."""

import uuid
from typing import Any

import pytest
from sqlalchemy import func, insert, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agenteval_core.models import Agent, AgentVersion, Run, RunAttempt, Task, TrajectoryEvent
from agenteval_core.states import COMPLETED_OUTCOMES, FAILED_OUTCOMES, Outcome, status_for

SM = async_sessionmaker[AsyncSession]
IMAGE = "agenteval-sandbox:py312"


def command_task(**kw: Any) -> dict[str, Any]:
    return {
        "id": uuid.uuid4(),
        "name": "cmd",
        "task_type": "command",
        "image": IMAGE,
        "command": ["true"],
        "files": {},
        "timeout_s": 30,
        "memory_mb": 256,
        "cpu_limit": 1.0,
        **kw,
    }


def agent_task(**kw: Any) -> dict[str, Any]:
    defaults = {"name": "agent", "task_type": "agent", "command": None, "instructions": "fix it"}
    return command_task(**(defaults | kw))


async def insert_ok(sm: SM, model: Any, values: dict[str, Any]) -> None:
    async with sm() as s:
        await s.execute(insert(model).values(**values))
        await s.commit()


async def insert_rejected(sm: SM, model: Any, values: dict[str, Any], constraint: str) -> None:
    async with sm() as s:
        with pytest.raises(IntegrityError, match=constraint):
            await s.execute(insert(model).values(**values))


# -- tasks ---------------------------------------------------------------------------


async def test_agent_task_accepted(sessionmaker: SM) -> None:
    await insert_ok(sessionmaker, Task, agent_task(timeout_s=1800, test_command=["pytest", "-q"]))


@pytest.mark.parametrize(
    ("values", "constraint"),
    [
        (agent_task(timeout_s=1801), "ck_tasks_timeout_s"),
        (command_task(timeout_s=301), "ck_tasks_timeout_s"),  # S1 limit unchanged
        (command_task(command=None), "ck_tasks_command"),
        (agent_task(command=["true"]), "ck_tasks_command"),  # agent decides; no fixed argv
        (agent_task(instructions=None), "ck_tasks_instructions"),
        (command_task(test_command=["pytest"]), "ck_tasks_test_command"),
        (command_task(task_type="reasoning", command=None), "ck_tasks_task_type"),  # not Phase 3
    ],
)
async def test_task_rules(sessionmaker: SM, values: dict[str, Any], constraint: str) -> None:
    await insert_rejected(sessionmaker, Task, values, constraint)


# -- agents / versions -----------------------------------------------------------------


def version(agent_id: uuid.UUID, **kw: Any) -> dict[str, Any]:
    return {
        "id": uuid.uuid4(),
        "agent_id": agent_id,
        "version": 1,
        "model_provider": "fake",
        "model": "fake-model",
        "system_prompt": "You are a careful engineer.",
        "tools": ["read_file"],
        "shell_allowlist": [],
        "limits": {"max_steps": 5},
        "model_settings": {},
        "config_hash": "a" * 64,
        **kw,
    }


async def make_agent(sm: SM) -> uuid.UUID:
    agent_id = uuid.uuid4()
    await insert_ok(sm, Agent, {"id": agent_id, "name": f"agent-{agent_id.hex[:8]}"})
    return agent_id


async def test_agent_names_are_unique(sessionmaker: SM) -> None:
    await insert_ok(sessionmaker, Agent, {"id": uuid.uuid4(), "name": "dup"})
    await insert_rejected(
        sessionmaker, Agent, {"id": uuid.uuid4(), "name": "dup"}, "uq_agents_name"
    )


async def test_agent_version_rules(sessionmaker: SM) -> None:
    agent_id = await make_agent(sessionmaker)
    await insert_ok(sessionmaker, AgentVersion, version(agent_id))
    await insert_rejected(
        sessionmaker,
        AgentVersion,
        version(agent_id, config_hash="b" * 64),
        "uq_agent_versions_agent_id_version",
    )
    await insert_rejected(
        sessionmaker,
        AgentVersion,
        version(agent_id, version=2),  # same config as v1
        "uq_agent_versions_agent_id_config_hash",
    )
    for bad, constraint in [
        ({"version": 0}, "ck_agent_versions_version"),
        ({"model_provider": "openai"}, "ck_agent_versions_model_provider"),  # not implemented
        ({"config_hash": "short"}, "ck_agent_versions_config_hash"),
    ]:
        values = version(agent_id, version=9, config_hash="c" * 64) | bad
        await insert_rejected(sessionmaker, AgentVersion, values, constraint)


# -- runs / attempts: widened outcome checks ------------------------------------------------


async def make_run(sm: SM, agent: bool = True) -> uuid.UUID:
    task = agent_task() if agent else command_task()
    await insert_ok(sm, Task, task)
    version_id = None
    if agent:
        version_id = uuid.uuid4()
        await insert_ok(sm, AgentVersion, version(await make_agent(sm), id=version_id))
    run_id = uuid.uuid4()
    await insert_ok(
        sm,
        Run,
        {
            "id": run_id,
            "task_id": task["id"],
            "agent_version_id": version_id,
            "status": "QUEUED",
            "attempt_count": 0,
        },
    )
    return run_id


@pytest.mark.parametrize("outcome", sorted(Outcome, key=str))
async def test_every_outcome_is_storable_with_its_status_for(
    sessionmaker: SM, outcome: Outcome
) -> None:
    run_id = await make_run(sessionmaker)
    async with sessionmaker() as s:
        await s.execute(
            update(Run).where(Run.id == run_id).values(status=status_for(outcome), outcome=outcome)
        )
        await s.commit()


@pytest.mark.parametrize(
    ("status", "outcome"),
    [("FAILED", o) for o in sorted(COMPLETED_OUTCOMES)]
    + [("COMPLETED", o) for o in sorted(FAILED_OUTCOMES)],
)
async def test_outcome_status_mismatch_rejected(
    sessionmaker: SM, status: str, outcome: Outcome
) -> None:
    run_id = await make_run(sessionmaker)
    async with sessionmaker() as s:
        with pytest.raises(IntegrityError, match="ck_runs_(completed|failed)_outcome"):
            await s.execute(
                update(Run).where(Run.id == run_id).values(status=status, outcome=outcome)
            )


async def make_attempt(sm: SM) -> uuid.UUID:
    run_id = await make_run(sm)
    attempt_id = uuid.uuid4()
    async with sm() as s:
        await s.execute(
            insert(RunAttempt).values(
                id=attempt_id,
                run_id=run_id,
                attempt_no=1,
                worker_id="w1",
                status="RUNNING",
                started_at=func.now(),
            )
        )
        await s.commit()
    return attempt_id


async def test_attempt_aggregates_accept_values_and_reject_negatives(sessionmaker: SM) -> None:
    attempt_id = await make_attempt(sessionmaker)
    async with sessionmaker() as s:
        await s.execute(
            update(RunAttempt)
            .where(RunAttempt.id == attempt_id)
            .values(
                steps=3, tokens_in=12_000, tokens_out=800, cost_usd="0.080000", final_answer="done"
            )
        )
        await s.commit()
    for bad, constraint in [
        ({"steps": -1}, "ck_run_attempts_steps"),
        ({"tokens_in": -1}, "ck_run_attempts_tokens"),
        ({"cost_usd": "-0.01"}, "ck_run_attempts_cost_usd"),
    ]:
        async with sessionmaker() as s:
            with pytest.raises(IntegrityError, match=constraint):
                await s.execute(update(RunAttempt).where(RunAttempt.id == attempt_id).values(**bad))


# -- trajectory_events ---------------------------------------------------------------------


def event(attempt_id: uuid.UUID, seq: int, **kw: Any) -> dict[str, Any]:
    return {
        "id": uuid.uuid4(),
        "attempt_id": attempt_id,
        "seq": seq,
        "step": 1,
        "event_type": "model_call",
        **kw,
    }


async def test_trajectory_events_rules(sessionmaker: SM) -> None:
    attempt_id = await make_attempt(sessionmaker)
    await insert_ok(
        sessionmaker, TrajectoryEvent, event(attempt_id, 0, event_type="run_started", step=0)
    )
    await insert_ok(
        sessionmaker,
        TrajectoryEvent,
        event(
            attempt_id,
            1,
            tokens_in=1000,
            tokens_out=50,
            cache_read_tokens=0,
            cache_write_tokens=900,
            cost_usd="0.006375",
            duration_ms=812,
            output={"stop": "tool_use", "tool_calls": [{"name": "read_file"}]},
        ),
    )
    await insert_ok(
        sessionmaker,
        TrajectoryEvent,
        event(
            attempt_id,
            2,
            event_type="tool_call",
            tool="read_file",
            status="ok",
            input={"path": "main.py"},
            output={"content": "print(1)"},
            duration_ms=71,
        ),
    )
    rejected = [
        (event(attempt_id, 2), "uq_trajectory_events_attempt_id_seq"),  # seq written once
        (event(attempt_id, 3, event_type="thinking"), "ck_trajectory_events_event_type"),
        (
            event(attempt_id, 3, event_type="tool_call", status="ok"),
            "ck_trajectory_events_tool_iff_tool_call",
        ),
        (event(attempt_id, 3, tool="read_file"), "ck_trajectory_events_tool_iff_tool_call"),
        (
            event(attempt_id, 3, event_type="tool_call", tool="shell"),
            "ck_trajectory_events_status_iff_tool_call",
        ),
        (
            event(attempt_id, 3, event_type="tool_call", tool="shell", status="exploded"),
            "ck_trajectory_events_status",
        ),
        (event(attempt_id, -1), "ck_trajectory_events_seq_step"),
        (event(attempt_id, 3, tokens_out=-5), "ck_trajectory_events_tokens"),
        (event(attempt_id, 3, cost_usd="-1"), "ck_trajectory_events_cost_usd"),
        (event(attempt_id, 3, duration_ms=-1), "ck_trajectory_events_duration_ms"),
        (event(uuid.uuid4(), 0), "fk_trajectory_events_attempt_id_run_attempts"),
    ]
    for values, constraint in rejected:
        await insert_rejected(sessionmaker, TrajectoryEvent, values, constraint)
