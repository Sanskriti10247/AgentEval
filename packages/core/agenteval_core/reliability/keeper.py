"""The lease keeper's own logic (ADR-008 D): batched renewal, the worker
liveness gate, and exit on parent death or a closed pipe.

`LeaseKeeper` is deliberately decoupled from asyncpg and from
`multiprocessing`, so it can be driven by a fake backend and a fake pipe in
unit tests, and by `AsyncpgLeaseBackend` plus a real `multiprocessing.Pipe()`
end in the worker process (`run_keeper_process`, the subprocess entry point).
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable, Mapping
from typing import Protocol

from agenteval_core.logging import get_logger
from agenteval_core.reliability.fencing import LeaseTiming, keeper_should_renew
from agenteval_core.reliability.keeper_protocol import (
    Alive,
    Beat,
    Hold,
    LeaseKind,
    Ping,
    Release,
    Stop,
)

log = get_logger(__name__)

ALIVE_INTERVAL_S = 1.0


class RenewalBackend(Protocol):
    """Whatever can batch-renew one kind of lease. `AsyncpgLeaseBackend` is the
    production implementation; tests use a fake."""

    async def renew(
        self, kind: LeaseKind, items: list[tuple[str, int]], ttl_s: float
    ) -> Mapping[str, str | None]:
        """Renews every (id, epoch) pair that still matches. Returns, for each
        id actually renewed, its cancel reason ('user'/'batch') or None.
        Raises on a connection or database failure; the caller reports every
        held item of that kind as `error` and retries next tick."""
        ...


class Transport(Protocol):
    """Duck-types `multiprocessing.connection.Connection` closely enough for
    a `multiprocessing.Pipe()` end or a fake to satisfy it."""

    def poll(self, timeout: float = 0.0) -> bool: ...
    def recv(self) -> object: ...
    def send(self, obj: object) -> None: ...


class _StopKeeper(Exception):
    pass


class LeaseKeeper:
    """One tick: drain incoming messages, send `alive`, and (every
    `heartbeat_s`) renew everything held, in one batched UPDATE per table -
    but only while the worker has pinged recently (the liveness gate)."""

    def __init__(
        self,
        backend: RenewalBackend,
        transport: Transport,
        timing: LeaseTiming,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._backend = backend
        self._pipe = transport
        self._timing = timing
        self._clock = clock
        self._held: dict[tuple[LeaseKind, str], int] = {}
        self._last_ping: float | None = None
        self._last_renewed = float("-inf")  # so the first tick always renews
        self._parent_pid = os.getppid()

    @property
    def held(self) -> Mapping[tuple[LeaseKind, str], int]:
        return dict(self._held)

    def handle_message(self, msg: object) -> None:
        if isinstance(msg, Hold):
            self._held[(msg.kind, msg.id)] = msg.epoch
        elif isinstance(msg, Release):
            # Only drop it if the epoch still matches: a release for a lease
            # already re-held under a newer epoch must not drop the new one.
            if self._held.get((msg.kind, msg.id)) == msg.epoch:
                del self._held[(msg.kind, msg.id)]
        elif isinstance(msg, Ping):
            self._last_ping = msg.t
        elif isinstance(msg, Stop):
            raise _StopKeeper
        else:
            log.warning("keeper_unknown_message", message_type=type(msg).__name__)

    def drain_incoming(self) -> None:
        """Read every message currently waiting, without blocking. Raises
        EOFError if the worker's end of the pipe is gone (parent dead)."""
        while self._pipe.poll(0):
            self.handle_message(self._pipe.recv())

    def parent_is_gone(self) -> bool:
        return os.getppid() != self._parent_pid

    async def renew_due(self, now: float) -> None:
        if now - self._last_renewed < self._timing.heartbeat_s:
            return
        self._last_renewed = now
        if not keeper_should_renew(last_ping=self._last_ping, now=now, timing=self._timing):
            # The worker looks alive but stuck: don't keep its leases alive for it.
            return
        by_kind: dict[LeaseKind, list[tuple[str, int]]] = {}
        for (kind, id_), epoch in self._held.items():
            by_kind.setdefault(kind, []).append((id_, epoch))
        for kind, items in by_kind.items():
            await self._renew_kind(kind, items)

    async def _renew_kind(self, kind: LeaseKind, items: list[tuple[str, int]]) -> None:
        t_sent = self._clock()
        try:
            renewed = await self._backend.renew(kind, items, self._timing.ttl_s)
        except Exception as exc:
            log.warning("keeper_renewal_error", kind=kind, error=str(exc))
            for id_, epoch in items:
                self._send(Beat(kind, id_, epoch, t_sent, "error", error_kind=type(exc).__name__))
            return
        for id_, epoch in items:
            if id_ in renewed:
                self._send(Beat(kind, id_, epoch, t_sent, "ok", cancel_reason=renewed[id_]))
            else:
                self._send(Beat(kind, id_, epoch, t_sent, "fenced"))

    def _send(self, msg: object) -> None:
        self._pipe.send(msg)

    async def run(self) -> None:
        """The keeper's main loop. Returns (does not raise) on a clean `stop`,
        a closed pipe, or parent death - every one of those means "exit"."""
        try:
            while True:
                await asyncio.sleep(ALIVE_INTERVAL_S)
                if self.parent_is_gone():
                    log.warning("keeper_parent_gone")
                    return
                try:
                    self.drain_incoming()
                except EOFError:
                    log.warning("keeper_pipe_closed")
                    return
                now = self._clock()
                self._send(Alive(now))
                await self.renew_due(now)
        except _StopKeeper:
            return
