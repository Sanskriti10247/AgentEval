"""Messages exchanged over the worker<->lease-keeper duplex pipe (ADR-008 D).

Every message is a small frozen dataclass so it can cross a
`multiprocessing.Pipe()` (pickled) or be constructed directly by a test. Only
`kind = "run"` is used before Step 7; the field is already in the protocol so
evaluation leases need no wire-format change later.
"""

from dataclasses import dataclass
from typing import Literal

LeaseKind = Literal["run", "evaluation"]
BeatResult = Literal["ok", "fenced", "error"]


@dataclass(frozen=True)
class Hold:
    """worker -> keeper: start renewing this lease."""

    kind: LeaseKind
    id: str
    epoch: int


@dataclass(frozen=True)
class Release:
    """worker -> keeper: stop renewing it (final transition, fence or self-stop)."""

    kind: LeaseKind
    id: str
    epoch: int


@dataclass(frozen=True)
class Ping:
    """worker -> keeper: proof of life, sent every second."""

    t: float


@dataclass(frozen=True)
class Stop:
    """worker -> keeper: shut down."""


@dataclass(frozen=True)
class Beat:
    """keeper -> worker: the result of one renewal attempt for one held lease.

    `t_sent` is the keeper's time.monotonic() taken just before the renewal
    statement was sent - both processes share CLOCK_MONOTONIC, so the worker
    can compute fence_at = t_sent + ttl - margin directly from it.
    """

    kind: LeaseKind
    id: str
    epoch: int
    t_sent: float
    result: BeatResult
    # Set (to 'user' or 'batch') exactly when `result == "ok"` and a cancel is
    # pending for this lease; always None for `kind = "evaluation"` (no such
    # column there) and for "fenced"/"error".
    cancel_reason: str | None = None
    error_kind: str | None = None


@dataclass(frozen=True)
class Alive:
    """keeper -> worker: proof the keeper's loop is still turning, every second."""

    t: float


WorkerMessage = Hold | Release | Ping | Stop
KeeperMessage = Beat | Alive
