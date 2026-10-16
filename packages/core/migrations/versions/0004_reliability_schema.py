"""reliability schema

Phase 5: leases and fencing epochs, retry and grading budgets, cancellation,
dead-letter reasons and replay on runs; attempt failure reasons, retryability
and workspace capture; evaluation leases, budgets and error reasons; batch
budgets and cancellation; file-backed artifacts (ADR-008); the workers
registry; and the two T-terminal functions, p5_finish and p5_cancel.

Constraints that the current write paths cannot satisfy yet are added by the
migration of the step that starts writing those fields, so every revision
works with the code at that revision:
  * ck_runs_lease (Step 4: claims take a lease),
  * the attempt failure_reason presence rule (Step 3: finishes record a reason),
  * ck_attempt_evaluations_lease and _error_reason (Step 7: grading leases).

Literals are frozen on purpose, as in 0002 and 0003.

Downgrade REFUSES while any Phase 5 data exists; the refusal raises inside the
migration transaction, so the schema stays at 0004.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# -- frozen constraint bodies ----------------------------------------------------
RUN_STATUSES_0003 = "'QUEUED','CLAIMED','RUNNING','COMPLETED','FAILED'"
RUN_STATUSES = "'CANCELLED','CLAIMED','COMPLETED','FAILED','QUEUED','RETRYING','RUNNING'"
ATTEMPT_STATUSES = "'ABANDONED','CANCELLED','FINISHED','RUNNING'"
COMPLETED_OUTCOMES = (
    "'AGENT_FINISHED','BUDGET_EXCEEDED','MAX_STEPS','MODEL_REFUSAL','NON_ZERO_EXIT',"
    "'SUCCESS','TIMEOUT'"
)
FAILED_OUTCOMES = "'INTERNAL_ERROR','MODEL_ERROR','SANDBOX_ERROR'"
ATTEMPT_REASONS = (
    "'cancelled_by_batch','cancelled_by_user','config_invalid','config_model_unpriced',"
    "'config_provider_disabled','internal_unexpected','lease_expired','model_authentication',"
    "'model_bad_request','model_conflict','model_connection','model_not_found',"
    "'model_overloaded','model_permission','model_protocol','model_rate_limited',"
    "'model_request_too_large','model_server_error','model_timeout','persistence_trajectory',"
    "'sandbox_create_failed','sandbox_exec_failed','sandbox_image_not_found','sandbox_lost',"
    "'sandbox_reaper_failed','unclassified','worker_backstop','worker_capability',"
    "'worker_shutdown'"
)
RUN_REASONS = "'claim_abandoned','internal_inconsistent'"
EVALUATION_REASONS = (
    "'artifact_corrupt','artifact_missing','budget_timeout','capture_incomplete',"
    "'capture_invalid','capture_missing','capture_persist_failed','capture_too_large',"
    "'check_error','internal_unexpected','lease_expired','sandbox_failed',"
    "'scoring_config_invalid','spec_invalid','unclassified','worker_shutdown'"
)
CAPTURES = "'incomplete','invalid','not_needed','persist_failed','stored','too_large'"
REASON_OUTCOME = (
    "failure_reason IS NULL OR failure_reason = 'unclassified'"
    " OR (failure_reason LIKE 'model\\_%' AND outcome = 'MODEL_ERROR')"
    " OR (failure_reason LIKE 'sandbox\\_%' AND outcome = 'SANDBOX_ERROR')"
    " OR (failure_reason LIKE 'cancelled\\_%' AND outcome IS NULL)"
    " OR (failure_reason NOT LIKE 'model\\_%' AND failure_reason NOT LIKE 'sandbox\\_%'"
    " AND failure_reason NOT LIKE 'cancelled\\_%' AND outcome = 'INTERNAL_ERROR')"
)

# Result columns of run_attempts that p5_finish / p5_cancel accept in their payload.
ATTEMPT_RESULT_KEYS = (
    "exit_code",
    "oom_killed",
    "stdout",
    "stderr",
    "stdout_truncated",
    "stderr_truncated",
    "duration_ms",
    "error_message",
    "steps",
    "tokens_in",
    "tokens_out",
    "cost_usd",
    "final_answer",
    "sandbox_image_id",
)


def _ts(name: str) -> sa.Column:  # type: ignore[type-arg]
    return sa.Column(name, sa.DateTime(timezone=True), nullable=True)


def _check(table: str, name: str, condition: str) -> None:
    op.create_check_constraint(op.f(f"ck_{table}_{name}"), table, condition)


def _replace_check(table: str, name: str, condition: str) -> None:
    op.drop_constraint(op.f(f"ck_{table}_{name}"), table, type_="check")
    _check(table, name, condition)


def _payload_set(col: str, cast: str) -> str:
    """`col = <payload value>` if the payload has the key, else unchanged."""
    return (
        f"{col} = CASE WHEN p_attempt ? '{col}' THEN (p_attempt->>'{col}')::{cast} ELSE {col} END"
    )


_CASTS = {
    "exit_code": "integer",
    "oom_killed": "boolean",
    "stdout": "text",
    "stderr": "text",
    "stdout_truncated": "boolean",
    "stderr_truncated": "boolean",
    "duration_ms": "integer",
    "error_message": "text",
    "steps": "integer",
    "tokens_in": "bigint",
    "tokens_out": "bigint",
    "cost_usd": "numeric",
    "final_answer": "text",
    "sandbox_image_id": "text",
}
_PAYLOAD_SET = ",\n           ".join(_payload_set(c, _CASTS[c]) for c in ATTEMPT_RESULT_KEYS)
_ALLOWED_KEYS = ",".join(f"'{k}'" for k in ATTEMPT_RESULT_KEYS)

P5_FINISH = f"""
CREATE FUNCTION p5_finish(
    p_run uuid, p_attempt_id uuid, p_epoch bigint, p_target text, p_outcome text,
    p_failure_reason text, p_retryable boolean, p_workspace_capture text,
    p_next_attempt_at timestamptz, p_dead_letter_reason text, p_error_message text,
    p_attempt jsonb DEFAULT '{{}}'::jsonb)
