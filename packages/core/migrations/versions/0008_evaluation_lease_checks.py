"""attempt_evaluations lease and error-reason checks

Step 7: grading now claims an evaluation with a lease (E4) and every ERROR row
carries a structured reason, so this migration adds the two CHECKs migration
0004's docstring deferred for this step:

    ck_attempt_evaluations_lease          (status = 'RUNNING') = (lease_expires_at IS NOT NULL)
    ck_attempt_evaluations_error_reason   status <> 'ERROR' OR error_reason IS NOT NULL

(a PENDING row may still carry the reason of the attempt that failed, G13, so
the reason rule is one-directional.)

Backfill first, as 0004 and 0006 did for their own columns. 0004 already gave
every legacy RUNNING row a lease and every legacy ERROR row 'unclassified', so
on a database migrated through 0004 these statements match nothing; they exist
for rows written in between (a blue-green deploy window, or a row written
directly by a test):

* a RUNNING row without a lease gets lease_expires_at = now(), so the
  reconciler's evaluation recovery (step 2) picks it up on its first tick;
* an ERROR row without a reason gets 'unclassified', and error_kind
  'non_retryable' if it had none.

The downgrade only drops the two CHECKs; no data is touched or lost.

Literals are frozen on purpose, as in 0002-0007.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-17
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEASE_IFF_RUNNING = "(status = 'RUNNING') = (lease_expires_at IS NOT NULL)"
ERROR_HAS_REASON = "status <> 'ERROR' OR error_reason IS NOT NULL"


def upgrade() -> None:
    op.execute(
        "UPDATE attempt_evaluations SET lease_expires_at = now()"
        " WHERE status = 'RUNNING' AND lease_expires_at IS NULL"
    )
    op.execute(
        "UPDATE attempt_evaluations SET error_reason = 'unclassified',"
        " error_kind = COALESCE(error_kind, 'non_retryable')"
        " WHERE status = 'ERROR' AND error_reason IS NULL"
    )
    op.create_check_constraint(
        op.f("ck_attempt_evaluations_lease"), "attempt_evaluations", LEASE_IFF_RUNNING
    )
    op.create_check_constraint(
        op.f("ck_attempt_evaluations_error_reason"), "attempt_evaluations", ERROR_HAS_REASON
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_attempt_evaluations_error_reason"), "attempt_evaluations", type_="check"
    )
    op.drop_constraint(op.f("ck_attempt_evaluations_lease"), "attempt_evaluations", type_="check")
