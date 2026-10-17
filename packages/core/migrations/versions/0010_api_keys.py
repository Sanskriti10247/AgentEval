"""api_keys: API-key authentication (ADR-012 A)

Phase 8. Callers identify themselves with an opaque API key. Only a digest is kept:

    api_keys (id, name, prefix UNIQUE, secret_sha256, scopes text[], created_at,
              last_used_at, expires_at NULL, revoked_at NULL)

`prefix` (8 hex characters) is the non-secret lookup handle; `secret_sha256` is the
32-byte SHA-256 of the 256-bit random secret (no KDF needed, ADR-012 A). `scopes` is a
non-empty subset of read / write / admin. The table is new, so there is nothing to
backfill, and no key exists until an operator creates one (python -m agenteval_api.keys).

The downgrade refuses while any key exists: dropping the table would silently lock
every client out (or, with authentication switched off, discard the audit of who held
access), so remove the keys deliberately first.

Literals are frozen on purpose, as in 0002-0009.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("prefix", sa.String(length=8), nullable=False),
        sa.Column("secret_sha256", sa.LargeBinary(), nullable=False),
        sa.Column("scopes", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "octet_length(secret_sha256) = 32", name=op.f("ck_api_keys_secret_sha256_len")
        ),
        sa.CheckConstraint(
            "cardinality(scopes) >= 1 AND scopes <@ ARRAY['read','write','admin']::text[]",
            name=op.f("ck_api_keys_scopes"),
        ),
        sa.CheckConstraint("prefix ~ '^[0-9a-f]{8}$'", name=op.f("ck_api_keys_prefix_format")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_keys")),
        sa.UniqueConstraint("prefix", name=op.f("uq_api_keys_prefix")),
    )


def downgrade() -> None:
    n = op.get_bind().execute(sa.text("SELECT count(*) FROM api_keys")).scalar_one()
    if n:
        raise RuntimeError(
            f"refusing to downgrade 0010: {n} API key(s) exist. Dropping api_keys would lock "
            "every client out and forget who held access; revoke and delete the keys first."
        )
    op.drop_table("api_keys")