RETURNS text
LANGUAGE plpgsql AS $$
-- T-terminal FINISH mode (R7 / R8 / R10 with A2) in one round trip.
-- Lock order: the attempt row, then the run row. Returns 'finished',
-- 'restart_cancel' (a cancel is pending: nothing written, call p5_cancel) or
-- 'fenced' (the attempt is no longer RUNNING at this epoch: nothing written).
DECLARE
    v_flag timestamptz;
    v_bad text;
BEGIN
    IF p_target NOT IN ('COMPLETED', 'RETRYING', 'FAILED') THEN
        RAISE EXCEPTION 'p5_finish: invalid target %', p_target USING ERRCODE = '22023';
    END IF;
    SELECT string_agg(k, ',') INTO v_bad FROM jsonb_object_keys(p_attempt) k
     WHERE k NOT IN ({_ALLOWED_KEYS});
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION 'p5_finish: unknown attempt fields %', v_bad USING ERRCODE = '22023';
    END IF;

    PERFORM 1 FROM run_attempts
     WHERE id = p_attempt_id AND run_id = p_run AND status = 'RUNNING' AND lease_epoch = p_epoch
     FOR NO KEY UPDATE;
    IF NOT FOUND THEN
        RETURN 'fenced';
    END IF;
    SELECT cancel_requested_at INTO v_flag FROM runs
     WHERE id = p_run AND status = 'RUNNING' AND lease_epoch = p_epoch
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'p5_finish: run % does not match its RUNNING attempt', p_run
            USING ERRCODE = 'P0001';
    END IF;
    IF v_flag IS NOT NULL THEN
        RETURN 'restart_cancel';
    END IF;

    UPDATE run_attempts
       SET status = 'FINISHED', outcome = p_outcome, failure_reason = p_failure_reason,
           retryable = p_retryable, workspace_capture = p_workspace_capture,
           finished_at = now(),
           {_PAYLOAD_SET}
     WHERE id = p_attempt_id;
    UPDATE runs
       SET status = p_target,
           outcome = CASE WHEN p_target = 'RETRYING' THEN NULL ELSE p_outcome END,
           error_message = CASE WHEN p_target = 'RETRYING' THEN NULL ELSE p_error_message END,
           next_attempt_at = CASE WHEN p_target = 'RETRYING' THEN p_next_attempt_at END,
           dead_letter_reason = CASE WHEN p_target = 'FAILED' THEN p_dead_letter_reason END,
           finished_at = CASE WHEN p_target = 'RETRYING' THEN NULL ELSE now() END,
           lease_expires_at = NULL
     WHERE id = p_run;
    RETURN 'finished';
