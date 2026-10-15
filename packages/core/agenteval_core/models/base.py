import uuid
from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum

import uuid_utils.compat
from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


# For nullable JSONB columns: store Python None as SQL NULL. The default
# (none_as_null=False) stores the JSON value `null`, which is NOT NULL to
# Postgres and silently defeats `IS NULL` checks such as ck_tasks_command.
NullableJSONB = JSONB(none_as_null=True)


def sql_values(values: Iterable[StrEnum]) -> str:
    """Enum values as a sorted SQL IN-list body, so CHECKs can't drift from code."""
    return ",".join(f"'{v.value}'" for v in sorted(values, key=lambda v: v.value))


def new_id() -> uuid.UUID:
    """UUIDv7: time-ordered, so primary-key inserts stay index-friendly."""
    return uuid_utils.compat.uuid7()


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UUIDPk:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)


class CreatedAt:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
