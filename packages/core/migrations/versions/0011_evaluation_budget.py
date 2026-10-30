"""evaluation budget: evaluations.max_cost_usd and the evaluation_budget_exhausted reason

An evaluation may carry a spending cap for all of its runs together:

    evaluations.max_cost_usd NUMERIC(12,6) NULL, CHECK (max_cost_usd > 0)

NULL means no cap (every existing row, and fake-model evaluations that name none). Before a
run of a capped evaluation starts its agent loop, the worker sums cost_usd over the
evaluation's attempts; at or above the cap the attempt finishes INTERNAL_ERROR with the new
attempt reason `evaluation_budget_exhausted`, so ck_run_attempts_failure_reason_values is
rebuilt with that value added. Both changes are additive: nothing is backfilled.

The downgrade refuses while any attempt carries the new reason (the old CHECK would reject
the row); the column and its values are dropped.

Literals are frozen on purpose, as in 0002-0010.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ATTEMPT_REASONS_0004 = (
    "'cancelled_by_batch','cancelled_by_user','config_invalid','config_model_unpriced',"
    "'config_provider_disabled','internal_unexpected','lease_expired','model_authentication',"
    "'model_bad_request','model_conflict','model_connection','model_not_found',"
    "'model_overloaded','model_permission','model_protocol','model_rate_limited',"
    "'model_request_too_large','model_server_error','model_timeout','persistence_trajectory',"
    "'sandbox_create_failed','sandbox_exec_failed','sandbox_image_not_found','sandbox_lost',"
    "'sandbox_reaper_failed','unclassified','worker_backstop','worker_capability',"
    "'worker_shutdown'"
)
ATTEMPT_REASONS = (
    "'cancelled_by_batch','cancelled_by_user','config_invalid','config_model_unpriced',"
    "'config_provider_disabled','evaluation_budget_exhausted','internal_unexpected',"
    "'lease_expired','model_authentication','model_bad_request','model_conflict',"
    "'model_connection','model_not_found','model_overloaded','model_permission',"
    "'model_protocol','model_rate_limited','model_request_too_large','model_server_error',"
    "'model_timeout','persistence_trajectory','sandbox_create_failed','sandbox_exec_failed',"
    "'sandbox_image_not_found','sandbox_lost','sandbox_reaper_failed','unclassified',"
    "'worker_backstop','worker_capability','worker_shutdown'"
)


def _replace_reasons(values: str) -> None:
    name = op.f("ck_run_attempts_failure_reason_values")
    op.drop_constraint(name, "run_attempts", type_="check")
    op.create_check_constraint(name, "run_attempts", f"failure_reason IN ({values})")


def upgrade() -> None:
    op.add_column("evaluations", sa.Column("max_cost_usd", sa.Numeric(12, 6), nullable=True))
    op.create_check_constraint(
        op.f("ck_evaluations_max_cost_usd"), "evaluations", "max_cost_usd > 0"
    )
    _replace_reasons(ATTEMPT_REASONS)


def downgrade() -> None:
    n = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM run_attempts"
                " WHERE failure_reason = 'evaluation_budget_exhausted'"
            )
        )
        .scalar_one()
    )
    if n:
        raise RuntimeError(
            f"refusing to downgrade 0011: {n} attempt(s) failed with "
            "evaluation_budget_exhausted, which the 0010 schema cannot store. Delete or "
            "reclassify those attempts first."
        )
    _replace_reasons(ATTEMPT_REASONS_0004)
    op.drop_constraint(op.f("ck_evaluations_max_cost_usd"), "evaluations", type_="check")
    op.drop_column("evaluations", "max_cost_usd")
