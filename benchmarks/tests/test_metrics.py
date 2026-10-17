import pytest

from benchmarks import metrics
from benchmarks.sampler import parse_cpu, parse_mem_mib, summarize_samples


def row(created=0.0, claimed=1.0, started=1.5, a_started=1.5, a_finished=3.5, finished=4.0,
        status="COMPLETED", outcome="SUCCESS", attempts=1, **extra):  # fmt: skip
    return {
        "status": status, "outcome": outcome, "attempt_count": attempts,
        "created": created, "claimed": claimed, "started": started, "finished": finished,
        "a_started": a_started, "a_finished": a_finished,
        "failure_reason": None, "dead_letter_reason": None, "a_reason": None,
        "e_status": None, "task_success": None, "failure_category": None, "e_finished": None,
        **extra,
    }  # fmt: skip


def test_percentile_interpolates_between_ranks() -> None:
    values = [4.0, 1.0, 3.0, 2.0]
    assert metrics.percentile(values, 0) == 1.0
    assert metrics.percentile(values, 50) == 2.5
    assert metrics.percentile(values, 100) == 4.0
    assert metrics.percentile(values, 95) == pytest.approx(3.85)
    assert metrics.percentile([7.0], 99) == 7.0


def test_percentile_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        metrics.percentile([], 50)
    with pytest.raises(ValueError):
        metrics.percentile([1.0], 101)


def test_describe() -> None:
    d = metrics.describe([1.0, 2.0, 3.0, 4.0, 5.0])
    assert d is not None
    assert (d["n"], d["min"], d["mean"], d["p50"], d["max"]) == (5, 1.0, 3.0, 3.0, 5.0)
    assert d["p95"] == pytest.approx(4.8)
    assert metrics.describe([]) is None


def test_throughput_window() -> None:
    assert metrics.throughput(10.0, 20.0, 50) == 5.0
    assert metrics.throughput(None, 20.0, 50) is None
    assert metrics.throughput(10.0, 10.0, 5) is None
    assert metrics.throughput(10.0, 20.0, 0) is None


def test_run_latencies_follow_the_definitions() -> None:
    lat = metrics.run_latencies(row(e_finished=4.25))
    assert lat == {"queue": 1.0, "start": 0.5, "exec": 2.0, "e2e": 4.0, "grade": 0.75,
                   "e2e_graded": 4.25}  # fmt: skip
    # A run that never started has no start/exec latency rather than a made-up one.
    never = metrics.run_latencies(row(claimed=None, started=None, a_started=None, a_finished=None))
    assert never["queue"] is None and never["exec"] is None


def test_summarize_counts_successes_retries_and_failure_reasons() -> None:
    rows = [
        row(created=0.0, finished=2.0),
        row(created=1.0, finished=10.0, attempts=2),
        row(created=2.0, finished=6.0, outcome="NON_ZERO_EXIT"),
        row(created=3.0, finished=8.0, status="FAILED", outcome="SANDBOX_ERROR",
            dead_letter_reason="attempts_exhausted", a_reason="sandbox_start_failed"),
    ]  # fmt: skip
    s = metrics.summarize(rows, expected_outcome="SUCCESS", graded=False)
    assert (s["runs"], s["terminal"], s["succeeded"]) == (4, 4, 2)
    assert s["success_rate"] == 0.5
    assert s["retry_rate"] == 0.25
    assert s["window_s"] == 10.0
    assert s["throughput_rps"] == pytest.approx(0.4)
    assert s["failures"] == {
        "FAILED:attempts_exhausted:sandbox_start_failed": 1,
        "outcome=NON_ZERO_EXIT": 1,
    }
    assert s["latency_s"]["queue"]["n"] == 4
    assert "grade" not in s["latency_s"]  # not a graded workload


def test_graded_success_needs_a_passing_evaluation() -> None:
    ok = row(outcome="AGENT_FINISHED", e_status="SUCCEEDED", task_success=True, e_finished=5.0)
    wrong = row(outcome="AGENT_FINISHED", e_status="SUCCEEDED", task_success=False,
                failure_category="wrong_answer", e_finished=5.5)  # fmt: skip
    s = metrics.summarize([ok, wrong], expected_outcome="AGENT_FINISHED", graded=True)
    assert s["succeeded"] == 1
    assert s["failures"] == {"evaluation=SUCCEEDED:wrong_answer": 1}
    assert s["graded_throughput_rps"] == pytest.approx(2 / 5.5)
    assert s["latency_s"]["grade"]["n"] == 2


def test_to_columns_offsets_from_the_first_created() -> None:
    cols = metrics.to_columns([row(created=100.0, finished=104.0)], 100.0)
    assert cols["created_s"] == [0.0] and cols["finished_s"] == [4.0]
    assert cols["e2e"] == [4.0] and cols["outcome"] == ["SUCCESS"]


def test_parse_docker_stats_fields() -> None:
    assert parse_cpu("12.50%") == 12.5
    assert parse_mem_mib("146MiB / 15.32GiB") == 146.0
    assert parse_mem_mib("1.5GiB / 15GiB") == 1536.0
    assert parse_mem_mib("512KiB / 1GiB") == 0.5
    with pytest.raises(ValueError):
        parse_mem_mib("lots")


def test_summarize_samples_sums_per_role_and_counts_absence_as_zero() -> None:
    def sample(t, sandbox):
        roles = {"worker": {"cpu": 10.0, "mem_mib": 100.0, "containers": 1}}
        if sandbox:
            roles["sandbox"] = {"cpu": 50.0, "mem_mib": 20.0, "containers": 2}
        return {"t": t, "roles": roles, "host_mem_available_mib": 4000.0 - t, "host_load1": 1.0}

    out = summarize_samples([sample(0.0, False), sample(2.0, True)])
    assert out["samples"] == 2 and out["interval_s"] == 2.0
    assert out["sandbox"]["cpu_mean"] == 25.0 and out["sandbox"]["cpu_peak"] == 50.0
    assert out["sandbox"]["present"] == 1 and out["sandbox"]["containers_max"] == 2
    assert out["worker"]["mem_peak_mib"] == 100.0
    assert out["host"]["mem_available_min_mib"] == 3998.0
