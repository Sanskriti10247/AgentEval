"""Fixed-window rate limiting in Redis (ADR-012 C).

One round trip per check: a Lua script does `INCR`, sets the window's `EXPIRE` on the
first hit (and repairs a counter that somehow lost its expiry), and returns the count
and the milliseconds left. Atomic, so concurrent API processes share one counter and
no key can be left without a TTL.

Fixed windows allow a burst of up to twice the budget across a window boundary. That
is accepted here: the limit protects the service from runaway clients, it is not a
billing meter, and a sliding window would cost more Redis work per request.

Redis is a coordination aid (ADR-001), not an authority: if it cannot be reached the
check FAILS OPEN (the request is allowed), logs, and counts
`agenteval_rate_limit_errors_total`, so a Redis outage does not take reads down.
"""

import asyncio
import math
from dataclasses import dataclass

from redis.asyncio import Redis

from agenteval_core.logging import get_logger
from agenteval_core.observability import metrics

log = get_logger(__name__)

WINDOW_S = 60
CHECK_TIMEOUT_S = 1.0

_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
local ttl = redis.call('PTTL', KEYS[1])
if count == 1 or ttl < 0 then
  redis.call('PEXPIRE', KEYS[1], ARGV[1])
  ttl = tonumber(ARGV[1])
end
return {count, ttl}
"""


@dataclass(frozen=True)
class Decision:
    allowed: bool
    limit: int
    remaining: int
    reset_s: int  # whole seconds until the window ends (>= 1)

    def headers(self) -> dict[str, str]:
        return {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(self.remaining),
            "X-RateLimit-Reset": str(self.reset_s),
        }


class RateLimiter:
    def __init__(self, redis: Redis, *, window_s: float = WINDOW_S) -> None:
        self._script = redis.register_script(_SCRIPT)
        self._window_ms = int(window_s * 1000)

    async def check(self, kind: str, subject: str, limit: int) -> Decision | None:
        """Count one request against `subject`'s `kind` budget. None when the limit is 0
        (disabled) or Redis is unreachable (fail open): no decision, no headers."""
        if limit <= 0:
            return None
        try:
            count, ttl_ms = await asyncio.wait_for(
                self._script(keys=[f"ratelimit:{kind}:{subject}"], args=[self._window_ms]),
                CHECK_TIMEOUT_S,
            )
        except Exception as exc:  # noqa: BLE001 - any failure must allow the request
            metrics.rate_limit_error()
            log.warning("rate_limit_unavailable", kind=kind, error_type=type(exc).__name__)
            return None
        count, ttl_ms = int(count), int(ttl_ms)
        return Decision(
            allowed=count <= limit,
            limit=limit,
            remaining=max(limit - count, 0),
            reset_s=max(math.ceil(ttl_ms / 1000), 1),
        )
