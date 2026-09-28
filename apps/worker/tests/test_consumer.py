"""Consumer shutdown behaviour (no Docker, no Redis)."""

import asyncio
import uuid

from agenteval_core.queue.streams import DispatchMessage
from agenteval_worker.consumer import Consumer


class OneMessageQueue:
    def __init__(self) -> None:
        self.sent = False

    async def ensure_group(self) -> None:
        pass

    async def read(self, consumer: str, *, count: int, block_ms: int) -> list[DispatchMessage]:
        await asyncio.sleep(0.01)
        if self.sent:
            await asyncio.sleep(block_ms / 1000)
            return []
        self.sent = True
        return [DispatchMessage("1-0", uuid.uuid4())]


class LongRuns:
    """Each run takes an hour unless cancelled cooperatively."""

    worker_id = "w"

    def __init__(self) -> None:
        self.cancel_reasons: list[str] = []
        self._cancel = asyncio.Event()

    async def handle(self, msg: DispatchMessage) -> None:
        try:
            await asyncio.wait_for(self._cancel.wait(), 3600)
        except TimeoutError:
            pass

    def cancel_all(self, reason: str) -> int:
        self.cancel_reasons.append(reason)
        self._cancel.set()
        return 1


async def test_stop_starts_the_drain_even_when_every_slot_is_busy() -> None:
    """Regression: the loop used to block in sem.acquire() until a run finished."""
    ex = LongRuns()
    c = Consumer(OneMessageQueue(), ex, concurrency=1, block_ms=50,  # type: ignore[arg-type]
                 drain_timeout_s=0.5, cancel_grace_s=0.2)  # fmt: skip
    stop = asyncio.Event()
    task = asyncio.create_task(c.run(stop))
    await asyncio.sleep(0.2)  # the only slot is now held by an hour-long run
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    stop.set()
    await asyncio.wait_for(task, 3)
    assert loop.time() - t0 < 1.5  # soft wait 0.3s, then cooperative cancel
    assert ex.cancel_reasons == ["worker_shutdown"]


async def test_hard_cancel_after_the_grace_if_cancellation_is_ignored() -> None:
    class Stubborn(LongRuns):
        def cancel_all(self, reason: str) -> int:
            self.cancel_reasons.append(reason)
            return 1  # ...but the run ignores it

    ex = Stubborn()
    c = Consumer(OneMessageQueue(), ex, concurrency=1, block_ms=50,  # type: ignore[arg-type]
                 drain_timeout_s=0.5, cancel_grace_s=0.2)  # fmt: skip
    stop = asyncio.Event()
    task = asyncio.create_task(c.run(stop))
    await asyncio.sleep(0.2)
    stop.set()
    await asyncio.wait_for(task, 3)  # total wait is still bounded by drain_timeout_s
    assert ex.cancel_reasons == ["worker_shutdown"]
