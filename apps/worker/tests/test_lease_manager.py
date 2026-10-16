"""The worker-side half of the lease keeper protocol (ADR-008 D):
`LeaseManager`'s own bookkeeping, driven directly (no subprocess, no pipe -
`.start()` is never called, so `_send()` is a no-op and `_handle_beat` /
`_fire_lost` can be exercised in isolation). Process-level behaviour (a real
SIGKILL, parent death) is integration-tested with a real keeper process."""

import pytest

from agenteval_core.reliability.fencing import LeaseTiming
from agenteval_core.reliability.keeper_protocol import Beat
from agenteval_worker.lease_manager import LeaseManager

T = LeaseTiming(
    ttl_s=2.0, heartbeat_s=0.5, margin_s=0.5, reconcile_interval_s=0.2, liveness_timeout_s=1.0
)


def _manager(clock: object = (lambda: 0.0)) -> LeaseManager:
    return LeaseManager("postgresql://unused/db", T, "w1", clock=clock)  # type: ignore[arg-type]


def test_hold_seeds_the_deadline_from_the_claim_send_time() -> None:
    lm = _manager()
    lm.hold("run", "r1", 1, 100.0, on_lease_lost=lambda: None)
    deadline = lm.deadline_for("run", "r1")
    assert deadline is not None
    assert deadline.fence_at == pytest.approx(100.0 + T.ttl_s - T.margin_s)


def test_release_forgets_the_lease() -> None:
    lm = _manager()
    lm.hold("run", "r1", 1, 100.0, on_lease_lost=lambda: None)
    lm.release("run", "r1", 1)
    assert lm.deadline_for("run", "r1") is None


def test_ok_beat_moves_the_deadline_forward() -> None:
    lm = _manager()
    lm.hold("run", "r1", 1, 100.0, on_lease_lost=lambda: None)
    lm._handle_beat(Beat("run", "r1", 1, 110.0, "ok"))
    deadline = lm.deadline_for("run", "r1")
    assert deadline is not None
    assert deadline.fence_at == pytest.approx(110.0 + T.ttl_s - T.margin_s)


def test_error_beat_never_moves_the_deadline() -> None:
    lm = _manager()
    lm.hold("run", "r1", 1, 100.0, on_lease_lost=lambda: None)
    before = lm.deadline_for("run", "r1")
    assert before is not None
    fence_at_before = before.fence_at
    lm._handle_beat(Beat("run", "r1", 1, 110.0, "error", error_kind="ConnectionError"))
    after = lm.deadline_for("run", "r1")
    assert after is not None and after.fence_at == fence_at_before


def test_fenced_beat_fires_lease_lost_immediately_and_drops_the_hold() -> None:
    lm = _manager()
    lost = []
    lm.hold("run", "r1", 1, 100.0, on_lease_lost=lambda: lost.append(True))
    lm._handle_beat(Beat("run", "r1", 1, 110.0, "fenced"))
    assert lost == [True]
    assert lm.deadline_for("run", "r1") is None  # released


def test_ok_beat_with_a_cancel_reason_invokes_the_cancel_callback() -> None:
    lm = _manager()
    reasons: list[str] = []
    lm.hold("run", "r1", 1, 100.0, on_lease_lost=lambda: None, on_cancel_requested=reasons.append)
    lm._handle_beat(Beat("run", "r1", 1, 110.0, "ok", cancel_reason="user"))
    assert reasons == ["user"]


def test_ok_beat_without_a_cancel_reason_does_not_call_the_callback() -> None:
    lm = _manager()
    reasons: list[str] = []
    lm.hold("run", "r1", 1, 100.0, on_lease_lost=lambda: None, on_cancel_requested=reasons.append)
    lm._handle_beat(Beat("run", "r1", 1, 110.0, "ok"))
    assert reasons == []


def test_a_beat_for_a_stale_epoch_is_ignored() -> None:
    """A beat for an epoch released (or since re-held under a newer one)
    must not affect the current hold - the keeper may still report one in
    flight right after a restart re-sends `hold`."""
    lm = _manager()
    lm.hold("run", "r1", 1, 100.0, on_lease_lost=lambda: None)
    lm.release("run", "r1", 1)
    lm.hold("run", "r1", 2, 200.0, on_lease_lost=lambda: None)
    lm._handle_beat(Beat("run", "r1", 1, 110.0, "fenced"))  # stale epoch 1
    assert lm.deadline_for("run", "r1") is not None  # epoch 2's hold survives


def test_a_beat_for_an_unheld_lease_is_ignored() -> None:
    lm = _manager()
    lm._handle_beat(Beat("run", "unknown", 1, 110.0, "fenced"))  # must not raise


def test_self_check_fires_lease_lost_once_the_deadline_passes() -> None:
    """The deadline itself (not just a `fenced` beat) can trigger the stop:
    the belt-and-suspenders path the self-check loop runs every tick."""
    now = [0.0]
    lm = _manager(clock=lambda: now[0])
    lost = []
    lm.hold("run", "r1", 1, 0.0, on_lease_lost=lambda: lost.append(True))
    now[0] = T.ttl_s - T.margin_s - 0.001
    for key, held in list(lm._held.items()):
        if held.deadline.must_stop(now[0]):
            lm._fire_lost(*key)
    assert lost == []
    now[0] = T.ttl_s - T.margin_s
    for key, held in list(lm._held.items()):
        if held.deadline.must_stop(now[0]):
            lm._fire_lost(*key)
    assert lost == [True]
