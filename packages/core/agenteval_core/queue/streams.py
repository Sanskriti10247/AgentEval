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


class RunQueue:
    def __init__(self, redis: Redis, stream: str, group: str) -> None:
        self._r = redis
        self.stream = stream
        self.group = group

    async def ensure_group(self) -> None:
        """Create the consumer group (and stream) if missing. Idempotent."""
        try:
            await self._r.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def publish(self, run_id: uuid.UUID) -> str:
        msg_id = await self._r.xadd(self.stream, {"run_id": str(run_id)})
        return msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)

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
                mid = msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)
                raw = fields.get(b"run_id", fields.get("run_id"))
                try:
                    run_id = uuid.UUID(raw.decode() if isinstance(raw, bytes) else str(raw))
                except (ValueError, AttributeError):
                    run_id = None
                messages.append(DispatchMessage(mid, run_id))
        return messages

    async def ack(self, message_id: str) -> None:
        await self._r.xack(self.stream, self.group, message_id)
