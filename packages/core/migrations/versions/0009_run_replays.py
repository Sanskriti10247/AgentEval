"""run_replays: the idempotency record of POST /runs/{id}/replay

Step 9 (R14, F10, G4). A replay moves a FAILED run back to QUEUED with a larger
attempt budget. The request may carry an Idempotency-Key, and a retried
request must not replay the run twice, so every replay leaves a row here in the
same transaction as the state change:

    run_replays (id, run_id, idempotency_key UNIQUE, replay_no,
                 additional_attempts, created_at)

The key is unique across all replays (NULLs are distinct, so a keyless replay
never collides), and (run_id, replay_no) is unique so two replays of one run can
never claim the same number. The table is new, so there is nothing to backfill.

The downgrade refuses while any row exists: dropping the table would forget which
requests were already applied, and a client retrying one afterwards would replay
the run a second time.

Literals are frozen on purpose, as in 0002-0008.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "run_replays",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("replay_no", sa.SmallInteger(), nullable=False),
        sa.Column("additional_attempts", sa.SmallInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("replay_no >= 1", name=op.f("ck_run_replays_replay_no")),
        sa.CheckConstraint(
            "additional_attempts BETWEEN 1 AND 9", name=op.f("ck_run_replays_additional_attempts")
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("fk_run_replays_run_id_runs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_replays")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_run_replays_idempotency_key")),
        sa.UniqueConstraint("run_id", "replay_no", name=op.f("uq_run_replays_run_id_replay_no")),
    )


def downgrade() -> None:
    n = op.get_bind().execute(sa.text("SELECT count(*) FROM run_replays")).scalar_one()
    if n:
        raise RuntimeError(
            f"refusing to downgrade 0009: {n} replay(s) are recorded. Dropping run_replays "
            "would forget which replay requests were applied, so a retried request could "
            "replay its run a second time; remove the rows first."
        )
    op.drop_table("run_replays")
