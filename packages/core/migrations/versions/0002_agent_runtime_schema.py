"""agent runtime schema

Phase 3: agents, immutable agent_versions, trajectory_events; agent task type;
runs.agent_version_id; per-attempt agent aggregates; outcome CHECKs widened
for the agent outcomes (mirrors states.status_for).

Literals below are frozen on purpose: a migration must describe the schema at
this revision, not whatever states.py says later.

Downgrade refuses to run while agent data exists (the restored 0001
constraints would reject it); Postgres DDL is transactional, so a failed
downgrade leaves the schema at 0002 untouched.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# -- frozen constraint bodies ----------------------------------------------------
OUTCOMES_0001 = "'SUCCESS','NON_ZERO_EXIT','TIMEOUT','SANDBOX_ERROR','INTERNAL_ERROR'"
COMPLETED_0001 = "'SUCCESS','NON_ZERO_EXIT','TIMEOUT'"

OUTCOMES_0002 = (
    "'AGENT_FINISHED','BUDGET_EXCEEDED','INTERNAL_ERROR','MAX_STEPS','MODEL_ERROR',"
    "'MODEL_REFUSAL','NON_ZERO_EXIT','SANDBOX_ERROR','SUCCESS','TIMEOUT'"
)
COMPLETED_0002 = (
    "'AGENT_FINISHED','BUDGET_EXCEEDED','MAX_STEPS','MODEL_REFUSAL','NON_ZERO_EXIT',"
    "'SUCCESS','TIMEOUT'"
)
EVENT_TYPES = "'run_started','model_call','model_error','tool_call','limit_reached','run_finished'"
TOOL_STATUSES = "'ok','error','timeout','denied','invalid_input','unknown_tool'"


def _replace_check(table: str, name: str, condition: str) -> None:
    op.drop_constraint(op.f(f"ck_{table}_{name}"), table, type_="check")
    op.create_check_constraint(op.f(f"ck_{table}_{name}"), table, condition)


def _outcome_checks(outcomes: str, completed: str) -> None:
    _replace_check("runs", "outcome", f"outcome IN ({outcomes})")
    _replace_check(
        "runs", "completed_outcome", f"status <> 'COMPLETED' OR outcome IN ({completed})"
    )
    _replace_check("runs", "failed_outcome", f"status <> 'FAILED' OR outcome NOT IN ({completed})")
    _replace_check("run_attempts", "outcome", f"outcome IN ({outcomes})")


def upgrade() -> None:
    # -- agents / agent_versions ---------------------------------------------------
    op.create_table(
        "agents",
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agents")),
        sa.UniqueConstraint("name", name=op.f("uq_agents_name")),
    )
    op.create_table(
        "agent_versions",
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("model_provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("tools", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("shell_allowlist", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("limits", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("model_settings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_agent_versions_version")),
        sa.CheckConstraint(
            "model_provider IN ('anthropic','fake')", name=op.f("ck_agent_versions_model_provider")
        ),
        sa.CheckConstraint("length(config_hash) = 64", name=op.f("ck_agent_versions_config_hash")),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.id"], name=op.f("fk_agent_versions_agent_id_agents")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_versions")),
        sa.UniqueConstraint("agent_id", "version", name="uq_agent_versions_agent_id_version"),
        sa.UniqueConstraint(
            "agent_id", "config_hash", name="uq_agent_versions_agent_id_config_hash"
        ),
    )

    # -- tasks: agent task type ------------------------------------------------------
    op.add_column("tasks", sa.Column("instructions", sa.Text(), nullable=True))
    op.add_column(
        "tasks",
        sa.Column("test_command", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.alter_column(
        "tasks", "command", existing_type=postgresql.JSONB(astext_type=sa.Text()), nullable=True
    )
    _replace_check("tasks", "task_type", "task_type IN ('command','agent')")
    _replace_check(
        "tasks",
        "timeout_s",
        "timeout_s >= 1 AND timeout_s <= CASE task_type WHEN 'agent' THEN 1800 ELSE 300 END",
    )
    op.create_check_constraint(
        op.f("ck_tasks_command"), "tasks", "(task_type = 'command') = (command IS NOT NULL)"
    )
    op.create_check_constraint(
        op.f("ck_tasks_instructions"), "tasks", "task_type <> 'agent' OR instructions IS NOT NULL"
    )
    op.create_check_constraint(
        op.f("ck_tasks_test_command"), "tasks", "task_type = 'agent' OR test_command IS NULL"
    )

    # -- runs: agent version -----------------------------------------------------------
    op.add_column("runs", sa.Column("agent_version_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_runs_agent_version_id_agent_versions"),
        "runs",
        "agent_versions",
        ["agent_version_id"],
        ["id"],
    )
    op.create_index("ix_runs_agent_version_id", "runs", ["agent_version_id"], unique=False)

    # -- run_attempts: agent aggregates ----------------------------------------------
    op.add_column("run_attempts", sa.Column("steps", sa.Integer(), nullable=True))
    op.add_column("run_attempts", sa.Column("tokens_in", sa.BigInteger(), nullable=True))
    op.add_column("run_attempts", sa.Column("tokens_out", sa.BigInteger(), nullable=True))
    op.add_column(
        "run_attempts", sa.Column("cost_usd", sa.Numeric(precision=12, scale=6), nullable=True)
    )
    op.add_column("run_attempts", sa.Column("final_answer", sa.Text(), nullable=True))
    op.create_check_constraint(op.f("ck_run_attempts_steps"), "run_attempts", "steps >= 0")
    op.create_check_constraint(
        op.f("ck_run_attempts_tokens"), "run_attempts", "tokens_in >= 0 AND tokens_out >= 0"
    )
    op.create_check_constraint(op.f("ck_run_attempts_cost_usd"), "run_attempts", "cost_usd >= 0")

    # -- widened outcome checks (status_for: agent results COMPLETE) -------------------
    _outcome_checks(OUTCOMES_0002, COMPLETED_0002)

    # -- trajectory_events -------------------------------------------------------------
    op.create_table(
        "trajectory_events",
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("step", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("tool", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=True),
        sa.Column("error_class", sa.String(length=64), nullable=True),
        sa.Column("input", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("output", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("tokens_in", sa.Integer(), nullable=True),
        sa.Column("tokens_out", sa.Integer(), nullable=True),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column(
            "ts", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            f"event_type IN ({EVENT_TYPES})", name=op.f("ck_trajectory_events_event_type")
        ),
        sa.CheckConstraint(
            f"status IN ({TOOL_STATUSES})", name=op.f("ck_trajectory_events_status")
        ),
        sa.CheckConstraint(
            "(event_type = 'tool_call') = (tool IS NOT NULL)",
            name=op.f("ck_trajectory_events_tool_iff_tool_call"),
        ),
        sa.CheckConstraint(
            "(event_type = 'tool_call') = (status IS NOT NULL)",
            name=op.f("ck_trajectory_events_status_iff_tool_call"),
        ),
        sa.CheckConstraint("seq >= 0 AND step >= 0", name=op.f("ck_trajectory_events_seq_step")),
        sa.CheckConstraint(
            "tokens_in >= 0 AND tokens_out >= 0 AND cache_read_tokens >= 0 "
            "AND cache_write_tokens >= 0",
            name=op.f("ck_trajectory_events_tokens"),
        ),
        sa.CheckConstraint("cost_usd >= 0", name=op.f("ck_trajectory_events_cost_usd")),
        sa.CheckConstraint("duration_ms >= 0", name=op.f("ck_trajectory_events_duration_ms")),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["run_attempts.id"],
            name=op.f("fk_trajectory_events_attempt_id_run_attempts"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trajectory_events")),
        sa.UniqueConstraint("attempt_id", "seq", name="uq_trajectory_events_attempt_id_seq"),
    )


def downgrade() -> None:
    op.drop_table("trajectory_events")

    # Fails (and rolls back) if any run/attempt carries an agent outcome.
    _outcome_checks(OUTCOMES_0001, COMPLETED_0001)

    op.drop_constraint(op.f("ck_run_attempts_cost_usd"), "run_attempts", type_="check")
    op.drop_constraint(op.f("ck_run_attempts_tokens"), "run_attempts", type_="check")
    op.drop_constraint(op.f("ck_run_attempts_steps"), "run_attempts", type_="check")
    for column in ("final_answer", "cost_usd", "tokens_out", "tokens_in", "steps"):
        op.drop_column("run_attempts", column)

    op.drop_index("ix_runs_agent_version_id", table_name="runs")
    op.drop_constraint(op.f("fk_runs_agent_version_id_agent_versions"), "runs", type_="foreignkey")
    op.drop_column("runs", "agent_version_id")

    # Fails (and rolls back) if any agent task exists.
    op.drop_constraint(op.f("ck_tasks_test_command"), "tasks", type_="check")
    op.drop_constraint(op.f("ck_tasks_instructions"), "tasks", type_="check")
    op.drop_constraint(op.f("ck_tasks_command"), "tasks", type_="check")
    _replace_check("tasks", "timeout_s", "timeout_s BETWEEN 1 AND 300")
    _replace_check("tasks", "task_type", "task_type IN ('command')")
    op.alter_column(
        "tasks", "command", existing_type=postgresql.JSONB(astext_type=sa.Text()), nullable=False
    )
    op.drop_column("tasks", "test_command")
    op.drop_column("tasks", "instructions")

    op.drop_table("agent_versions")
    op.drop_table("agents")
