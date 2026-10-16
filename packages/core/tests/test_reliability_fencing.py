import pytest

from agenteval_core.reliability.backoff import backoff_delay
from agenteval_core.reliability.fencing import (
    LeaseDeadline,
    LeaseTiming,
    fence_deadline,
    keeper_should_renew,
)

T = LeaseTiming().validate()  # 30 s ttl, 10 s heartbeat, 10 s margin, 5 s tick, 15 s liveness


def test_defaults_are_valid_and_match_the_design() -> None:
    assert LeaseTiming(30.0, 10.0, 10.0, 5.0, 15.0) == T


@pytest.mark.parametrize(
    "bad",
    [
        LeaseTiming(heartbeat_s=20),  # not below ttl - margin
        LeaseTiming(margin_s=8),  # below 2 x tick
        LeaseTiming(liveness_timeout_s=20),  # not below ttl - margin
        LeaseTiming(ttl_s=0),
    ],
)
def test_invalid_timing_is_refused(bad: LeaseTiming) -> None:
    with pytest.raises(ValueError):
        bad.validate()


def test_deadline_counts_from_the_send_time() -> None:
    assert fence_deadline(100.0, T) == 120.0
    lease = LeaseDeadline(t_claim_sent=100.0, timing=T)
    assert not lease.must_stop(119.999)
    assert lease.must_stop(120.0)


def test_only_successful_renewals_move_the_deadline_and_never_backwards() -> None:
    lease = LeaseDeadline(t_claim_sent=100.0, timing=T)
    lease.renewed(110.0)
    assert lease.fence_at == 130.0
    lease.renewed(105.0)  # an older renewal reported late
    assert lease.fence_at == 130.0


def test_a_run_of_failed_renewals_ends_in_a_self_stop() -> None:
    lease = LeaseDeadline(t_claim_sent=100.0, timing=T)
    lease.renewed(110.0)
    # Renewals at 120 and 130 fail: nothing calls renewed(); the deadline holds.
    assert not lease.must_stop(129.9)
    assert lease.must_stop(130.0)


def test_fenced_is_immediate_and_final() -> None:
    lease = LeaseDeadline(t_claim_sent=100.0, timing=T)
    lease.fenced()
    assert lease.must_stop(100.0)
    lease.renewed(200.0)
    assert lease.must_stop(100.0)


def test_keeper_renews_only_for_a_live_worker() -> None:
    assert keeper_should_renew(last_ping=100.0, now=114.9, timing=T)
    assert not keeper_should_renew(last_ping=100.0, now=115.0, timing=T)
    assert not keeper_should_renew(last_ping=None, now=100.0, timing=T)


def test_a_stuck_worker_loses_its_lease_before_it_could_act() -> None:
    """Liveness gate + self-stop: the last renewal before the worker froze bounds
    everything. The lease expires at t_sent + ttl, the worker stops at
    t_sent + ttl - margin, and recovery can only act after expiry."""
    last_ok = 100.0
    lease = LeaseDeadline(t_claim_sent=last_ok, timing=T)
    expiry = last_ok + T.ttl_s
    assert lease.fence_at <= expiry - T.margin_s


def test_backoff_doubles_up_to_the_cap_with_jitter_bounds() -> None:
    mid = lambda a, b: 1.0  # noqa: E731
    assert [backoff_delay(n, uniform=mid) for n in range(1, 8)] == [2, 4, 8, 16, 32, 60, 60]
    assert backoff_delay(1, uniform=lambda a, b: a) == pytest.approx(1.6)
    assert backoff_delay(1, uniform=lambda a, b: b) == pytest.approx(2.4)
    assert backoff_delay(10_000, uniform=mid) == 60
    with pytest.raises(ValueError):
        backoff_delay(0)
