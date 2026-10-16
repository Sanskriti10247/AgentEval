import pytest

from agenteval_core.config import Settings


def test_lease_timing_matches_the_approved_defaults() -> None:
    timing = Settings().lease_timing().validate()
    assert (timing.ttl_s, timing.heartbeat_s, timing.margin_s) == (30.0, 10.0, 10.0)
    assert (timing.reconcile_interval_s, timing.liveness_timeout_s) == (5.0, 15.0)


def test_lease_timing_rejects_an_invalid_combination() -> None:
    bad = Settings(heartbeat_s=25.0)  # not below ttl - margin
    with pytest.raises(ValueError, match="heartbeat interval"):
        bad.lease_timing().validate()


def test_lease_timing_reads_the_agenteval_prefixed_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTEVAL_LEASE_TTL_S", "40")
    monkeypatch.setenv("AGENTEVAL_HEARTBEAT_S", "12")
    monkeypatch.setenv("AGENTEVAL_SELF_FENCE_MARGIN_S", "12")
    monkeypatch.setenv("AGENTEVAL_RECONCILE_INTERVAL_S", "5")
    monkeypatch.setenv("AGENTEVAL_WORKER_LIVENESS_TIMEOUT_S", "15")
    timing = Settings().lease_timing().validate()
    assert (timing.ttl_s, timing.heartbeat_s, timing.margin_s) == (40.0, 12.0, 12.0)
