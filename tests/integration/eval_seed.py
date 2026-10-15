"""Seed an evaluation batch with one run per outcome shape, as the worker
(Steps 4/5) would have left it, with MARKER planted in every internal column
that must never reach the API (D11).

    task A (spec)    rep 1 passed           rep 2 tests_failed       rep 3 tampering (score 0)
    task B (spec)    rep 1 platform_sandbox rep 2 evaluation ERROR   rep 3 FAILED, no attempt
    task C (no spec) rep 1 SKIPPED/ungraded rep 2 evaluation RUNNING rep 3 still QUEUED
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agenteval_core.evaluation.metrics import TrajectoryMetrics
from agenteval_core.evaluation.scoring import DEFAULT_SCORING_CONFIG, ScoringConfig
from agenteval_core.models import (
    Agent,
    AgentVersion,
    AttemptEvaluation,
    Run,
    RunAttempt,
    Score,
)
from tests.integration.conftest import SANDBOX_IMAGE

SM = async_sessionmaker[AsyncSession]
MARKER = "HIDDEN_MARKER_9b0e"
NOW = datetime.now(UTC)
SPEC = {
    "checks": [{"kind": "python_tests", "name": f"chk_{MARKER}", "expected_tests": 1}],
    "hidden_files": {f"test_{MARKER}.py": f"# {MARKER}\n"},
    "protected_paths": ["test_calc.py"],
}


@dataclass
class Seeded:
    dataset_id: str
    version_id: uuid.UUID
    evaluation_id: str
    tasks: dict[str, str]  # "A" | "B" | "C" -> task id
    runs: dict[str, uuid.UUID]  # "A1" ... "C3" -> run id
    command_run: uuid.UUID | None = None


def agent_task(**kw: Any) -> dict[str, Any]:
    return {
        "name": "fix-add",
        "task_type": "agent",
        "image": SANDBOX_IMAGE,
        "instructions": "Fix calc.py.",
        "files": {"calc.py": "x = 1\n", "test_calc.py": "pass\n"},
        "timeout_s": 120,
        **kw,
    }


async def agent_version(sm: SM) -> uuid.UUID:
    async with sm() as s:
        agent = Agent(name=f"agent-{uuid.uuid4().hex[:8]}")
        s.add(agent)
        await s.flush()
        version = AgentVersion(
            agent_id=agent.id,
            version=1,
            model_provider="fake",
            model="fake-model",
            system_prompt="",
            tools=["read_file"],
            shell_allowlist=[],
            limits={},
            model_settings={},
            config_hash=uuid.uuid4().hex * 2,
        )
        s.add(version)
        await s.commit()
        return version.id


async def frozen_dataset(client: httpx.AsyncClient) -> tuple[str, dict[str, str]]:
    resp = await client.post(
        "/api/v1/datasets", json={"name": f"bench-{uuid.uuid4().hex[:6]}", "version": "1"}
    )
    ds = resp.json()["id"]
    tasks = {}
    for label, spec in (("A", SPEC), ("B", SPEC), ("C", None)):
        body = agent_task(name=f"task-{label}", dataset_id=ds, difficulty="easy", evaluation=spec)
        r = await client.post("/api/v1/tasks", json=body)
        assert r.status_code == 201, r.text
        tasks[label] = r.json()["id"]
    assert (await client.post(f"/api/v1/datasets/{ds}/freeze")).status_code == 200
    return ds, tasks


async def create_evaluation(
    client: httpx.AsyncClient,
    dataset_id: str,
    version_id: uuid.UUID,
    *,
    repetitions: int = 3,
    scoring_config: ScoringConfig | None = None,
    key: str | None = None,
) -> httpx.Response:
    body: dict[str, Any] = {
        "name": "bench-run",
        "dataset_id": dataset_id,
        "agent_version_id": str(version_id),
        "repetitions": repetitions,
    }
    if scoring_config is not None:
        body["scoring_config"] = scoring_config.model_dump(mode="json")
    headers = {"Idempotency-Key": key} if key else {}
    return await client.post("/api/v1/evaluations", json=body, headers=headers)


def check_json(passed: bool) -> dict[str, Any]:
    return {
        "name": f"chk_{MARKER}",
        "kind": "python_tests",
        "required": True,
        "weight": 1.0,
        "passed": passed,
        "score": 1.0 if passed else 0.0,
        "expected": f"expected {MARKER}",
        "actual": f"actual {MARKER}",
        "detail": f"detail {MARKER}",
        "error": None,
        "failure": None if passed else "tests_failed",
        "tests_passed": 1 if passed else 0,
        "tests_total": 1,
        "duration_ms": 7,
    }


METRICS = TrajectoryMetrics(
    model_calls=3, tool_calls=4, tool_error_rate=0.0, ran_tests=True
).model_dump(mode="json")


def components(success: int) -> dict[str, Any]:
    return {
        "task_success": str(success),
        "test_score": str(success),
        "reliability": "1",
        "efficiency": "0.5" if success else "0",
    }


async def finish(
    sm: SM,
    run_id: uuid.UUID,
    *,
    outcome: str,
    status: str,
    duration_ms: int | None,
    evaluation: dict[str, Any] | None,
    score: tuple[dict[str, Any], str] | None = None,
    scoring_hash: str = DEFAULT_SCORING_CONFIG.config_hash(),
) -> None:
    """Write what the executor and the EvaluationRunner would have written."""
    async with sm() as s:
        error = f"{outcome.lower()}: {MARKER}" if status == "FAILED" else None
        await s.execute(
            update(Run)
            .where(Run.id == run_id)
            .values(
                status=status,
                outcome=outcome,
                worker_id="w-seed",
                attempt_count=1 if duration_ms is not None else 0,
                error_message=error,
                finished_at=NOW,
            )
        )
        if duration_ms is not None:
            attempt = RunAttempt(
                run_id=run_id,
                attempt_no=1,
                worker_id="w-seed",
                status="FINISHED",
                outcome=outcome,
                duration_ms=duration_ms,
                steps=4,
                tokens_in=100,
                tokens_out=20,
                cost_usd=Decimal("0.010000"),
                final_answer="done",
                error_message=error,
                started_at=NOW,
                finished_at=NOW,
            )
            s.add(attempt)
            await s.flush()
            if evaluation is not None:
                ev = AttemptEvaluation(
                    attempt_id=attempt.id,
                    evaluator_version="phase4.1",
                    metrics=METRICS,
                    failure_detail=f"failure detail {MARKER}",
                    started_at=NOW,
                    **evaluation,
                )
                s.add(ev)
                await s.flush()
                if score is not None:
                    comps, overall = score
                    s.add(
                        Score(
                            attempt_evaluation_id=ev.id,
                            scoring_config_hash=scoring_hash,
                            scoring_config=DEFAULT_SCORING_CONFIG.model_dump(mode="json"),
                            components=comps,
                            overall=Decimal(overall),
                        )
                    )
        await s.commit()


def graded(category: str, success: bool, *, tampered: bool = False) -> dict[str, Any]:
    return {
        "status": "SUCCEEDED",
        "task_success": success,
        "failure_category": category,
        "failure_tags": ["never_ran_tests"] if not success else [],
        "checks": [check_json(success or tampered)],
        "integrity": {
            "ok": not tampered,
            "tampered": {f"{MARKER}.py": "content changed"} if tampered else {},
            "normalized_paths": [MARKER],
        },
        "duration_ms": 50,
        "finished_at": NOW,
    }


async def run_ids(sm: SM, evaluation_id: str, tasks: dict[str, str]) -> dict[str, uuid.UUID]:
    by_task = {v: k for k, v in tasks.items()}
    async with sm() as s:
        rows = await s.execute(
            select(Run.id, Run.task_id, Run.repetition).where(
                Run.evaluation_id == uuid.UUID(evaluation_id)
            )
        )
        return {f"{by_task[str(t)]}{rep}": rid for rid, t, rep in rows}


async def seed(client: httpx.AsyncClient, sm: SM) -> Seeded:
    ds, tasks = await frozen_dataset(client)
    version = await agent_version(sm)
    resp = await create_evaluation(client, ds, version)
    assert resp.status_code == 202, resp.text
    ev_id = resp.json()["id"]
    runs = await run_ids(sm, ev_id, tasks)

    ok = "AGENT_FINISHED"
    await finish(
        sm,
        runs["A1"],
        outcome=ok,
        status="COMPLETED",
        duration_ms=100,
        evaluation=graded("passed", True),
        score=(components(1), "0.90000"),
    )
    await finish(
        sm,
        runs["A2"],
        outcome=ok,
        status="COMPLETED",
        duration_ms=200,
        evaluation=graded("tests_failed", False),
        score=(components(0), "0.30000"),
    )
    await finish(
        sm,
        runs["A3"],
        outcome=ok,
        status="COMPLETED",
        duration_ms=300,
        evaluation=graded("tampering", False, tampered=True),
        score=(components(0), "0.00000"),
    )
    await finish(
        sm,
        runs["B1"],
        outcome="SANDBOX_ERROR",
        status="FAILED",
        duration_ms=400,
        evaluation={
            "status": "SKIPPED",
            "failure_category": "platform_sandbox",
            "failure_tags": [],
            "duration_ms": 0,
            "finished_at": NOW,
        },
    )
    await finish(
        sm,
        runs["B2"],
        outcome=ok,
        status="COMPLETED",
        duration_ms=500,
        evaluation={
            "status": "ERROR",
            "failure_category": "evaluation_error",
            "failure_tags": [],
            "error_message": f"boom {MARKER}",
            "duration_ms": 9,
            "finished_at": NOW,
        },
    )
    await finish(
        sm, runs["B3"], outcome="INTERNAL_ERROR", status="FAILED", duration_ms=None, evaluation=None
    )
    await finish(
        sm,
        runs["C1"],
        outcome=ok,
        status="COMPLETED",
        duration_ms=600,
        evaluation={
            "status": "SKIPPED",
            "failure_category": None,
            "failure_tags": [],
            "duration_ms": 0,
            "finished_at": NOW,
        },
    )
    await finish(
        sm,
        runs["C2"],
        outcome=ok,
        status="COMPLETED",
        duration_ms=700,
        evaluation={"status": "RUNNING"},
    )
    # C3 stays QUEUED.
    return Seeded(ds, version, ev_id, tasks, runs)
