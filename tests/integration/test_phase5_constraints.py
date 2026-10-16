"""Every CHECK added by migration 0004, accepted and rejected, on the migrated
head schema. Each rejection must name the constraint that fired."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.integration.phase5_seed import add_artifact, running_run

# (constraint, statement that must violate it). The run is RUNNING at epoch 1
# with its RUNNING attempt; :r and :a are their ids.
REJECTED = [
    ("ck_runs_status", "UPDATE runs SET status = 'PAUSED' WHERE id = :r"),
    (
        "ck_runs_cancel_reason",
        "UPDATE runs SET cancel_reason = 'admin', cancel_requested_at = now() WHERE id = :r",
    ),
    ("ck_runs_max_attempts", "UPDATE runs SET max_attempts = 11 WHERE id = :r"),
    ("ck_runs_attempts", "UPDATE runs SET attempt_count = 4 WHERE id = :r"),
    ("ck_runs_replay_count", "UPDATE runs SET replay_count = -1 WHERE id = :r"),
    ("ck_runs_unstarted_claims", "UPDATE runs SET unstarted_claims = 4 WHERE id = :r"),
    (
        "ck_runs_dead_letter_reason",
        "UPDATE runs SET status = 'FAILED', outcome = 'INTERNAL_ERROR', "
        "dead_letter_reason = 'gave_up' WHERE id = :r",
    ),
    ("ck_runs_dead_letter", "UPDATE runs SET dead_letter_reason = 'non_retryable' WHERE id = :r"),
    (
        "ck_runs_failure_reason_values",
        "UPDATE runs SET status = 'FAILED', outcome = 'INTERNAL_ERROR', "
        "failure_reason = 'lease_expired' WHERE id = :r",
    ),
    (
        "ck_runs_failure_reason_failed",
        "UPDATE runs SET failure_reason = 'claim_abandoned' WHERE id = :r",
    ),
    ("ck_runs_retrying", "UPDATE runs SET status = 'RETRYING' WHERE id = :r"),
    ("ck_runs_retrying", "UPDATE runs SET next_attempt_at = now() WHERE id = :r"),
    (
        "ck_runs_cancel_reason_iff_request",
        "UPDATE runs SET cancel_requested_at = now() WHERE id = :r",
    ),
    (
        "ck_runs_cancel_states",
        "UPDATE runs SET status = 'COMPLETED', outcome = 'SUCCESS', cancel_requested_at = now(), "
        "cancel_reason = 'user' WHERE id = :r",
    ),
    ("ck_runs_cancelled_has_request", "UPDATE runs SET status = 'CANCELLED' WHERE id = :r"),
    ("ck_run_attempts_status", "UPDATE run_attempts SET status = 'LOST' WHERE id = :a"),
    (
        "ck_run_attempts_outcome_iff_finished",
        "UPDATE run_attempts SET status = 'ABANDONED', failure_reason = 'lease_expired', "
        "retryable = true WHERE id = :a",
    ),
    (
        "ck_run_attempts_failure_reason_values",
        "UPDATE run_attempts SET failure_reason = 'claim_abandoned', retryable = false "
        "WHERE id = :a",
    ),
    ("ck_run_attempts_retryable", "UPDATE run_attempts SET retryable = true WHERE id = :a"),
    (
        "ck_run_attempts_abandoned",
        "UPDATE run_attempts SET status = 'ABANDONED', outcome = 'INTERNAL_ERROR', "
        "failure_reason = 'worker_shutdown', retryable = true WHERE id = :a",
    ),
    (
        "ck_run_attempts_cancelled",
        "UPDATE run_attempts SET status = 'CANCELLED', failure_reason = 'lease_expired', "
        "retryable = true WHERE id = :a",
    ),
    (
        "ck_run_attempts_reason_outcome",
        "UPDATE run_attempts SET status = 'FINISHED', outcome = 'SANDBOX_ERROR', "
        "failure_reason = 'model_timeout', retryable = true WHERE id = :a",
    ),
    (
        "ck_run_attempts_workspace_capture_values",
        "UPDATE run_attempts SET status = 'FINISHED', outcome = 'SUCCESS', "
        "workspace_capture = 'lost' WHERE id = :a",
    ),
    (
        "ck_run_attempts_capture",
        "UPDATE run_attempts SET workspace_capture = 'stored' WHERE id = :a",
    ),
    (
        "ck_run_attempts_capture",
        "UPDATE run_attempts SET status = 'FINISHED', outcome = 'SANDBOX_ERROR', "
        "failure_reason = 'sandbox_lost', retryable = true, workspace_capture = 'stored' "
        "WHERE id = :a",
    ),
]


@pytest.mark.parametrize(
    ("constraint", "sql"), REJECTED, ids=[f"{c}-{i}" for i, (c, _) in enumerate(REJECTED)]
)
async def test_run_and_attempt_checks_reject(
    engine: AsyncEngine, constraint: str, sql: str
) -> None:
    run_id, attempt_id = await running_run(engine)
    with pytest.raises(IntegrityError, match=constraint):
        async with engine.begin() as conn:
            await conn.execute(text(sql), {"r": run_id, "a": attempt_id})


ACCEPTED = [
    # R8: RETRYING, no outcome or message on the run (G14); the attempt keeps its reason.
    "UPDATE run_attempts SET status = 'FINISHED', outcome = 'MODEL_ERROR', "
    "failure_reason = 'model_server_error', retryable = true WHERE id = :a; "
    "UPDATE runs SET status = 'RETRYING', next_attempt_at = now() + interval '2 s' WHERE id = :r",
    # R11 with A4: CANCELLED, request recorded, no outcome.
    "UPDATE runs SET cancel_requested_at = now(), cancel_reason = 'batch' WHERE id = :r; "
    "UPDATE run_attempts SET status = 'CANCELLED', failure_reason = 'cancelled_by_batch', "
    "retryable = false WHERE id = :a; "
    "UPDATE runs SET status = 'CANCELLED' WHERE id = :r",
    # R9/R10 with A3: ABANDONED with lease_expired, then dead-lettered.
    "UPDATE run_attempts SET status = 'ABANDONED', outcome = 'INTERNAL_ERROR', "
    "failure_reason = 'lease_expired', retryable = true WHERE id = :a; "
    "UPDATE runs SET status = 'FAILED', outcome = 'INTERNAL_ERROR', "
    "dead_letter_reason = 'retries_exhausted' WHERE id = :r",
    # R7 with a stored capture.
    "UPDATE run_attempts SET status = 'FINISHED', outcome = 'AGENT_FINISHED', "
    "workspace_capture = 'stored' WHERE id = :a; "
    "UPDATE runs SET status = 'COMPLETED', outcome = 'AGENT_FINISHED' WHERE id = :r",
    # Config failures pair with INTERNAL_ERROR; the legacy reason pairs with any failed outcome.
    "UPDATE run_attempts SET status = 'FINISHED', outcome = 'INTERNAL_ERROR', "
    "failure_reason = 'config_invalid', retryable = false WHERE id = :a",
    "UPDATE run_attempts SET status = 'FINISHED', outcome = 'MODEL_ERROR', "
    "failure_reason = 'unclassified', retryable = false WHERE id = :a",
]


@pytest.mark.parametrize("sql", ACCEPTED)
async def test_valid_phase5_shapes_are_accepted(engine: AsyncEngine, sql: str) -> None:
    run_id, attempt_id = await running_run(engine)
    async with engine.begin() as conn:
        for stmt in sql.split("; "):
            await conn.execute(text(stmt), {"r": run_id, "a": attempt_id})


async def test_run_level_failure_reason_on_a_failed_run(engine: AsyncEngine) -> None:
    run_id, attempt_id = await running_run(engine)
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM run_attempts WHERE id = :a"), {"a": attempt_id})
        await conn.execute(
            text(
                "UPDATE runs SET status = 'FAILED', outcome = 'INTERNAL_ERROR', attempt_count = 0, "
                "failure_reason = 'claim_abandoned', dead_letter_reason = 'retries_exhausted', "
                "unstarted_claims = 3 WHERE id = :r"
            ),
            {"r": run_id},
        )


EVAL_REJECTED = [
    (
        "ck_attempt_evaluations_max_eval_attempts",
        "UPDATE attempt_evaluations SET max_eval_attempts = 0 WHERE id = :v",
    ),
    (
        "ck_attempt_evaluations_eval_attempts",
        "UPDATE attempt_evaluations SET eval_attempts = -1 WHERE id = :v",
    ),
    (
        "ck_attempt_evaluations_eval_budget",
        "UPDATE attempt_evaluations SET status = 'RUNNING', eval_attempts = 4, "
        "max_eval_attempts = 3 WHERE id = :v",
    ),
    (
        "ck_attempt_evaluations_pending_budget",
        "UPDATE attempt_evaluations SET eval_attempts = 1 WHERE id = :v",
    ),
    (
        "ck_attempt_evaluations_error_reason_values",
        "UPDATE attempt_evaluations SET error_reason = 'oops' WHERE id = :v",
    ),
    (
        "ck_attempt_evaluations_error_kind_values",
        "UPDATE attempt_evaluations SET status = 'ERROR', failure_category = 'evaluation_error', "
        "error_message = 'x', finished_at = now(), error_kind = 'tired' WHERE id = :v",
    ),
    (
        "ck_attempt_evaluations_error_kind",
        "UPDATE attempt_evaluations SET error_kind = 'non_retryable' WHERE id = :v",
    ),
    (
        "ck_attempt_evaluations_reason_states",
        "UPDATE attempt_evaluations SET status = 'RUNNING', error_reason = 'sandbox_failed' "
        "WHERE id = :v",
    ),
]


@pytest.mark.parametrize(("constraint", "sql"), EVAL_REJECTED, ids=[c for c, _ in EVAL_REJECTED])
async def test_evaluation_checks_reject(engine: AsyncEngine, constraint: str, sql: str) -> None:
    _, attempt_id = await running_run(engine)
    async with engine.begin() as conn:
        v = (
            await conn.execute(
                text(
                    "INSERT INTO attempt_evaluations (id, attempt_id, status, evaluator_version) "
                    "VALUES (gen_random_uuid(), :a, 'PENDING', 'v1') RETURNING id"
                ),
                {"a": attempt_id},
            )
        ).scalar_one()
    with pytest.raises(IntegrityError, match=constraint):
        async with engine.begin() as conn:
            await conn.execute(text(sql), {"v": v})


async def test_pending_retry_row_with_budget_left_is_accepted(engine: AsyncEngine) -> None:
    _, attempt_id = await running_run(engine)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO attempt_evaluations (id, attempt_id, status, evaluator_version, "
                "eval_attempts, max_eval_attempts, error_reason, next_attempt_at) VALUES "
                "(gen_random_uuid(), :a, 'PENDING', 'v1', 2, 3, 'sandbox_failed', now())"
            ),
            {"a": attempt_id},
        )


async def test_artifact_lives_in_exactly_one_place(engine: AsyncEngine) -> None:
    _, attempt_id = await running_run(engine)
    await add_artifact(engine, attempt_id, storage_key=None)  # Phase 4 bytea row
    _, other = await running_run(engine)
    await add_artifact(engine, other)  # ADR-008 file-backed row
    with pytest.raises(IntegrityError, match="ck_attempt_artifacts_one_location"):
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE attempt_artifacts SET storage_key = 'x' WHERE attempt_id = :a"),
                {"a": attempt_id},
            )
    with pytest.raises(IntegrityError, match="ck_attempt_artifacts_one_location"):
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE attempt_artifacts SET storage_key = NULL WHERE attempt_id = :a"),
                {"a": other},
            )
    with pytest.raises(IntegrityError, match="ck_attempt_artifacts_size_matches_content"):
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE attempt_artifacts SET size_bytes = 5 WHERE attempt_id = :a"),
                {"a": attempt_id},
            )


BATCH_REJECTED = [
    ("ck_evaluations_run_max_attempts", "run_max_attempts = 0"),
    ("ck_evaluations_eval_max_attempts", "eval_max_attempts = 11"),
    ("ck_evaluations_cancel_reason", "cancel_reason = 'batch', cancel_requested_at = now()"),
    ("ck_evaluations_cancel_reason_iff_request", "cancel_requested_at = now()"),
]


@pytest.mark.parametrize(
    ("constraint", "assignment"), BATCH_REJECTED, ids=[c for c, _ in BATCH_REJECTED]
)
async def test_batch_checks_reject(engine: AsyncEngine, constraint: str, assignment: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO agents (id, name) VALUES ('44444444-4444-4444-4444-444444444444', 'a')"
            )
        )
        await conn.execute(
            text(
                """INSERT INTO agent_versions (id, agent_id, version, model_provider, model,
                     system_prompt, tools, shell_allowlist, limits, model_settings, config_hash)
                   VALUES ('55555555-5555-5555-5555-555555555555',
                     '44444444-4444-4444-4444-444444444444', 1, 'fake', 'fake-model', 's', '[]',
                     '[]', '{}', '{"script": []}', repeat('a', 64))"""
            )
        )
        await conn.execute(
            text(
                "INSERT INTO datasets (id, name, version, status, frozen_at) VALUES "
                "('66666666-6666-6666-6666-666666666666', 'd', '1', 'FROZEN', now())"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO evaluations (id, name, dataset_id, agent_version_id, repetitions, "
                "scoring_config, scoring_config_hash) VALUES "
                "('77777777-7777-7777-7777-777777777777', 'e', "
                "'66666666-6666-6666-6666-666666666666', '55555555-5555-5555-5555-555555555555', "
                "1, '{}', repeat('d', 64))"
            )
        )
    with pytest.raises(IntegrityError, match=constraint):
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    f"UPDATE evaluations SET {assignment} "
                    "WHERE id = '77777777-7777-7777-7777-777777777777'"
                )
            )
