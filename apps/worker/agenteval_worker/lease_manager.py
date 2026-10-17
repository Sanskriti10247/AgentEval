"""Worker-side half of the lease keeper protocol (ADR-008 D).

`LeaseManager` owns the keeper subprocess and the duplex pipe to it. For every
held lease it tracks a `LeaseDeadline` (fencing.py): `ok` beats move it
forward, `fenced` and reaching the deadline fire the lease's `on_lease_lost`
callback immediately, and `error` beats leave it untouched (the deadline is
the only thing that can stop renewal early). The worker - not the keeper -
decides when it must stop; this class only acts on what the keeper reports
and on the clock.

If the keeper stops reporting `alive` (dead, or SIGKILLed), the manager
restarts it and re-sends `hold` for every lease still held, with its current
epoch; the deadlines already tracked here are untouched by the restart.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import time
from collections.abc import Callable
from dataclasses import dataclass
from multiprocessing.connection import Connection
from multiprocessing.context import SpawnContext
from multiprocessing.process import BaseProcess

from agenteval_core.logging import get_logger
from agenteval_core.observability import metrics as prom
from agenteval_core.reliability.fencing import LeaseDeadline, LeaseTiming
from agenteval_core.reliability.keeper_process import run_keeper_process
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

# ADR-008 D: "no alive for 3 s" means the keeper is dead.
KEEPER_DEAD_AFTER_S = 3.0
PING_INTERVAL_S = 1.0
SELF_CHECK_INTERVAL_S = 0.5


@dataclass
class _Held:
    epoch: int
    deadline: LeaseDeadline
    on_lease_lost: Callable[[], None]
    on_cancel_requested: Callable[[str], None] | None
    on_renewed: Callable[[float], None] | None = None


class LeaseManager:
    def __init__(
        self,
        database_url: str,
        timing: LeaseTiming,
        worker_id: str,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._database_url = database_url
        self._timing = timing
        self._worker_id = worker_id
        self._clock = clock
        self._held: dict[tuple[LeaseKind, str], _Held] = {}
        self._ctx: SpawnContext = multiprocessing.get_context("spawn")
        self._process: BaseProcess | None = None
        self._conn: Connection | None = None
        self._last_alive: float | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._stopped = False

    async def start(self) -> None:
        self._spawn()
        self._tasks = [
            asyncio.create_task(self._ping_loop()),
            asyncio.create_task(self._read_loop()),
            asyncio.create_task(self._self_check_loop()),
            asyncio.create_task(self._watchdog_loop()),
        ]

    async def stop(self) -> None:
        self._stopped = True
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._send(Stop())
        self._kill_process()

    def hold(
        self,
        kind: LeaseKind,
        id_: str,
        epoch: int,
        t_sent: float,
        *,
        on_lease_lost: Callable[[], None],
        on_cancel_requested: Callable[[str], None] | None = None,
        on_renewed: Callable[[float], None] | None = None,
    ) -> None:
        """Register a lease claimed at `t_sent` (monotonic). `fence_at` is
        seeded from that send time, per design §16: the owner self-stops even
        if the first beat is late. `on_renewed` (if given) is told the new
        `fence_at` after every successful beat, so a caller's own copy of the
        deadline (e.g. the runtime guard on a `CancelToken`) stays in sync
        with this one, independent of this manager's own self-check loop."""
        deadline = LeaseDeadline(t_claim_sent=t_sent, timing=self._timing)
        self._held[(kind, id_)] = _Held(
            epoch, deadline, on_lease_lost, on_cancel_requested, on_renewed
        )
        self._send(Hold(kind, id_, epoch))

    def release(self, kind: LeaseKind, id_: str, epoch: int) -> None:
        self._held.pop((kind, id_), None)
        self._send(Release(kind, id_, epoch))

    def deadline_for(self, kind: LeaseKind, id_: str) -> LeaseDeadline | None:
        held = self._held.get((kind, id_))
        return held.deadline if held is not None else None

    # -- the keeper process ------------------------------------------------------------

    def _spawn(self) -> None:
        parent_conn, child_conn = self._ctx.Pipe(duplex=True)
        process = self._ctx.Process(
            target=run_keeper_process,
            args=(child_conn, self._database_url, self._timing, self._worker_id),
            daemon=True,
        )
        process.start()
        child_conn.close()  # only the child needs its end
        self._conn = parent_conn
        self._process = process
        self._last_alive = self._clock()

    def _kill_process(self) -> None:
        if self._process is not None and self._process.is_alive():
            self._process.terminate()
            self._process.join(timeout=2.0)
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
        self._process = None
        self._conn = None

    def _restart(self) -> None:
        log.warning("lease_keeper_restarting", worker_id=self._worker_id)
        self._kill_process()
        self._spawn()
        for (kind, id_), held in self._held.items():
            self._send(Hold(kind, id_, held.epoch))

    def _send(self, msg: object) -> None:
        if self._conn is None:
            return
        try:
            self._conn.send(msg)
        except (BrokenPipeError, OSError):
            pass  # the watchdog notices the silence and restarts

    # -- background loops --------------------------------------------------------------

    async def _ping_loop(self) -> None:
        while True:
            self._send(Ping(self._clock()))
            await asyncio.sleep(PING_INTERVAL_S)

    async def _read_loop(self) -> None:
        while True:
            await asyncio.sleep(0.1)
            if self._conn is None:
                continue
            try:
                while self._conn.poll(0):
                    self._handle(self._conn.recv())
            except (EOFError, OSError):
                continue  # the watchdog notices and restarts

    def _handle(self, msg: object) -> None:
        if isinstance(msg, Alive):
            self._last_alive = self._clock()
        elif isinstance(msg, Beat):
            self._handle_beat(msg)

    def _handle_beat(self, beat: Beat) -> None:
        held = self._held.get((beat.kind, beat.id))
        if held is None or held.epoch != beat.epoch:
            return  # a stale beat for a lease we've since released or re-held
        if beat.result == "ok":
            held.deadline.renewed(beat.t_sent)
            if held.on_renewed is not None:
                held.on_renewed(held.deadline.fence_at)
            if beat.cancel_reason is not None and held.on_cancel_requested is not None:
                held.on_cancel_requested(beat.cancel_reason)
        elif beat.result == "fenced":
            held.deadline.fenced()
            self._fire_lost(beat.kind, beat.id)
        # "error": the deadline is untouched; only a timely `ok` moves it.

    def _fire_lost(self, kind: LeaseKind, id_: str) -> None:
        held = self._held.pop((kind, id_), None)
        if held is not None:
            prom.lease_lost(kind)
            held.on_lease_lost()

    async def _self_check_loop(self) -> None:
        """Belt and suspenders: even if a `fenced` beat is lost, the deadline
        itself is still checked here every tick."""
        while True:
            await asyncio.sleep(SELF_CHECK_INTERVAL_S)
            now = self._clock()
            for key, held in list(self._held.items()):
                if held.deadline.must_stop(now):
                    self._fire_lost(*key)

    async def _watchdog_loop(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            if (
                self._last_alive is not None
                and self._clock() - self._last_alive > KEEPER_DEAD_AFTER_S
            ):
                self._restart()
