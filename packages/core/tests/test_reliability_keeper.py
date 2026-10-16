"""The lease keeper's own logic (ADR-008 D): message handling, batched
renewal, the liveness gate, and exit on parent death or a closed pipe. A fake
backend and a fake pipe stand in for asyncpg and `multiprocessing.Pipe()` -
see `keeper_process.py` for the real ones."""

import asyncio
import os
from collections.abc import Mapping

import pytest

from agenteval_core.reliability import keeper as keeper_mod
from agenteval_core.reliability.fencing import LeaseTiming
from agenteval_core.reliability.keeper import LeaseKeeper, _StopKeeper
from agenteval_core.reliability.keeper_protocol import Alive, Beat, Hold, Ping, Release, Stop

T = LeaseTiming(
    ttl_s=2.0, heartbeat_s=0.5, margin_s=0.5, reconcile_interval_s=0.2, liveness_timeout_s=1.0
)


class FakePipe:
    """A duck-typed `multiprocessing.connection.Connection`: one inbox the
    test fills, one outbox the keeper writes to."""

    def __init__(self) -> None:
        self.inbox: list[object] = []
        self.outbox: list[object] = []
        self.closed = False

    def poll(self, timeout: float = 0.0) -> bool:
        if self.closed:
            raise EOFError
        return bool(self.inbox)

    def recv(self) -> object:
        if self.closed:
            raise EOFError
        return self.inbox.pop(0)

    def send(self, obj: object) -> None:
        self.outbox.append(obj)


class FakeBackend:
    """Renews everything whose (id, epoch) is in `rows`; reports its cancel
    reason (or None). Raises if `fail` is set, to exercise the error path."""

    def __init__(self, rows: dict[tuple[str, int], str | None] | None = None) -> None:
        self.rows = rows or {}
        self.fail: Exception | None = None
        self.calls: list[tuple[str, list[tuple[str, int]], float]] = []

    async def renew(
        self, kind: str, items: list[tuple[str, int]], ttl_s: float
    ) -> Mapping[str, str | None]:
        self.calls.append((kind, list(items), ttl_s))
        if self.fail is not None:
            raise self.fail
        return {id_: self.rows[(id_, epoch)] for id_, epoch in items if (id_, epoch) in self.rows}


@pytest.fixture(autouse=True)
def _fast_alive_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    # keeper.run() sleeps ALIVE_INTERVAL_S before every tick; shrink it so
    # the exit-condition tests don't each cost a full second.
    monkeypatch.setattr(keeper_mod, "ALIVE_INTERVAL_S", 0.01)


def test_hold_then_release_tracks_exactly_what_is_held() -> None:
    keeper = LeaseKeeper(FakeBackend(), FakePipe(), T)
    keeper.handle_message(Hold("run", "r1", 1))
    keeper.handle_message(Hold("evaluation", "e1", 7))
    assert keeper.held == {("run", "r1"): 1, ("evaluation", "e1"): 7}
    keeper.handle_message(Release("run", "r1", 1))
    assert keeper.held == {("evaluation", "e1"): 7}


def test_release_with_a_stale_epoch_does_not_drop_the_current_hold() -> None:
    """A release for an epoch we've since re-held under a newer one (a
    restart re-sent `hold`) must not remove the new hold."""
    keeper = LeaseKeeper(FakeBackend(), FakePipe(), T)
    keeper.handle_message(Hold("run", "r1", 1))
    keeper.handle_message(Hold("run", "r1", 2))  # re-held at a newer epoch
    keeper.handle_message(Release("run", "r1", 1))  # the stale release arrives late
    assert keeper.held == {("run", "r1"): 2}


def test_stop_message_raises_the_internal_stop_signal() -> None:
    keeper = LeaseKeeper(FakeBackend(), FakePipe(), T)
    with pytest.raises(_StopKeeper):
        keeper.handle_message(Stop())


async def test_renew_due_batches_one_call_per_table_and_reports_ok() -> None:
    backend = FakeBackend({("r1", 1): None, ("r2", 1): "user"})
    pipe = FakePipe()
    keeper = LeaseKeeper(backend, pipe, T)
    keeper.handle_message(Hold("run", "r1", 1))
    keeper.handle_message(Hold("run", "r2", 1))
    keeper.handle_message(Ping(100.0))
    await keeper.renew_due(100.0)
    assert len(backend.calls) == 1  # one batched UPDATE for the whole "run" table
    kind, items, ttl_s = backend.calls[0]
    assert kind == "run" and ttl_s == T.ttl_s
    assert sorted(items) == [("r1", 1), ("r2", 1)]
    beats = [m for m in pipe.outbox if isinstance(m, Beat)]
    by_id = {b.id: b for b in beats}
    assert (by_id["r1"].result, by_id["r1"].cancel_reason) == ("ok", None)
    assert (by_id["r2"].result, by_id["r2"].cancel_reason) == ("ok", "user")


