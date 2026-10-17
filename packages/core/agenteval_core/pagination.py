"""Keyset-pagination cursors shared by every list endpoint.

A cursor is `<created_at, UTC, microseconds, Z>|<id>`: no '+', so it survives
an unencoded query string. Clients treat it as opaque. Lists are ordered
newest first on (created_at DESC, id DESC); the next page is every row strictly
before the cursor's pair, so pages never overlap and never skip a row even
while new rows are inserted.
"""

import uuid
from datetime import UTC, datetime


def format_stamp(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_stamp(stamp: str) -> datetime:
    ts = datetime.fromisoformat(stamp)
    if ts.tzinfo is None:
        raise ValueError("naive timestamp")
    return ts


def encode_cursor(created_at: datetime, row_id: uuid.UUID) -> str:
    return f"{format_stamp(created_at)}|{row_id}"


def decode_cursor(after: str) -> tuple[datetime, uuid.UUID]:
    """Raises ValueError on anything malformed; callers pick the API error."""
    stamp, row_id = after.split("|", 1)
    return parse_stamp(stamp), uuid.UUID(row_id)


def split_page[T](rows: list[T], limit: int) -> tuple[list[T], bool]:
    """Repositories fetch `limit + 1` rows; the extra one says another page exists."""
    return rows[:limit], len(rows) > limit
