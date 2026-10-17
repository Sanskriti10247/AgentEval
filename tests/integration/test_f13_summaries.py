"""Phase 5 Step 9 / F13: batch progress, results and summary describe each run by
its LATEST attempt and its CURRENT status, never by a stale earlier attempt.

A batch of three runs is hand-seeded as the worker would leave it after retries:

    A1  attempt 1 failed (model_overloaded), attempt 2 passed        COMPLETED
    B1  attempt 1 failed (model_overloaded), waiting for attempt 2   RETRYING
    C1  attempt 1 failed, then the user cancelled it                 CANCELLED
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from agenteval_core.evaluation.scoring import DEFAULT_SCORING_CONFIG
from agenteval_core.models import AttemptEvaluation, RunAttempt, Score
from tests.integration.eval_seed import (
    SM,
    agent_version,
    components,
    create_evaluation,
    frozen_dataset,
    graded,
    run_ids,
)

API = "/api/v1"
NOW = datetime.now(UTC)
SKIPPED_MODEL = {
    "status": "SKIPPED",
    "failure_category": "platform_model",
    "failure_tags": [],
    "duration_ms": 0,
    "finished_at": NOW,
}


async def add_attempt(
    sm: SM,
    run_id: uuid.UUID,
    attempt_no: int,
    *,
    failed: bool,
    evaluation: dict[str, Any],
    score: tuple[dict[str, Any], str] | None = None,
) -> None:
    async with sm() as s:
        attempt = RunAttempt(
            run_id=run_id,
            attempt_no=attempt_no,
            worker_id="w-seed",
            status="FINISHED",
            outcome="MODEL_ERROR" if failed else "AGENT_FINISHED",
            duration_ms=100 * attempt_no,
            steps=2,
            tokens_in=10,
            tokens_out=5,
            cost_usd=Decimal("0.010000"),
            started_at=NOW,
            finished_at=NOW,
            failure_reason="model_overloaded" if failed else None,
            retryable=True if failed else None,
        )
        s.add(attempt)
        await s.flush()
        row = AttemptEvaluation(
            attempt_id=attempt.id, evaluator_version="phase4.1", started_at=NOW, **evaluation
        )
        s.add(row)
        await s.flush()
        if score is not None:
            comps, overall = score
            s.add(
                Score(
                    attempt_evaluation_id=row.id,
                    scoring_config_hash=DEFAULT_SCORING_CONFIG.config_hash(),
                    scoring_config=DEFAULT_SCORING_CONFIG.model_dump(mode="json"),
                    components=comps,
                    overall=Decimal(overall),
                )
            )
        await s.commit()


async def set_run(engine: AsyncEngine, run_id: uuid.UUID, sql: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(f"UPDATE runs SET {sql}, max_attempts = 3 WHERE id = :i"), {"i": run_id}
        )


async def seed_retried_batch(
    client: httpx.AsyncClient, sm: SM, engine: AsyncEngine
) -> tuple[str, dict[str, uuid.UUID]]:
    ds, tasks = await frozen_dataset(client)
    version = await agent_version(sm)
    evaluation_id = (await create_evaluation(client, ds, version, repetitions=1)).json()["id"]
    runs = await run_ids(sm, evaluation_id, tasks)  # keys "A1", "B1", "C1"

    await add_attempt(sm, runs["A1"], 1, failed=True, evaluation=SKIPPED_MODEL)
    await add_attempt(
        sm, runs["A1"], 2, failed=False, evaluation=graded("passed", True),
        score=(components(1), "0.90000"),
    )  # fmt: skip
    await set_run(
        engine, runs["A1"],
        "status = 'COMPLETED', outcome = 'AGENT_FINISHED', attempt_count = 2, finished_at = now()",
    )  # fmt: skip

    await add_attempt(sm, runs["B1"], 1, failed=True, evaluation=SKIPPED_MODEL)
    await set_run(
        engine, runs["B1"],
        "status = 'RETRYING', attempt_count = 1, next_attempt_at = now() + interval '1 hour'",
    )  # fmt: skip

    await add_attempt(sm, runs["C1"], 1, failed=True, evaluation=SKIPPED_MODEL)
    await set_run(
        engine, runs["C1"],
        "status = 'CANCELLED', attempt_count = 1, cancel_requested_at = now(),"
        " cancel_reason = 'user', finished_at = now()",
    )  # fmt: skip
    return evaluation_id, runs


async def fetch(client: httpx.AsyncClient, evaluation_id: str) -> tuple[Any, Any, Any]:
    ev = (await client.get(f"{API}/evaluations/{evaluation_id}")).json()
    results = (await client.get(f"{API}/evaluations/{evaluation_id}/results")).json()
    summary = (await client.get(f"{API}/evaluations/{evaluation_id}/summary")).json()
    return ev["progress"], {r["run_id"]: r for r in results["results"]}, summary


async def test_a_retried_run_is_summarised_by_its_latest_attempt(
    client: httpx.AsyncClient, sessionmaker: SM, engine: AsyncEngine
) -> None:
    evaluation_id, runs = await seed_retried_batch(client, sessionmaker, engine)
    _, rows, summary = await fetch(client, evaluation_id)

    a1 = rows[str(runs["A1"])]
    assert (a1["run_status"], a1["attempt_no"], a1["duration_ms"]) == ("COMPLETED", 2, 200)
    assert a1["evaluation"]["status"] == "SUCCEEDED" and a1["evaluation"]["task_success"] is True
    # The stale attempt-1 platform_model failure is not part of the result.
    assert (summary["attempts"], summary["graded"], summary["passed"]) == (1, 1, 1)
    assert summary["platform_failures"] == 0 and summary["failure_categories"] == {"passed": 1}
    assert summary["latency_ms"] == {"p50": 200, "p95": 200}  # attempt 2's duration
    assert summary["cost_usd"]["total"] in ("0.010000", "0.01")  # one attempt's cost, not two
    assert (summary["tokens"]["tokens_in"], summary["tokens"]["tokens_out"]) == (10, 5)


async def test_a_retrying_run_is_not_finished(
    client: httpx.AsyncClient, sessionmaker: SM, engine: AsyncEngine
) -> None:
    evaluation_id, runs = await seed_retried_batch(client, sessionmaker, engine)
    progress, rows, summary = await fetch(client, evaluation_id)

    b1 = rows[str(runs["B1"])]
    assert b1["run_status"] == "RETRYING"
    # Its attempt-1 evaluation (SKIPPED, platform_model) exists but is history.
    assert (b1["attempt_no"], b1["evaluation"], b1["duration_ms"], b1["run_outcome"]) == (
        None, None, None, None,
    )  # fmt: skip
    assert summary["in_progress"] == 1 and summary["complete"] is False
    assert progress["complete"] is False
    assert progress["runs_by_status"]["RETRYING"] == 1
    assert progress["evaluations_by_status"]["SKIPPED"] == 0  # nobody's latest result is SKIPPED


async def test_a_cancelled_run_has_no_result_and_does_not_hold_the_batch_open(
    client: httpx.AsyncClient, sessionmaker: SM, engine: AsyncEngine
) -> None:
    evaluation_id, runs = await seed_retried_batch(client, sessionmaker, engine)
    progress, rows, summary = await fetch(client, evaluation_id)

    c1 = rows[str(runs["C1"])]
    assert c1["run_status"] == "CANCELLED"
    assert (c1["attempt_no"], c1["evaluation"], c1["run_outcome"], c1["cost_usd"]) == (
        None, None, None, None,
    )  # fmt: skip
    assert progress["runs_by_status"]["CANCELLED"] == 1
    # Only B1 (RETRYING) keeps the batch open: C1 is terminal, with nothing to wait for.
    assert summary["in_progress"] == 1

    await set_run(
        engine, runs["B1"],
        "status = 'CANCELLED', cancel_requested_at = now(), cancel_reason = 'batch',"
        " finished_at = now(), next_attempt_at = NULL",
    )  # fmt: skip
    progress, _, summary = await fetch(client, evaluation_id)
    assert progress["complete"] is True and summary["complete"] is True
    assert summary["in_progress"] == 0
    assert (summary["attempts"], summary["passed"]) == (1, 1)  # the cancelled runs add nothing
    assert progress["runs_by_status"] == {
        "QUEUED": 0, "CLAIMED": 0, "RUNNING": 0, "RETRYING": 0, "COMPLETED": 1, "FAILED": 0,
        "CANCELLED": 2,
    }  # fmt: skip


async def test_complete_means_every_run_terminal_and_nothing_awaiting_evaluation(
    client: httpx.AsyncClient, sessionmaker: SM, engine: AsyncEngine
) -> None:
    evaluation_id, runs = await seed_retried_batch(client, sessionmaker, engine)
    await set_run(
        engine, runs["B1"],
        "status = 'CANCELLED', cancel_requested_at = now(), cancel_reason = 'batch',"
        " finished_at = now(), next_attempt_at = NULL",
    )  # fmt: skip
    progress, _, _ = await fetch(client, evaluation_id)
    assert progress["complete"] is True and progress["awaiting_evaluation"] == 0

    # A's latest attempt is still being graded: terminal run, but awaiting evaluation.
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM scores"))
        await conn.execute(
            text(
                """UPDATE attempt_evaluations SET status = 'PENDING', task_success = NULL,
                     failure_category = NULL, finished_at = NULL, failure_tags = '[]'
                   WHERE attempt_id = (SELECT id FROM run_attempts WHERE run_id = :r
                                       AND attempt_no = 2)"""
            ),
            {"r": runs["A1"]},
        )
    progress, rows, summary = await fetch(client, evaluation_id)
    assert progress["awaiting_evaluation"] == 1 and progress["complete"] is False
    assert summary["complete"] is False and summary["in_progress"] == 1
    assert rows[str(runs["A1"])]["evaluation"]["status"] == "PENDING"
    assert progress["evaluations_by_status"]["PENDING"] == 1


async def test_a_replayed_run_is_in_progress_until_its_next_attempt_finishes(
    client: httpx.AsyncClient, sessionmaker: SM, engine: AsyncEngine
) -> None:
    """FAILED (counted) -> replay -> QUEUED (no result again, batch open) -> attempt 2 passes."""
    evaluation_id, runs = await seed_retried_batch(client, sessionmaker, engine)
    await add_attempt(sessionmaker, runs["B1"], 2, failed=True, evaluation=SKIPPED_MODEL)
    await set_run(
        engine, runs["B1"],
        "status = 'FAILED', outcome = 'MODEL_ERROR', attempt_count = 2, finished_at = now(),"
        " dead_letter_reason = 'retries_exhausted', next_attempt_at = NULL,"
        " error_message = 'model failed'",
    )  # fmt: skip
    progress, rows, summary = await fetch(client, evaluation_id)
    assert progress["complete"] is True
    assert summary["platform_failures"] == 1 and summary["attempts"] == 2
    assert rows[str(runs["B1"])]["attempt_no"] == 2

    replayed = await client.post(f"{API}/runs/{runs['B1']}/replay", json={"additional_attempts": 2})
    assert replayed.status_code == 202, replayed.text
    progress, rows, summary = await fetch(client, evaluation_id)
    assert progress["complete"] is False and progress["runs_by_status"]["QUEUED"] == 1
    assert (summary["in_progress"], summary["complete"], summary["attempts"]) == (1, False, 1)
    assert (
        rows[str(runs["B1"])]["attempt_no"] is None and rows[str(runs["B1"])]["evaluation"] is None
    )
    assert summary["platform_failures"] == 0  # the failed attempt 2 is history now

    # Attempt 3 passes: the run is COMPLETED and counted by attempt 3.
    await add_attempt(
        sessionmaker, runs["B1"], 3, failed=False, evaluation=graded("passed", True),
        score=(components(1), "0.90000"),
    )  # fmt: skip
    await set_run(
        engine, runs["B1"],
        "status = 'COMPLETED', outcome = 'AGENT_FINISHED', attempt_count = 3,"
        " finished_at = now(), dispatched_at = NULL",
    )  # fmt: skip
    progress, rows, summary = await fetch(client, evaluation_id)
    assert progress["complete"] is True and summary["complete"] is True
    assert (rows[str(runs["B1"])]["attempt_no"], summary["passed"], summary["attempts"]) == (
        3,
        2,
        2,
    )