async def test_renew_due_reports_fenced_for_a_row_that_no_longer_matches() -> None:
    backend = FakeBackend({})  # the UPDATE's WHERE matches nothing: a stale epoch
    pipe = FakePipe()
    keeper = LeaseKeeper(backend, pipe, T)
    keeper.handle_message(Hold("run", "r1", 1))
    keeper.handle_message(Ping(100.0))
    await keeper.renew_due(100.0)
    [beat] = [m for m in pipe.outbox if isinstance(m, Beat)]
    assert beat.result == "fenced"


async def test_renew_due_reports_error_for_every_held_item_on_a_backend_failure() -> None:
    backend = FakeBackend()
    backend.fail = ConnectionError("db is down")
    pipe = FakePipe()
    keeper = LeaseKeeper(backend, pipe, T)
    keeper.handle_message(Hold("run", "r1", 1))
    keeper.handle_message(Hold("run", "r2", 1))
    keeper.handle_message(Ping(100.0))
    await keeper.renew_due(100.0)
    beats = [m for m in pipe.outbox if isinstance(m, Beat)]
    assert {b.result for b in beats} == {"error"}
    assert {b.error_kind for b in beats} == {"ConnectionError"}


async def test_renew_due_only_renews_once_per_heartbeat_interval() -> None:
    backend = FakeBackend({("r1", 1): None})
    keeper = LeaseKeeper(backend, FakePipe(), T)
    keeper.handle_message(Hold("run", "r1", 1))
    keeper.handle_message(Ping(0.0))
    await keeper.renew_due(0.0)
    assert len(backend.calls) == 1
    await keeper.renew_due(0.1)  # well under heartbeat_s (0.5)
    assert len(backend.calls) == 1
    await keeper.renew_due(0.5)
    assert len(backend.calls) == 2


async def test_renew_due_skips_a_worker_that_has_not_pinged_recently() -> None:
    """The liveness gate (keeper_should_renew): a worker alive but stuck
    stops being renewed, so its leases expire and recovery takes over."""
    backend = FakeBackend({("r1", 1): None})
    keeper = LeaseKeeper(backend, FakePipe(), T)
    keeper.handle_message(Hold("run", "r1", 1))
    keeper.handle_message(Ping(0.0))
    await keeper.renew_due(T.liveness_timeout_s)  # exactly at the gate's edge
    assert backend.calls == []


async def test_renew_due_resumes_once_pings_catch_up_again() -> None:
    backend = FakeBackend({("r1", 1): None})
    keeper = LeaseKeeper(backend, FakePipe(), T)
    keeper.handle_message(Hold("run", "r1", 1))
    keeper.handle_message(Ping(0.0))
    await keeper.renew_due(0.0)  # first tick: renews
    assert len(backend.calls) == 1
    await keeper.renew_due(2.0)  # due, but the ping is stale: gated, skipped
    assert len(backend.calls) == 1
    keeper.handle_message(Ping(2.0))  # the worker pings again
    await keeper.renew_due(2.4)  # under heartbeat_s since the last attempt (2.0)
    assert len(backend.calls) == 1
    await keeper.renew_due(2.5)
    assert len(backend.calls) == 2


async def test_run_exits_cleanly_on_a_stop_message() -> None:
    pipe = FakePipe()
    pipe.inbox.append(Stop())
    keeper = LeaseKeeper(FakeBackend(), pipe, T)
    await keeper.run()  # returns instead of raising


async def test_run_exits_on_a_closed_pipe() -> None:
    pipe = FakePipe()
    pipe.closed = True
    keeper = LeaseKeeper(FakeBackend(), pipe, T)
    await keeper.run()


async def test_run_exits_when_the_parent_pid_changes() -> None:
    """Parent death detection: os.getppid() changing means the worker is
    gone, so the keeper stops renewing anything at once."""
    pipe = FakePipe()
    keeper = LeaseKeeper(FakeBackend(), pipe, T)
    assert not keeper.parent_is_gone()
    real_getppid = os.getppid
    try:
        os.getppid = lambda: real_getppid() + 12345  # type: ignore[assignment]
        assert keeper.parent_is_gone()
        await keeper.run()  # exits on the very first tick
    finally:
        os.getppid = real_getppid  # type: ignore[assignment]


async def test_run_sends_alive_each_tick_and_stops_cleanly_on_stop() -> None:
    pipe = FakePipe()
    keeper = LeaseKeeper(FakeBackend(), pipe, T)
    task = asyncio.create_task(keeper.run())
    async with asyncio.timeout(2):
        while not any(isinstance(m, Alive) for m in pipe.outbox):  # noqa: ASYNC110 - polling a fake pipe
            await asyncio.sleep(0.01)
    pipe.inbox.append(Stop())
    await asyncio.wait_for(task, 2)  # exits instead of raising
