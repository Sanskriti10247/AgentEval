"""create tasks runs run_attempts

Revision ID: 0001
Revises:
Create Date: 2026-09-28 15:57:59.738778
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("task_type", sa.String(length=32), nullable=False),
        sa.Column("image", sa.String(length=255), nullable=False),
        sa.Column("command", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("files", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("timeout_s", sa.Integer(), nullable=False),
        sa.Column("memory_mb", sa.Integer(), nullable=False),
        sa.Column("cpu_limit", sa.Float(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("task_type IN ('command')", name=op.f("ck_tasks_task_type")),
        sa.CheckConstraint(
            "cpu_limit >= 0.1 AND cpu_limit <= 2.0", name=op.f("ck_tasks_cpu_limit")
        ),
        sa.CheckConstraint("memory_mb BETWEEN 64 AND 1024", name=op.f("ck_tasks_memory_mb")),
        sa.CheckConstraint("timeout_s BETWEEN 1 AND 300", name=op.f("ck_tasks_timeout_s")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tasks")),
    )
    op.create_table(
        "runs",
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(length=255), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
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
            "(status IN ('COMPLETED','FAILED')) = (outcome IS NOT NULL)",
            name=op.f("ck_runs_outcome_iff_terminal"),
        ),
        sa.CheckConstraint(
            "outcome IN ('SUCCESS','NON_ZERO_EXIT','TIMEOUT','SANDBOX_ERROR','INTERNAL_ERROR')",
            name=op.f("ck_runs_outcome"),
        ),
        sa.CheckConstraint(
            "status <> 'COMPLETED' OR outcome IN ('SUCCESS','NON_ZERO_EXIT','TIMEOUT')",
            name=op.f("ck_runs_completed_outcome"),
        ),
        sa.CheckConstraint(
            "status <> 'FAILED' OR outcome NOT IN ('SUCCESS','NON_ZERO_EXIT','TIMEOUT')",
            name=op.f("ck_runs_failed_outcome"),
        ),
        sa.CheckConstraint(
            "status IN ('QUEUED','CLAIMED','RUNNING','COMPLETED','FAILED')",
            name=op.f("ck_runs_status"),
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], name=op.f("fk_runs_task_id_tasks")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runs")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_runs_idempotency_key")),
    )
    op.create_index("ix_runs_status_created_at", "runs", ["status", "created_at"], unique=False)
    op.create_index("ix_runs_task_id", "runs", ["task_id"], unique=False)
    op.create_table(
        "run_attempts",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("oom_killed", sa.Boolean(), nullable=False),
        sa.Column("stdout", sa.Text(), nullable=False),
        sa.Column("stderr", sa.Text(), nullable=False),
        sa.Column("stdout_truncated", sa.Boolean(), nullable=False),
        sa.Column("stderr_truncated", sa.Boolean(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "(status = 'FINISHED') = (outcome IS NOT NULL)",
            name=op.f("ck_run_attempts_outcome_iff_finished"),
        ),
        sa.CheckConstraint(
            "outcome IN ('SUCCESS','NON_ZERO_EXIT','TIMEOUT','SANDBOX_ERROR','INTERNAL_ERROR')",
            name=op.f("ck_run_attempts_outcome"),
        ),
        sa.CheckConstraint("status IN ('RUNNING','FINISHED')", name=op.f("ck_run_attempts_status")),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("fk_run_attempts_run_id_runs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_attempts")),
        sa.UniqueConstraint("run_id", "attempt_no", name="uq_run_attempts_run_id_attempt_no"),
    )


def downgrade() -> None:
    op.drop_table("run_attempts")
    op.drop_index("ix_runs_task_id", table_name="runs")
    op.drop_index("ix_runs_status_created_at", table_name="runs")
    op.drop_table("runs")
    op.drop_table("tasks")
