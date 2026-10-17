"""workers stop reason

Step 5: the reconciler marks a worker that stopped heartbeating as lost
(G3, reconciler step 7), so a stopped row must say how it stopped:

    stop_reason IN ('shutdown', 'lost')
    (stop_reason IS NULL) = (stopped_at IS NULL)

Backfill first: every row already stopped was stopped by a graceful shutdown
(the only writer before this step), so it becomes 'shutdown'. Rows still
running keep NULL.

The downgrade refuses while any worker is 'lost': dropping the column would
silently turn a crash record into a clean shutdown.

Literals are frozen on purpose, as in 0002-0006.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STOP_REASON_VALUES = "stop_reason IN ('shutdown','lost')"
STOP_REASON_IFF_STOPPED = "(stop_reason IS NULL) = (stopped_at IS NULL)"


def upgrade() -> None:
    op.add_column("workers", sa.Column("stop_reason", sa.String(length=16), nullable=True))
    op.execute("UPDATE workers SET stop_reason = 'shutdown' WHERE stopped_at IS NOT NULL")
    op.create_check_constraint(op.f("ck_workers_stop_reason"), "workers", STOP_REASON_VALUES)
    op.create_check_constraint(
        op.f("ck_workers_stop_reason_iff_stopped"), "workers", STOP_REASON_IFF_STOPPED
    )


def downgrade() -> None:
    lost = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM workers WHERE stop_reason = 'lost'"))
        .scalar_one()
    )
    if lost:
        raise RuntimeError(
            f"refusing to downgrade 0007: {lost} worker(s) are recorded as lost. "
            "Dropping stop_reason would turn those crashes into clean shutdowns; "
            "remove the rows first."
        )
    op.drop_constraint(op.f("ck_workers_stop_reason_iff_stopped"), "workers", type_="check")
    op.drop_constraint(op.f("ck_workers_stop_reason"), "workers", type_="check")
    op.drop_column("workers", "stop_reason")
