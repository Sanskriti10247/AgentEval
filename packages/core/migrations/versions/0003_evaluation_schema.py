"""evaluation schema

Phase 4: datasets (draft -> frozen), task evaluation specs (internal; agent
tasks only), evaluation batches, workspace snapshots, attempt evaluations and
append-only scores; runs.evaluation_id/repetition; run_attempts.sandbox_image_id.

Literals are frozen on purpose: the migration describes the schema at this
revision, not whatever the enums in code say later.

Downgrade REFUSES while any Phase 4 data exists. Unlike 0002, nothing would
fail on its own here: dropping the tables and columns would silently destroy
evaluation results. The refusal raises inside the migration transaction, so
the schema stays at 0003.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "datasets",
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("version", sa.String(length=50), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status = 'FROZEN') = (frozen_at IS NOT NULL)", name=op.f("ck_datasets_frozen_at")
        ),
        sa.CheckConstraint("status IN ('DRAFT','FROZEN')", name=op.f("ck_datasets_status")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_datasets")),
        sa.UniqueConstraint("name", "version", name="uq_datasets_name_version"),
    )
    op.create_table(
        "evaluations",
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("dataset_id", sa.Uuid(), nullable=False),
        sa.Column("agent_version_id", sa.Uuid(), nullable=False),
        sa.Column("repetitions", sa.Integer(), nullable=False),
        sa.Column("scoring_config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("scoring_config_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(scoring_config_hash) = 64", name=op.f("ck_evaluations_scoring_config_hash")
        ),
        sa.CheckConstraint("repetitions BETWEEN 1 AND 10", name=op.f("ck_evaluations_repetitions")),
        sa.ForeignKeyConstraint(
            ["agent_version_id"],
            ["agent_versions.id"],
            name=op.f("fk_evaluations_agent_version_id_agent_versions"),
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id"], ["datasets.id"], name=op.f("fk_evaluations_dataset_id_datasets")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evaluations")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_evaluations_idempotency_key")),
    )
    op.create_table(
        "attempt_artifacts",
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("kind IN ('workspace')", name=op.f("ck_attempt_artifacts_kind")),
        sa.CheckConstraint("length(sha256) = 64", name=op.f("ck_attempt_artifacts_sha256")),
        sa.CheckConstraint(
            "size_bytes = octet_length(content)",
            name=op.f("ck_attempt_artifacts_size_matches_content"),
        ),
        sa.CheckConstraint(
            "size_bytes BETWEEN 0 AND 16777216", name=op.f("ck_attempt_artifacts_size_bytes")
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["run_attempts.id"],
            name=op.f("fk_attempt_artifacts_attempt_id_run_attempts"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_attempt_artifacts")),
        sa.UniqueConstraint("attempt_id", "kind", name="uq_attempt_artifacts_attempt_id_kind"),
    )
    op.create_table(
        "attempt_evaluations",
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("evaluator_version", sa.String(length=32), nullable=False),
        sa.Column(
            "checks", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "metrics", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "integrity", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True
        ),
        sa.Column("task_success", sa.Boolean(), nullable=True),
        sa.Column("failure_category", sa.String(length=32), nullable=True),
        sa.Column(
            "failure_tags",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("failure_detail", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "failure_category IN ('budget_exceeded','evaluation_error','max_steps','missing_output','no_answer','passed','platform_cancelled','platform_internal','platform_model','platform_sandbox','refusal','tampering','tests_failed','timeout','wrong_answer')",
            name=op.f("ck_attempt_evaluations_failure_category"),
        ),
        sa.CheckConstraint(
            "status <> 'ERROR' OR (failure_category = 'evaluation_error' AND error_message IS NOT NULL AND task_success IS NULL AND finished_at IS NOT NULL)",
            name=op.f("ck_attempt_evaluations_error"),
        ),
        sa.CheckConstraint(
            "status <> 'SKIPPED' OR (task_success IS NULL AND finished_at IS NOT NULL)",
            name=op.f("ck_attempt_evaluations_skipped"),
        ),
        sa.CheckConstraint(
            "status <> 'SUCCEEDED' OR (task_success IS NOT NULL AND failure_category IS NOT NULL AND finished_at IS NOT NULL)",
            name=op.f("ck_attempt_evaluations_succeeded"),
        ),
        sa.CheckConstraint(
            "status IN ('ERROR','PENDING','RUNNING','SKIPPED','SUCCEEDED')",
            name=op.f("ck_attempt_evaluations_status"),
        ),
        sa.CheckConstraint(
            "status NOT IN ('PENDING','RUNNING') OR (task_success IS NULL AND failure_category IS NULL AND finished_at IS NULL)",
            name=op.f("ck_attempt_evaluations_unfinished"),
        ),
        sa.CheckConstraint(
            "task_success IS NULL OR task_success = (failure_category = 'passed')",
            name=op.f("ck_attempt_evaluations_passed_iff_success"),
        ),
        sa.CheckConstraint("duration_ms >= 0", name=op.f("ck_attempt_evaluations_duration_ms")),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["run_attempts.id"],
            name=op.f("fk_attempt_evaluations_attempt_id_run_attempts"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_attempt_evaluations")),
        sa.UniqueConstraint("attempt_id", name="uq_attempt_evaluations_attempt_id"),
    )
    op.create_table(
        "scores",
        sa.Column("attempt_evaluation_id", sa.Uuid(), nullable=False),
        sa.Column("scoring_config_hash", sa.String(length=64), nullable=False),
        sa.Column("scoring_config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("components", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("overall", sa.Numeric(precision=6, scale=5), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(scoring_config_hash) = 64", name=op.f("ck_scores_scoring_config_hash")
        ),
        sa.CheckConstraint("overall >= 0 AND overall <= 1", name=op.f("ck_scores_overall")),
        sa.ForeignKeyConstraint(
            ["attempt_evaluation_id"],
            ["attempt_evaluations.id"],
            name=op.f("fk_scores_attempt_evaluation_id_attempt_evaluations"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scores")),
        sa.UniqueConstraint(
            "attempt_evaluation_id",
            "scoring_config_hash",
            name="uq_scores_attempt_evaluation_id_scoring_config_hash",
        ),
    )
    op.add_column(
        "run_attempts", sa.Column("sandbox_image_id", sa.String(length=128), nullable=True)
    )
    op.add_column("runs", sa.Column("evaluation_id", sa.Uuid(), nullable=True))
    op.add_column("runs", sa.Column("repetition", sa.Integer(), nullable=True))
    op.create_index("ix_runs_evaluation_id", "runs", ["evaluation_id"], unique=False)
    op.create_unique_constraint(
        "uq_runs_evaluation_id_task_id_repetition",
        "runs",
        ["evaluation_id", "task_id", "repetition"],
    )
    op.create_foreign_key(
        op.f("fk_runs_evaluation_id_evaluations"), "runs", "evaluations", ["evaluation_id"], ["id"]
    )
    op.create_check_constraint(
        op.f("ck_runs_evaluation_repetition"),
        "runs",
        "(evaluation_id IS NULL) = (repetition IS NULL)",
    )
    op.create_check_constraint(op.f("ck_runs_repetition"), "runs", "repetition >= 1")
    op.add_column("tasks", sa.Column("dataset_id", sa.Uuid(), nullable=True))
    op.add_column("tasks", sa.Column("difficulty", sa.String(length=16), nullable=True))
    op.add_column(
        "tasks",
        sa.Column(
            "evaluation", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True
        ),
    )
    op.create_foreign_key(
        op.f("fk_tasks_dataset_id_datasets"), "tasks", "datasets", ["dataset_id"], ["id"]
    )
    op.create_check_constraint(
        op.f("ck_tasks_evaluation"), "tasks", "task_type = 'agent' OR evaluation IS NULL"
    )
    op.create_check_constraint(
        op.f("ck_tasks_dataset_id"), "tasks", "task_type = 'agent' OR dataset_id IS NULL"
    )
    op.create_check_constraint(
        op.f("ck_tasks_evaluation_size"), "tasks", "octet_length(evaluation::text) <= 2097152"
    )
    op.create_check_constraint(
        op.f("ck_tasks_difficulty"), "tasks", "difficulty IN ('easy','hard','medium')"
    )


# What would be lost by a downgrade, as {description: count query}.
_PHASE4_DATA = {
    "datasets": "SELECT count(*) FROM datasets",
    "evaluations": "SELECT count(*) FROM evaluations",
    "attempt_artifacts": "SELECT count(*) FROM attempt_artifacts",
    "attempt_evaluations": "SELECT count(*) FROM attempt_evaluations",
    "scores": "SELECT count(*) FROM scores",
    "tasks with dataset/difficulty/evaluation": (
        "SELECT count(*) FROM tasks WHERE dataset_id IS NOT NULL "
        "OR difficulty IS NOT NULL OR evaluation IS NOT NULL"
    ),
    "runs in an evaluation": "SELECT count(*) FROM runs WHERE evaluation_id IS NOT NULL",
    "attempts with sandbox_image_id": (
        "SELECT count(*) FROM run_attempts WHERE sandbox_image_id IS NOT NULL"
    ),
}


def _refuse_if_phase4_data() -> None:
    bind = op.get_bind()
    found = {
        what: n
        for what, sql in _PHASE4_DATA.items()
        if (n := bind.execute(sa.text(sql)).scalar_one())
    }
    if found:
        detail = ", ".join(f"{what}: {n}" for what, n in found.items())
        raise RuntimeError(
            f"refusing to downgrade 0003: Phase 4 data exists ({detail}). "
            "Dropping it would silently destroy evaluation results; remove the data first."
        )


def downgrade() -> None:
    _refuse_if_phase4_data()
    op.drop_constraint(op.f("ck_tasks_difficulty"), "tasks", type_="check")
    op.drop_constraint(op.f("ck_tasks_evaluation_size"), "tasks", type_="check")
    op.drop_constraint(op.f("ck_tasks_dataset_id"), "tasks", type_="check")
    op.drop_constraint(op.f("ck_tasks_evaluation"), "tasks", type_="check")
    op.drop_constraint(op.f("fk_tasks_dataset_id_datasets"), "tasks", type_="foreignkey")
    op.drop_column("tasks", "evaluation")
    op.drop_column("tasks", "difficulty")
    op.drop_column("tasks", "dataset_id")
    op.drop_constraint(op.f("ck_runs_repetition"), "runs", type_="check")
    op.drop_constraint(op.f("ck_runs_evaluation_repetition"), "runs", type_="check")
    op.drop_constraint(op.f("fk_runs_evaluation_id_evaluations"), "runs", type_="foreignkey")
    op.drop_constraint("uq_runs_evaluation_id_task_id_repetition", "runs", type_="unique")
    op.drop_index("ix_runs_evaluation_id", table_name="runs")
    op.drop_column("runs", "repetition")
    op.drop_column("runs", "evaluation_id")
    op.drop_column("run_attempts", "sandbox_image_id")
    op.drop_table("scores")
    op.drop_table("attempt_evaluations")
    op.drop_table("attempt_artifacts")
    op.drop_table("evaluations")
    op.drop_table("datasets")
