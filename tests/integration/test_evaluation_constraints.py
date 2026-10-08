"""The 0003 CHECK/UNIQUE/FK rules, exercised against the migrated test database."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, insert, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agenteval_core.evaluation.taxonomy import (
    MAX_EVALUATION_SPEC_BYTES,
    MAX_SNAPSHOT_BYTES,
    EvaluationStatus,
    FailureCategory,
)
from agenteval_core.models import (
    Agent,
    AgentVersion,
    AttemptArtifact,
    AttemptEvaluation,
    Dataset,
    Evaluation,
    Run,
    RunAttempt,
    Score,
    Task,
)

SM = async_sessionmaker[AsyncSession]
NOW = datetime.now(UTC)


async def ok(sm: SM, table: Any, /, **values: Any) -> uuid.UUID:
    values.setdefault("id", uuid.uuid4())
    async with sm() as s:
        await s.execute(insert(table).values(**values))
        await s.commit()
    return values["id"]


async def rejected(sm: SM, table: Any, constraint: str, /, **values: Any) -> None:
    values.setdefault("id", uuid.uuid4())
    async with sm() as s:
        with pytest.raises(IntegrityError, match=constraint):
            await s.execute(insert(table).values(**values))


def agent_task(**kw: Any) -> dict[str, Any]:
    return {
        "name": "t",
        "task_type": "agent",
        "image": "img",
        "instructions": "do",
        "files": {},
        "timeout_s": 60,
        "memory_mb": 256,
        "cpu_limit": 1.0,
        **kw,
    }


def command_task(**kw: Any) -> dict[str, Any]:
    return {
        "name": "c",
        "task_type": "command",
        "image": "img",
        "command": ["true"],
        "files": {},
        "timeout_s": 30,
        "memory_mb": 256,
        "cpu_limit": 1.0,
        **kw,
    }


async def version(sm: SM) -> uuid.UUID:
    agent = await ok(sm, Agent, name=f"a-{uuid.uuid4().hex[:8]}")
    return await ok(
        sm,
        AgentVersion,
        agent_id=agent,
        version=1,
        model_provider="fake",
        model="fake-model",
        system_prompt="",
        tools=["read_file"],
        shell_allowlist=[],
        limits={},
        model_settings={},
        config_hash="a" * 64,
    )


async def frozen_dataset(sm: SM) -> uuid.UUID:
    return await ok(
        sm, Dataset, name=f"d-{uuid.uuid4().hex[:6]}", version="1", status="FROZEN", frozen_at=NOW
    )


async def attempt(sm: SM) -> uuid.UUID:
    task = await ok(sm, Task, **agent_task())
    run = await ok(
        sm,
        Run,
        task_id=task,
        agent_version_id=await version(sm),
        status="RUNNING",
        attempt_count=1,
        worker_id="w1",
    )
    return await ok(
        sm,
        RunAttempt,
        run_id=run,
        attempt_no=1,
        worker_id="w1",
        status="RUNNING",
        started_at=func.now(),
    )


# -- datasets -------------------------------------------------------------------------------------


async def test_dataset_rules(sessionmaker: SM) -> None:
    await ok(sessionmaker, Dataset, name="bench", version="1", status="DRAFT")
    await ok(sessionmaker, Dataset, name="bench", version="2", status="FROZEN", frozen_at=NOW)
    await rejected(
        sessionmaker, Dataset, "uq_datasets_name_version", name="bench", version="1", status="DRAFT"
    )
    await rejected(
        sessionmaker, Dataset, "ck_datasets_frozen_at", name="x", version="1", status="FROZEN"
    )  # FROZEN needs frozen_at
    await rejected(
        sessionmaker,
        Dataset,
        "ck_datasets_frozen_at",
        name="y",
        version="1",
        status="DRAFT",
        frozen_at=NOW,
    )
    await rejected(
        sessionmaker, Dataset, "ck_datasets_status", name="z", version="1", status="ARCHIVED"
    )


# -- tasks ----------------------------------------------------------------------------------------


async def test_task_evaluation_fields(sessionmaker: SM) -> None:
    ds = await frozen_dataset(sessionmaker)
    await ok(
        sessionmaker,
        Task,
        **agent_task(dataset_id=ds, difficulty="hard", evaluation={"checks": [{"kind": "file"}]}),
    )
    # D4: command tasks stay as in S1 - no evaluation spec, no dataset.
    await rejected(
        sessionmaker, Task, "ck_tasks_evaluation", **command_task(evaluation={"checks": []})
    )
    await rejected(sessionmaker, Task, "ck_tasks_dataset_id", **command_task(dataset_id=ds))
    await rejected(sessionmaker, Task, "ck_tasks_difficulty", **agent_task(difficulty="extreme"))
    await rejected(
        sessionmaker, Task, "fk_tasks_dataset_id_datasets", **agent_task(dataset_id=uuid.uuid4())
    )
    big = {"hidden": "x" * MAX_EVALUATION_SPEC_BYTES}
    await rejected(sessionmaker, Task, "ck_tasks_evaluation_size", **agent_task(evaluation=big))


async def test_command_tasks_are_unchanged(sessionmaker: SM) -> None:
    await ok(sessionmaker, Task, **command_task())
    await ok(sessionmaker, Task, **command_task(difficulty="easy"))  # metadata only


# -- evaluations and batch runs -------------------------------------------------------------------


async def test_evaluation_rules(sessionmaker: SM) -> None:
    ds, v = await frozen_dataset(sessionmaker), await version(sessionmaker)
    base = {
        "name": "e",
        "dataset_id": ds,
        "agent_version_id": v,
        "scoring_config": {},
        "scoring_config_hash": "h" * 64,
    }
    await ok(sessionmaker, Evaluation, **base, repetitions=10, idempotency_key="k1")
    await rejected(sessionmaker, Evaluation, "ck_evaluations_repetitions", **base, repetitions=0)
    await rejected(sessionmaker, Evaluation, "ck_evaluations_repetitions", **base, repetitions=11)
    await rejected(
        sessionmaker,
        Evaluation,
        "ck_evaluations_scoring_config_hash",
        **{**base, "scoring_config_hash": "short"},
        repetitions=1,
    )
    await rejected(
        sessionmaker,
        Evaluation,
        "uq_evaluations_idempotency_key",
        **base,
        repetitions=1,
        idempotency_key="k1",
    )


async def test_batch_runs_one_per_cell(sessionmaker: SM) -> None:
    ds, v = await frozen_dataset(sessionmaker), await version(sessionmaker)
    ev = await ok(
        sessionmaker,
        Evaluation,
        name="e",
        dataset_id=ds,
        agent_version_id=v,
        repetitions=2,
        scoring_config={},
        scoring_config_hash="h" * 64,
    )
    task = await ok(sessionmaker, Task, **agent_task(dataset_id=ds))
    cell = {
        "task_id": task,
        "agent_version_id": v,
        "status": "QUEUED",
        "attempt_count": 0,
        "evaluation_id": ev,
    }
    await ok(sessionmaker, Run, **cell, repetition=1)
    await ok(sessionmaker, Run, **cell, repetition=2)
    await rejected(
        sessionmaker, Run, "uq_runs_evaluation_id_task_id_repetition", **cell, repetition=1
    )
    await rejected(sessionmaker, Run, "ck_runs_repetition", **cell, repetition=0)
    await rejected(sessionmaker, Run, "ck_runs_evaluation_repetition", **cell, repetition=None)
    await rejected(
        sessionmaker,
        Run,
        "ck_runs_evaluation_repetition",
        **{**cell, "evaluation_id": None},
        repetition=1,
    )
    # Ad-hoc runs (no evaluation) are unaffected and can repeat freely.
    adhoc = {**cell, "evaluation_id": None}
    await ok(sessionmaker, Run, **adhoc)
    await ok(sessionmaker, Run, **adhoc)


# -- artifacts ------------------------------------------------------------------------------------


async def test_artifact_rules(sessionmaker: SM) -> None:
    a = await attempt(sessionmaker)
    blob = b"\x1f\x8b snapshot"
    good = {
        "attempt_id": a,
        "kind": "workspace",
        "content": blob,
        "size_bytes": len(blob),
        "sha256": "f" * 64,
    }
    await ok(sessionmaker, AttemptArtifact, **good)
    await rejected(sessionmaker, AttemptArtifact, "uq_attempt_artifacts_attempt_id_kind", **good)
    other = await attempt(sessionmaker)
    for bad, constraint in [
        ({"kind": "logs"}, "ck_attempt_artifacts_kind"),
        ({"size_bytes": len(blob) + 1}, "ck_attempt_artifacts_size_matches_content"),
        ({"sha256": "short"}, "ck_attempt_artifacts_sha256"),
        ({"size_bytes": MAX_SNAPSHOT_BYTES + 1}, "ck_attempt_artifacts_size_bytes"),
    ]:
        await rejected(
            sessionmaker, AttemptArtifact, constraint, **{**good, "attempt_id": other, **bad}
        )


async def test_snapshot_at_exactly_the_cap_is_accepted(sessionmaker: SM) -> None:
    blob = b"\x00" * MAX_SNAPSHOT_BYTES
    await ok(
        sessionmaker,
        AttemptArtifact,
        attempt_id=await attempt(sessionmaker),
        kind="workspace",
        content=blob,
        size_bytes=len(blob),
        sha256="e" * 64,
    )


# -- attempt evaluations: status invariants -------------------------------------------------------


def evaluation(attempt_id: uuid.UUID, **kw: Any) -> dict[str, Any]:
    return {"attempt_id": attempt_id, "evaluator_version": "v1", **kw}


VALID = [
    {"status": "PENDING"},
    {"status": "RUNNING", "started_at": NOW},
    {
        "status": "SUCCEEDED",
        "task_success": True,
        "failure_category": "passed",
        "finished_at": NOW,
        "checks": [{"name": "hidden_tests", "passed": True}],
        "duration_ms": 1200,
    },
    {
        "status": "SUCCEEDED",
        "task_success": False,
        "failure_category": "tests_failed",
        "failure_tags": ["never_ran_tests"],
        "finished_at": NOW,
    },
    {
        "status": "ERROR",
        "failure_category": "evaluation_error",
        "error_message": "eval container lost",
        "finished_at": NOW,
    },
    {"status": "SKIPPED", "failure_category": "platform_sandbox", "finished_at": NOW},
    {"status": "SKIPPED", "finished_at": NOW},  # agent task without an evaluation spec
]


@pytest.mark.parametrize(
    "values", VALID, ids=lambda v: f"{v['status']}-{v.get('failure_category')}"
)
async def test_valid_evaluation_states(sessionmaker: SM, values: dict[str, Any]) -> None:
    await ok(sessionmaker, AttemptEvaluation, **evaluation(await attempt(sessionmaker), **values))


INVALID = [
    ({"status": "DONE"}, "ck_attempt_evaluations_status"),
    ({"status": "PENDING", "failure_category": "passed"}, "ck_attempt_evaluations_unfinished"),
    ({"status": "RUNNING", "task_success": False}, "ck_attempt_evaluations_unfinished"),
    ({"status": "RUNNING", "finished_at": NOW}, "ck_attempt_evaluations_unfinished"),
    (
        {"status": "SUCCEEDED", "failure_category": "passed", "finished_at": NOW},
        "ck_attempt_evaluations_succeeded",
    ),  # task_success missing
    (
        {"status": "SUCCEEDED", "task_success": True, "failure_category": "passed"},
        "ck_attempt_evaluations_succeeded",
    ),  # finished_at missing
    (
        {
            "status": "SUCCEEDED",
            "task_success": True,
            "failure_category": "tests_failed",
            "finished_at": NOW,
        },
        "ck_attempt_evaluations_passed_iff_success",
    ),
    (
        {
            "status": "SUCCEEDED",
            "task_success": False,
            "failure_category": "passed",
            "finished_at": NOW,
        },
        "ck_attempt_evaluations_passed_iff_success",
    ),
    (
        {
            "status": "ERROR",
            "failure_category": "tests_failed",
            "error_message": "x",
            "finished_at": NOW,
        },
        "ck_attempt_evaluations_error",
    ),
    (
        {"status": "ERROR", "failure_category": "evaluation_error", "finished_at": NOW},
        "ck_attempt_evaluations_error",
    ),  # no error_message
    (
        {"status": "SKIPPED", "task_success": False, "finished_at": NOW},
        "ck_attempt_evaluations_skipped",
    ),
    ({"status": "SKIPPED"}, "ck_attempt_evaluations_skipped"),  # finished_at missing
    (
        {
            "status": "SUCCEEDED",
            "task_success": False,
            "failure_category": "flaky",
            "finished_at": NOW,
        },
        "ck_attempt_evaluations_failure_category",
    ),
    (
        {
            "status": "SUCCEEDED",
            "task_success": True,
            "failure_category": "passed",
            "finished_at": NOW,
            "duration_ms": -1,
        },
        "ck_attempt_evaluations_duration_ms",
    ),
]


@pytest.mark.parametrize(("values", "constraint"), INVALID, ids=[c for _, c in INVALID])
async def test_invalid_evaluation_states(
    sessionmaker: SM, values: dict[str, Any], constraint: str
) -> None:
    await rejected(
        sessionmaker,
        AttemptEvaluation,
        constraint,
        **evaluation(await attempt(sessionmaker), **values),
    )


async def test_one_evaluation_per_attempt(sessionmaker: SM) -> None:
    a = await attempt(sessionmaker)
    await ok(sessionmaker, AttemptEvaluation, **evaluation(a, status="PENDING"))
    await rejected(
        sessionmaker,
        AttemptEvaluation,
        "uq_attempt_evaluations_attempt_id",
        **evaluation(a, status="PENDING"),
    )


async def test_every_category_and_status_in_code_is_storable(sessionmaker: SM) -> None:
    """The CHECK lists are derived from the enums; this guards the reverse."""
    for category in FailureCategory:
        success = category is FailureCategory.PASSED
        await ok(
            sessionmaker,
            AttemptEvaluation,
            **evaluation(
                await attempt(sessionmaker),
                status="SUCCEEDED",
                task_success=success,
                failure_category=category,
                finished_at=NOW,
            ),
        )
    assert {s.value for s in EvaluationStatus} == {
        "PENDING",
        "RUNNING",
        "SUCCEEDED",
        "ERROR",
        "SKIPPED",
    }


# -- scores ---------------------------------------------------------------------------------------


async def test_scores_are_append_only_per_config(sessionmaker: SM) -> None:
    ev = await ok(
        sessionmaker,
        AttemptEvaluation,
        **evaluation(
            await attempt(sessionmaker),
            status="SUCCEEDED",
            task_success=True,
            failure_category="passed",
            finished_at=NOW,
        ),
    )
    row = {
        "attempt_evaluation_id": ev,
        "scoring_config": {"weights": {}},
        "components": {"task_success": 1},
    }
    await ok(sessionmaker, Score, **row, scoring_config_hash="1" * 64, overall=Decimal("0.875"))
    await ok(sessionmaker, Score, **row, scoring_config_hash="2" * 64, overall=Decimal("1"))
    await rejected(
        sessionmaker,
        Score,
        "uq_scores_attempt_evaluation_id_scoring_config_hash",
        **row,
        scoring_config_hash="1" * 64,
        overall=Decimal("0.5"),
    )
    for overall in (Decimal("-0.00001"), Decimal("1.00001")):
        await rejected(
            sessionmaker,
            Score,
            "ck_scores_overall",
            **row,
            scoring_config_hash="3" * 64,
            overall=overall,
        )
    await rejected(
        sessionmaker,
        Score,
        "ck_scores_scoring_config_hash",
        **row,
        scoring_config_hash="x",
        overall=Decimal("0.5"),
    )


async def test_sandbox_image_id_is_optional_metadata(sessionmaker: SM) -> None:
    a = await attempt(sessionmaker)
    async with sessionmaker() as s:
        await s.execute(
            update(RunAttempt)
            .where(RunAttempt.id == a)
            .values(sandbox_image_id="sha256:" + "0" * 64)
        )
        await s.commit()