END $$;
"""

P5_CANCEL = f"""
CREATE FUNCTION p5_cancel(
    p_run uuid, p_attempt_id uuid, p_epoch bigint, p_attempt jsonb DEFAULT '{{}}'::jsonb,
    OUT result text, OUT storage_keys text[])
LANGUAGE plpgsql AS $$
-- T-terminal CANCEL mode (R11 with A4) in one round trip. Deletes the
-- attempt's artifact rows under the attempt lock only, then takes the run row
-- for the short state change. The caller removes `storage_keys` files after
-- this returns (ADR-008). Returns 'cancelled' or 'fenced'.
DECLARE
    v_reason text;
    v_bad text;
BEGIN
    storage_keys := '{{}}';
    SELECT string_agg(k, ',') INTO v_bad FROM jsonb_object_keys(p_attempt) k
     WHERE k NOT IN ({_ALLOWED_KEYS});
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION 'p5_cancel: unknown attempt fields %', v_bad USING ERRCODE = '22023';
    END IF;

    PERFORM 1 FROM run_attempts
     WHERE id = p_attempt_id AND run_id = p_run AND status = 'RUNNING' AND lease_epoch = p_epoch
     FOR NO KEY UPDATE;
    IF NOT FOUND THEN
        result := 'fenced';
        RETURN;
    END IF;
    WITH d AS (DELETE FROM attempt_artifacts WHERE attempt_id = p_attempt_id RETURNING storage_key)
    SELECT coalesce(array_agg(storage_key) FILTER (WHERE storage_key IS NOT NULL), '{{}}')
      INTO storage_keys FROM d;
    SELECT cancel_reason INTO v_reason FROM runs
     WHERE id = p_run AND status = 'RUNNING' AND lease_epoch = p_epoch
       AND cancel_requested_at IS NOT NULL
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'p5_cancel: run % is not RUNNING with a cancel request', p_run
            USING ERRCODE = 'P0001';
    END IF;

    UPDATE run_attempts
       SET status = 'CANCELLED', outcome = NULL, failure_reason = 'cancelled_by_' || v_reason,
           retryable = false, workspace_capture = NULL, finished_at = now(),
           {_PAYLOAD_SET}
     WHERE id = p_attempt_id;
    UPDATE runs
       SET status = 'CANCELLED', lease_expires_at = NULL, next_attempt_at = NULL,
           finished_at = now()
     WHERE id = p_run;
    result := 'cancelled';
