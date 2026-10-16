"""attempt failure_reason required

Step 3: finishes now record a failure_reason (and retryable) on every FINISHED
attempt with a failed outcome, via T-finish (p5_finish/p5_cancel, migration
0004). This migration adds the CHECK that migration 0004's docstring deferred
("the attempt failure_reason presence rule"):

    (failure_reason IS NOT NULL) = (
        status IN ('ABANDONED', 'CANCELLED')
        OR (status = 'FINISHED' AND outcome IN ('INTERNAL_ERROR', 'MODEL_ERROR', 'SANDBOX_ERROR'))
    )

ABANDONED and CANCELLED attempts already require a reason under migration
0004's own CHECKs (ck_run_attempts_abandoned, ck_run_attempts_cancelled); this
is the first migration to require one for a FINISHED attempt with a failed
outcome too, now that the write path always supplies it.

Literals are frozen on purpose, as in 0002-0004.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-16
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FAILED_OUTCOMES = "'INTERNAL_ERROR','MODEL_ERROR','SANDBOX_ERROR'"
REASON_PRESENCE = (
    "(failure_reason IS NOT NULL) = ("
    "status IN ('ABANDONED','CANCELLED') "
    f"OR (status = 'FINISHED' AND outcome IN ({FAILED_OUTCOMES})))"
)


def upgrade() -> None:
    # Backfill first: any FINISHED+failed attempt written before Step 3's code
    # started recording a reason (e.g. during a blue-green deploy against an
    # already-migrated-to-0004 database) gets the same legacy classification
    # migration 0004 gave pre-Phase-5 rows.
    op.execute(
        "UPDATE run_attempts SET failure_reason = 'unclassified', retryable = false"
        f" WHERE status = 'FINISHED' AND outcome IN ({FAILED_OUTCOMES})"
        " AND failure_reason IS NULL"
    )
    op.create_check_constraint(op.f("ck_run_attempts_reason"), "run_attempts", REASON_PRESENCE)


def downgrade() -> None:
    op.drop_constraint(op.f("ck_run_attempts_reason"), "run_attempts", type_="check")
