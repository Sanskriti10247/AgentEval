"""Lease timing arithmetic (F2, F7; ADR-008 D).

The worker decides when it must stop. The lease keeper only renews and reports
each renewal's send time; both processes read the same monotonic clock.

* `fence_at = t_sent + ttl - margin`, from the send time of the last renewal
  that succeeded. It only ever moves forward, and only on success.
* The keeper renews only while the worker has pinged within
  `liveness_timeout`, so a worker that is alive but stuck loses its leases.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class LeaseTiming:
    ttl_s: float = 30.0
    heartbeat_s: float = 10.0
    margin_s: float = 10.0
    reconcile_interval_s: float = 5.0
    liveness_timeout_s: float = 15.0

    def validate(self) -> "LeaseTiming":
        """Startup refuses combinations that cannot keep a healthy lease."""
        timings = (
            self.ttl_s,
            self.heartbeat_s,
            self.margin_s,
            self.reconcile_interval_s,
            self.liveness_timeout_s,
        )
        if any(t <= 0 for t in timings):
            raise ValueError("lease timings must be positive")
        if self.heartbeat_s >= self.ttl_s - self.margin_s:
            raise ValueError("heartbeat interval must be shorter than ttl - margin")
        if self.margin_s < 2 * self.reconcile_interval_s:
            raise ValueError("self-fence margin must be at least twice the reconcile interval")
        if self.liveness_timeout_s >= self.ttl_s - self.margin_s:
            raise ValueError("worker liveness timeout must be shorter than ttl - margin")
        return self


def fence_deadline(t_sent: float, timing: LeaseTiming) -> float:
    return t_sent + timing.ttl_s - timing.margin_s


class LeaseDeadline:
    """Worker-side self-stop deadline for one lease."""

    def __init__(self, t_claim_sent: float, timing: LeaseTiming) -> None:
        self._timing = timing
        self.fence_at = fence_deadline(t_claim_sent, timing)
        self.lost = False

    def renewed(self, t_sent: float) -> None:
        # A late report of an older renewal never moves the deadline back.
        if not self.lost:
            self.fence_at = max(self.fence_at, fence_deadline(t_sent, self._timing))

    def fenced(self) -> None:
        """The guarded renewal matched 0 rows: someone else owns the run now."""
        self.lost = True

    def must_stop(self, now: float) -> bool:
        return self.lost or now >= self.fence_at


def keeper_should_renew(*, last_ping: float | None, now: float, timing: LeaseTiming) -> bool:
    """Keeper-side gate: renew only for a worker that has shown progress recently."""
    return last_ping is not None and now - last_ping < timing.liveness_timeout_s
