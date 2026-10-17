"""Redis Streams dispatch.

Messages carry only a run_id. Redis is a delivery mechanism, never the source
of truth: the worker's conditional claim in Postgres decides who runs a job,
so a duplicate or stale message is harmless.
"""

import uuid
from dataclasses import dataclass
from typing import Any, cast

from redis.asyncio import Redis
from redis.exceptions import ResponseError

# Headroom between XREADGROUP's BLOCK and the client socket timeout.
BLOCKING_READ_HEADROOM_S = 5.0


def redis_for_blocking_reads(url: str, block_ms: int) -> Redis:
    """A client whose socket timeout outlasts a blocking XREADGROUP.

    redis-py >= 8 defaults socket_timeout to 5s, so a BLOCK of >= 5s would
    otherwise fail with "Timeout reading from ..." and drop the connection.
    """
    return Redis.from_url(url, socket_timeout=block_ms / 1000 + BLOCKING_READ_HEADROOM_S)


@dataclass(frozen=True)
class DispatchMessage:
    message_id: str
    run_id: uuid.UUID | None  # None if the payload was malformed


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _parse_entry(msg_id: Any, fields: dict[Any, Any] | None) -> DispatchMessage:
    raw = (fields or {}).get(b"run_id", (fields or {}).get("run_id"))
    try:
        run_id = uuid.UUID(_text(raw))
    except (ValueError, AttributeError):
        run_id = None
    return DispatchMessage(_text(msg_id), run_id)


@dataclass(frozen=True)
class AutoclaimResult:
    """XAUTOCLAIM's three-part reply (Redis 7.0+, G8)."""

    next_cursor: str  # "0-0" once the whole pending list has been scanned
    messages: list[DispatchMessage]  # entries now owned by the claiming consumer
    # Entries that were in the PEL but no longer exist in the stream (trimmed or
    # XDELed). Redis has already removed them from the PEL: they are never acked.
    deleted_ids: list[str]


@dataclass(frozen=True)
class ConsumerInfo:
    name: str
    pending: int
    idle_ms: int


class RunQueue:
    def __init__(
        self, redis: Redis, stream: str, group: str, *, maxlen: int | None = 100_000
    ) -> None:
        self._r = redis
        self.stream = stream
        self.group = group
        # Approximate trim on every XADD (reconciler step 7); None disables it.
        self.maxlen = maxlen

    async def ensure_group(self) -> None:
        """Create the consumer group (and stream) if missing. Idempotent."""
        try:
            await self._r.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def publish(self, run_id: uuid.UUID) -> str:
        # MAXLEN ~ N: the stream never grows without bound, and the trim is cheap
        # because Redis only drops whole macro nodes. Trimmed entries are always
        # safe to lose: Postgres is the truth and redispatch republishes.
        if self.maxlen is None:
            msg_id = await self._r.xadd(self.stream, {"run_id": str(run_id)})
        else:
            msg_id = await self._r.xadd(
                self.stream, {"run_id": str(run_id)}, maxlen=self.maxlen, approximate=True
            )
        return _text(msg_id)

    async def read(
        self, consumer: str, *, count: int = 1, block_ms: int = 5000
    ) -> list[DispatchMessage]:
        """Read new messages for this consumer (blocks up to block_ms)."""
        # Shape: [(stream, [(message_id, {field: value}), ...])] or None on timeout.
        resp = cast(
            list[tuple[bytes, list[tuple[bytes, dict[Any, Any]]]]] | None,
            await self._r.xreadgroup(
                self.group, consumer, {self.stream: ">"}, count=count, block=block_ms
            ),
        )
        messages: list[DispatchMessage] = []
        for _stream, entries in resp or []:
            for msg_id, fields in entries:
                messages.append(_parse_entry(msg_id, fields))
        return messages

    async def autoclaim(
        self, consumer: str, min_idle_ms: int, start_id: str = "0-0", count: int = 100
    ) -> AutoclaimResult:
        """XAUTOCLAIM entries pending for at least `min_idle_ms` to `consumer`.

        Never `justid=True`: that form drops the entry bodies this design needs
        (and, on some versions, the cursor). redis-py returns the three-part
        Redis 7 reply: [next_cursor, entries, deleted_ids] (G8).
        """
        resp = cast(
            list[Any],
            await self._r.xautoclaim(
                self.stream, self.group, consumer, min_idle_ms, start_id=start_id, count=count
            ),
        )
        cursor = _text(resp[0])
        messages = [_parse_entry(mid, fields) for mid, fields in resp[1] or []]
        deleted = [_text(d) for d in (resp[2] if len(resp) > 2 else None) or []]
        return AutoclaimResult(cursor, messages, deleted)

    async def consumers(self) -> list[ConsumerInfo]:
        """XINFO CONSUMERS: every consumer of the group with its pending count."""
        try:
            rows = cast(
                list[dict[Any, Any]], await self._r.xinfo_consumers(self.stream, self.group)
            )
        except ResponseError as exc:
            if "NOGROUP" in str(exc) or "no such key" in str(exc).lower():
                return []
            raise
        return [ConsumerInfo(_text(r["name"]), int(r["pending"]), int(r["idle"])) for r in rows]

    async def delete_consumer(self, name: str) -> int:
        """XGROUP DELCONSUMER; returns the number of pending entries it dropped
        (callers only delete consumers with none)."""
        return int(await self._r.xgroup_delconsumer(self.stream, self.group, name))

    async def ack(self, message_id: str) -> None:
        await self._r.xack(self.stream, self.group, message_id)
