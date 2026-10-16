"""runs lease check

Step 4: claims now set lease_expires_at (and heartbeat_at) alongside the
epoch bump (R1), so this migration adds the CHECK migration 0004's docstring
deferred for this step:

    (status IN ('CLAIMED','RUNNING')) = (lease_expires_at IS NOT NULL)

Backfill first, exactly as 0004 already did for its own (looser) purposes:
any CLAIMED/RUNNING row still missing a lease (none should exist once Step 4's
claim path is live, but a blue-green deploy window, or a row written directly
by a test, can leave one) gets lease_expires_at = now(). Such a row becomes
recoverable on the reconciler's first tick (Step 5), exactly like 0004's own
backfill of the same column.

Literals are frozen on purpose, as in 0002-0005.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-16
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEASE_IFF_CLAIMED_OR_RUNNING = "(status IN ('CLAIMED','RUNNING')) = (lease_expires_at IS NOT NULL)"


def upgrade() -> None:
    op.execute(
        "UPDATE runs SET lease_expires_at = now()"
        " WHERE status IN ('CLAIMED','RUNNING') AND lease_expires_at IS NULL"
    )
    op.create_check_constraint(op.f("ck_runs_lease"), "runs", LEASE_IFF_CLAIMED_OR_RUNNING)


def downgrade() -> None:
    op.drop_constraint(op.f("ck_runs_lease"), "runs", type_="check")
