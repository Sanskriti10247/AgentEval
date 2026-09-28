import time

from agenteval_core.config import Settings
from agenteval_core.queue.streams import RunQueue, redis_for_blocking_reads


async def test_blocking_read_longer_than_redis_default_socket_timeout(settings: Settings) -> None:
    """Regression: redis-py 8 defaults socket_timeout=5s, which broke BLOCK >= 5s."""
    redis = redis_for_blocking_reads(settings.redis_url, block_ms=5500)
    try:
        queue = RunQueue(redis, settings.dispatch_stream, settings.consumer_group)
        await queue.ensure_group()
        t0 = time.monotonic()
        assert await queue.read("c", block_ms=5500) == []
        assert time.monotonic() - t0 >= 5.4
    finally:
        await redis.aclose()


async def test_ensure_group_is_idempotent(queue: RunQueue) -> None:
    await queue.ensure_group()
    await queue.ensure_group()
