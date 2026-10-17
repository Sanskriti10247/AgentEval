"""The worker's Prometheus endpoint (ADR-010 A): served on a thread from the
configured port, off when the port is 0, and never able to stop the worker."""

import socket
import urllib.request

import pytest

from agenteval_core.config import Settings
from agenteval_core.observability import metrics as prom
from agenteval_worker.main import _start_metrics_server


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_the_endpoint_serves_worker_families_on_the_configured_port() -> None:
    port = free_port()
    _start_metrics_server("127.0.0.1", port)
    prom.lease_lost("run")
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as resp:
        body = resp.read().decode()
    assert resp.status == 200
    for family in (
        "agenteval_runs_finished_total",
        "agenteval_lease_losses_total",
        "agenteval_model_tokens_total",
        "agenteval_worker_inflight",
    ):
        assert f"# TYPE {family} " in body, family
    assert 'agenteval_lease_losses_total{kind="run"}' in body


def test_port_zero_starts_nothing() -> None:
    _start_metrics_server("127.0.0.1", 0)  # no listener, no error


def test_a_taken_port_costs_the_metrics_not_the_worker() -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        _start_metrics_server("127.0.0.1", int(taken.getsockname()[1]))  # must not raise


def test_defaults_are_loopback_9464_and_the_port_is_validated() -> None:
    settings = Settings()
    assert (settings.worker_metrics_host, settings.worker_metrics_port) == ("127.0.0.1", 9464)
    with pytest.raises(ValueError):
        Settings(worker_metrics_port=70000)