END $$;
"""


def upgrade() -> None:
    # -- runs ----------------------------------------------------------------------
    op.add_column(
        "runs", sa.Column("lease_epoch", sa.BigInteger(), server_default="0", nullable=False)
    )
    for col in (
        "lease_expires_at",
        "heartbeat_at",
        "next_attempt_at",
        "dispatched_at",
        "cancel_requested_at",
    ):
        op.add_column("runs", _ts(col))
    op.add_column("runs", sa.Column("cancel_reason", sa.String(length=16), nullable=True))
    op.add_column(
        "runs", sa.Column("max_attempts", sa.SmallInteger(), server_default="1", nullable=False)
    )
    op.add_column("runs", sa.Column("dead_letter_reason", sa.String(length=32), nullable=True))
    op.add_column(
        "runs", sa.Column("replay_count", sa.SmallInteger(), server_default="0", nullable=False)
    )
    op.add_column(
        "runs",
        sa.Column("unstarted_claims", sa.SmallInteger(), server_default="0", nullable=False),
    )
    op.add_column("runs", sa.Column("failure_reason", sa.String(length=40), nullable=True))
    _replace_check("runs", "status", f"status IN ({RUN_STATUSES})")
    _check("runs", "cancel_reason", "cancel_reason IN ('batch','user')")
    _check("runs", "max_attempts", "max_attempts BETWEEN 1 AND 10")
    _check(
        "runs", "dead_letter_reason", "dead_letter_reason IN ('non_retryable','retries_exhausted')"
    )
    _check("runs", "replay_count", "replay_count >= 0")
    _check("runs", "unstarted_claims", "unstarted_claims BETWEEN 0 AND 3")
    _check("runs", "failure_reason_values", f"failure_reason IN ({RUN_REASONS})")
    _check("runs", "failure_reason_failed", "failure_reason IS NULL OR status = 'FAILED'")
    _check("runs", "retrying", "(status = 'RETRYING') = (next_attempt_at IS NOT NULL)")
    _check("runs", "dead_letter", "dead_letter_reason IS NULL OR status = 'FAILED'")
    _check(
        "runs",
        "cancel_reason_iff_request",
        "(cancel_reason IS NULL) = (cancel_requested_at IS NULL)",
    )
    _check(
        "runs",
        "cancel_states",
        "cancel_requested_at IS NULL OR status IN ('CLAIMED','RUNNING','CANCELLED')",
    )
    _check(
        "runs",
        "cancelled_has_request",
        "status <> 'CANCELLED' OR cancel_requested_at IS NOT NULL",
    )
    _check("runs", "attempts", "attempt_count <= max_attempts")
    op.create_index(
        "ix_runs_lease",
        "runs",
        ["lease_expires_at"],
        postgresql_where=sa.text("status IN ('CLAIMED','RUNNING')"),
    )
    op.create_index(
        "ix_runs_retry",
        "runs",
        ["next_attempt_at"],
        postgresql_where=sa.text("status = 'RETRYING'"),
    )
    op.create_index(
        "ix_runs_dispatch", "runs", ["dispatched_at"], postgresql_where=sa.text("status = 'QUEUED'")
    )
    op.create_index(
        "ix_runs_failed",
        "runs",
        ["created_at", "id"],
        postgresql_where=sa.text("status = 'FAILED'"),
    )

    # -- run_attempts ----------------------------------------------------------------
    op.add_column(
        "run_attempts",
        sa.Column("lease_epoch", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.add_column("run_attempts", sa.Column("failure_reason", sa.String(length=40), nullable=True))
    op.add_column("run_attempts", sa.Column("retryable", sa.Boolean(), nullable=True))
    op.add_column(
        "run_attempts", sa.Column("workspace_capture", sa.String(length=16), nullable=True)
    )
    _replace_check("run_attempts", "status", f"status IN ({ATTEMPT_STATUSES})")
    _replace_check(
        "run_attempts",
        "outcome_iff_finished",
        "(status IN ('FINISHED','ABANDONED')) = (outcome IS NOT NULL)",
    )
    _check("run_attempts", "failure_reason_values", f"failure_reason IN ({ATTEMPT_REASONS})")
    _check("run_attempts", "workspace_capture_values", f"workspace_capture IN ({CAPTURES})")
    _check(
        "run_attempts",
        "abandoned",
        "status <> 'ABANDONED' OR (outcome = 'INTERNAL_ERROR' AND failure_reason = 'lease_expired')",
    )
    _check(
        "run_attempts",
        "cancelled",
        "status <> 'CANCELLED' OR failure_reason IN ('cancelled_by_batch','cancelled_by_user')",
    )
    _check("run_attempts", "retryable", "(retryable IS NULL) = (failure_reason IS NULL)")
    _check("run_attempts", "reason_outcome", REASON_OUTCOME)
    _check(
        "run_attempts",
        "capture",
        f"workspace_capture IS NULL OR (status = 'FINISHED' AND outcome IN ({COMPLETED_OUTCOMES}))",
    )

    # -- attempt_evaluations -----------------------------------------------------------
    op.add_column(
        "attempt_evaluations", sa.Column("worker_id", sa.String(length=255), nullable=True)
    )
    op.add_column(
        "attempt_evaluations",
        sa.Column("lease_epoch", sa.BigInteger(), server_default="0", nullable=False),
    )
    for col in ("lease_expires_at", "heartbeat_at", "next_attempt_at", "dispatched_at"):
        op.add_column("attempt_evaluations", _ts(col))
    op.add_column(
        "attempt_evaluations",
        sa.Column("eval_attempts", sa.SmallInteger(), server_default="0", nullable=False),
    )
    op.add_column(
        "attempt_evaluations",
        sa.Column("max_eval_attempts", sa.SmallInteger(), server_default="1", nullable=False),
    )
    op.add_column(
        "attempt_evaluations", sa.Column("error_reason", sa.String(length=32), nullable=True)
    )
    op.add_column(
        "attempt_evaluations", sa.Column("error_kind", sa.String(length=20), nullable=True)
    )
    _check("attempt_evaluations", "eval_attempts", "eval_attempts >= 0")
    _check("attempt_evaluations", "max_eval_attempts", "max_eval_attempts BETWEEN 1 AND 10")
    _check("attempt_evaluations", "eval_budget", "eval_attempts <= max_eval_attempts")
    _check(
        "attempt_evaluations",
        "pending_budget",
        "status <> 'PENDING' OR eval_attempts < max_eval_attempts",
    )
    _check("attempt_evaluations", "error_reason_values", f"error_reason IN ({EVALUATION_REASONS})")
    _check(
        "attempt_evaluations",
        "error_kind_values",
        "error_kind IN ('non_retryable','retries_exhausted')",
    )
    _check("attempt_evaluations", "error_kind", "error_kind IS NULL OR status = 'ERROR'")
    _check(
        "attempt_evaluations",
        "reason_states",
        "error_reason IS NULL OR status IN ('ERROR','PENDING')",
    )
    op.create_index(
        "ix_attempt_evaluations_lease",
        "attempt_evaluations",
        ["lease_expires_at"],
        postgresql_where=sa.text("status = 'RUNNING'"),
    )
    op.create_index(
        "ix_attempt_evaluations_pending",
        "attempt_evaluations",
        ["next_attempt_at", "dispatched_at"],
        postgresql_where=sa.text("status = 'PENDING'"),
    )

    # -- attempt_artifacts (ADR-008: bytes on the artifact volume) ----------------------
    op.add_column(
        "attempt_artifacts",
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "attempt_artifacts", sa.Column("storage_key", sa.String(length=255), nullable=True)
    )
    op.alter_column("attempt_artifacts", "content", existing_type=sa.LargeBinary(), nullable=True)
    _replace_check(
        "attempt_artifacts",
        "size_matches_content",
        "content IS NULL OR size_bytes = octet_length(content)",
    )
    _check("attempt_artifacts", "one_location", "num_nonnulls(content, storage_key) = 1")

    # -- evaluations (batches) -----------------------------------------------------------
    op.add_column("evaluations", _ts("cancel_requested_at"))
    op.add_column("evaluations", sa.Column("cancel_reason", sa.String(length=16), nullable=True))
    op.add_column(
        "evaluations",
        sa.Column("run_max_attempts", sa.SmallInteger(), server_default="1", nullable=False),
    )
    op.add_column(
        "evaluations",
        sa.Column("eval_max_attempts", sa.SmallInteger(), server_default="1", nullable=False),
    )
    _check("evaluations", "cancel_reason", "cancel_reason IN ('user')")
    _check(
        "evaluations",
        "cancel_reason_iff_request",
        "(cancel_reason IS NULL) = (cancel_requested_at IS NULL)",
    )
    _check("evaluations", "run_max_attempts", "run_max_attempts BETWEEN 1 AND 10")
    _check("evaluations", "eval_max_attempts", "eval_max_attempts BETWEEN 1 AND 10")

    # -- workers ---------------------------------------------------------------------------
    op.create_table(
        "workers",
        sa.Column("id", sa.String(length=255), nullable=False),
        sa.Column("hostname", sa.String(length=255), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("concurrency", sa.SmallInteger(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_heartbeat_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workers")),
    )
    op.create_index(
        "ix_workers_live",
        "workers",
        ["last_heartbeat_at"],
        postgresql_where=sa.text("stopped_at IS NULL"),
    )

    # -- backfill (legacy rows; every constraint above already holds) ----------------------
    # Stuck claims and runs become recoverable on the reconciler's first tick (Step 5).
    op.execute("UPDATE runs SET lease_expires_at = now() WHERE status IN ('CLAIMED','RUNNING')")
    op.execute(
        "UPDATE run_attempts SET failure_reason = 'unclassified', retryable = false"
        f" WHERE status = 'FINISHED' AND outcome IN ({FAILED_OUTCOMES})"
    )
    op.execute(
        "UPDATE attempt_evaluations SET error_reason = 'unclassified', error_kind = 'non_retryable'"
        " WHERE status = 'ERROR'"
    )
    op.execute(
        "UPDATE attempt_evaluations SET lease_expires_at = now(), worker_id = 'legacy'"
        " WHERE status = 'RUNNING'"
    )

    # -- T-terminal functions (ADR-008 C) ----------------------------------------------------
    op.execute(P5_FINISH)
    op.execute(P5_CANCEL)


# What a downgrade would lose or misrepresent, as {description: count query}.
_PHASE5_DATA = {
    "runs in RETRYING or CANCELLED": "SELECT count(*) FROM runs WHERE status IN ('RETRYING','CANCELLED')",
    "runs with Phase 5 budgets or history": (
        "SELECT count(*) FROM runs WHERE max_attempts > 1 OR replay_count > 0"
        " OR unstarted_claims > 0 OR dead_letter_reason IS NOT NULL OR failure_reason IS NOT NULL"
        " OR cancel_requested_at IS NOT NULL OR attempt_count > 1"
    ),
    "attempts ABANDONED or CANCELLED": (
        "SELECT count(*) FROM run_attempts WHERE status IN ('ABANDONED','CANCELLED')"
    ),
    "attempts with a recorded reason or capture": (
        "SELECT count(*) FROM run_attempts WHERE (failure_reason IS NOT NULL"
        " AND failure_reason <> 'unclassified') OR workspace_capture IS NOT NULL"
    ),
    "evaluations with grading history": (
        "SELECT count(*) FROM attempt_evaluations WHERE eval_attempts > 0 OR max_eval_attempts > 1"
        " OR (error_reason IS NOT NULL AND error_reason <> 'unclassified')"
        " OR (worker_id IS NOT NULL AND worker_id <> 'legacy')"
    ),
    "batches with Phase 5 settings": (
        "SELECT count(*) FROM evaluations WHERE run_max_attempts > 1 OR eval_max_attempts > 1"
        " OR cancel_requested_at IS NOT NULL"
    ),
    "file-backed artifacts": "SELECT count(*) FROM attempt_artifacts WHERE storage_key IS NOT NULL",
    "workers": "SELECT count(*) FROM workers",
}


def _refuse_if_phase5_data() -> None:
    bind = op.get_bind()
    found = {
        what: n
        for what, sql in _PHASE5_DATA.items()
        if (n := bind.execute(sa.text(sql)).scalar_one())
    }
    if found:
        detail = ", ".join(f"{what}: {n}" for what, n in found.items())
        raise RuntimeError(
            f"refusing to downgrade 0004: Phase 5 data exists ({detail}). "
            "Dropping it would silently lose retry, cancellation or artifact state; "
            "remove the data first."
        )


def downgrade() -> None:
    _refuse_if_phase5_data()
    op.execute("DROP FUNCTION p5_cancel(uuid, uuid, bigint, jsonb)")
    op.execute(
        "DROP FUNCTION p5_finish(uuid, uuid, bigint, text, text, text, boolean, text,"
        " timestamptz, text, text, jsonb)"
    )
    op.drop_index("ix_workers_live", table_name="workers")
    op.drop_table("workers")

    for name in (
        "eval_max_attempts",
        "run_max_attempts",
        "cancel_reason_iff_request",
        "cancel_reason",
    ):
        op.drop_constraint(op.f(f"ck_evaluations_{name}"), "evaluations", type_="check")
    for col in ("eval_max_attempts", "run_max_attempts", "cancel_reason", "cancel_requested_at"):
        op.drop_column("evaluations", col)

    op.drop_constraint(
        op.f("ck_attempt_artifacts_one_location"), "attempt_artifacts", type_="check"
    )
    _replace_check(
        "attempt_artifacts", "size_matches_content", "size_bytes = octet_length(content)"
    )
    op.alter_column("attempt_artifacts", "content", existing_type=sa.LargeBinary(), nullable=False)
    op.drop_column("attempt_artifacts", "storage_key")
    op.drop_column("attempt_artifacts", "metadata")

    op.drop_index("ix_attempt_evaluations_pending", table_name="attempt_evaluations")
    op.drop_index("ix_attempt_evaluations_lease", table_name="attempt_evaluations")
    for name in (
        "reason_states",
        "error_kind",
        "error_kind_values",
        "error_reason_values",
        "pending_budget",
        "eval_budget",
        "max_eval_attempts",
        "eval_attempts",
    ):
        op.drop_constraint(
            op.f(f"ck_attempt_evaluations_{name}"), "attempt_evaluations", type_="check"
        )
    for col in (
        "error_kind",
        "error_reason",
        "max_eval_attempts",
        "eval_attempts",
        "dispatched_at",
        "next_attempt_at",
        "heartbeat_at",
        "lease_expires_at",
        "lease_epoch",
        "worker_id",
    ):
        op.drop_column("attempt_evaluations", col)

    for name in (
        "capture",
        "reason_outcome",
        "retryable",
        "cancelled",
        "abandoned",
        "workspace_capture_values",
        "failure_reason_values",
    ):
        op.drop_constraint(op.f(f"ck_run_attempts_{name}"), "run_attempts", type_="check")
    _replace_check(
        "run_attempts", "outcome_iff_finished", "(status = 'FINISHED') = (outcome IS NOT NULL)"
    )
    _replace_check("run_attempts", "status", "status IN ('RUNNING','FINISHED')")
    for col in ("workspace_capture", "retryable", "failure_reason", "lease_epoch"):
        op.drop_column("run_attempts", col)

    for idx in ("ix_runs_failed", "ix_runs_dispatch", "ix_runs_retry", "ix_runs_lease"):
        op.drop_index(idx, table_name="runs")
    for name in (
        "attempts",
        "cancelled_has_request",
        "cancel_states",
        "cancel_reason_iff_request",
        "dead_letter",
        "retrying",
        "failure_reason_failed",
        "failure_reason_values",
        "unstarted_claims",
        "replay_count",
        "dead_letter_reason",
        "max_attempts",
        "cancel_reason",
    ):
        op.drop_constraint(op.f(f"ck_runs_{name}"), "runs", type_="check")
    _replace_check("runs", "status", f"status IN ({RUN_STATUSES_0003})")
    for col in (
        "failure_reason",
        "unstarted_claims",
        "replay_count",
        "dead_letter_reason",
        "max_attempts",
        "cancel_reason",
        "cancel_requested_at",
        "dispatched_at",
        "next_attempt_at",
        "heartbeat_at",
        "lease_expires_at",
        "lease_epoch",
    ):
        op.drop_column("runs", col)
