from benchmarks import report


def result(workers=1, runs=200, tp=10.0, label="scaling", workload="noop-command", status="ok"):
    lat = {"p50": 0.5, "p95": 1.0, "p99": 1.5, "max": 2.0, "mean": 0.6, "min": 0.1, "n": runs}
    return {
        "label": label, "workload": workload, "runs": runs, "workers": workers, "concurrency": 2,
        "status": status, "git": {"sha": "a" * 40, "dirty": False},
        "started_at": "2026-10-17T00:00:00+00:00",
        "wall": {"submit_s": 1.0, "total_s": 20.0},
        "machine": {"cpu_model": "Test CPU", "nproc": 4, "mem_total_mib": 8000,
                    "mem_available_mib": 5000, "swap_total_mib": 0, "kernel": "k",
                    "docker_server_version": "29", "load_avg": [1.0, 1.0, 1.0],
                    "other_containers_running": ["other-stack"]},
        "summary": {"throughput_rps": tp, "graded_throughput_rps": None, "success_rate": 1.0,
                    "retry_rate": 0.0,
                    "latency_s": {k: lat for k in ("queue", "start", "exec", "e2e")}},
        "resources": {"samples": 5,
                      **{r: {"cpu_mean": 10.0, "cpu_peak": 20.0, "mem_mean_mib": 100.0,
                             "mem_peak_mib": 120.0} for r in report.ROLES},
                      "host": {"mem_available_min_mib": 3000.0}},
    }  # fmt: skip


def test_stat_and_fmt_use_median_and_spread() -> None:
    rs = [result(tp=9.0), result(tp=11.0), result(tp=10.0)]
    s = report.stat(rs, "summary.throughput_rps")
    assert s == (10.0, 9.0, 11.0, 3)
    assert report.fmt(s) == "10.00 (9.00-11.00)"
    assert report.fmt(report.stat([result(tp=3.0)], "summary.throughput_rps")) == "3.00"
    assert report.fmt(report.stat(rs, "summary.nope")) == "n/a"


def test_render_scaling_table_speedup_and_skips() -> None:
    results = [
        result(workers=1, tp=4.0),
        result(workers=2, tp=7.0),
        {**result(workers=25), "status": "skipped", "reason": "not enough memory"},
    ]
    text, svg = report.render(results, observations="Measured: nothing surprising.")
    assert "| 2 | 1 | 7.00 | 1.75 | 88% |" in text
    assert "not enough memory" in text
    assert "`other-stack`" in text
    assert "Measured: nothing surprising." in text
    assert svg is not None and svg.startswith("<svg") and "<polyline" in svg


def test_render_without_results_still_renders() -> None:
    text, svg = report.render([])
    assert "no results" in text and svg is None
