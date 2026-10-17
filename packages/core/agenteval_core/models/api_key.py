from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, LargeBinary, String, Text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from agenteval_core.models.base import Base, CreatedAt, UUIDPk


class ApiKey(UUIDPk, CreatedAt, Base):
    """An API key (ADR-012 A). Only the SHA-256 of the secret is stored; the
    plaintext exists once, in the response to the call that created it."""

    __tablename__ = "api_keys"
    __table_args__ = (
        CheckConstraint("octet_length(secret_sha256) = 32", name="secret_sha256_len"),
        CheckConstraint(
            "cardinality(scopes) >= 1 AND scopes <@ ARRAY['read','write','admin']::text[]",
            name="scopes",
        ),
        CheckConstraint("prefix ~ '^[0-9a-f]{8}$'", name="prefix_format"),
    )

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    prefix: Mapped[str] = mapped_column(String(8), nullable=False, unique=True)
    secret_sha256: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
