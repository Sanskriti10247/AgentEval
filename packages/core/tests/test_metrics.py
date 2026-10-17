"""Metrics (ADR-010 A): every helper moves its family on its event, label values
come from closed sets, and instrumentation can never raise into the caller.

Counters are process-global, so every assertion is a before/after delta."""

import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from prometheus_client import REGISTRY

from agenteval_core.observability import metrics as m
from agenteval_core.providers.base import ModelErrorKind
from agenteval_core.reliability.taxonomy import FailureReason
from agenteval_core.reliability.transitions import EvaluationState, RunState
from agenteval_core.states import Outcome
from agenteval_core.tools.base import ToolStatus

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def value(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_run_decided_counts_the_transition_and_a_terminal_status() -> None:
    before = (
        value("agenteval_run_transitions_total", code="R7", actor="worker"),
        value("agenteval_runs_finished_total", status="COMPLETED", outcome="AGENT_FINISHED"),
    )
    m.run_decided("R7", "worker", RunState.COMPLETED, Outcome.AGENT_FINISHED)
    assert value("agenteval_run_transitions_total", code="R7", actor="worker") == before[0] + 1
    assert (
        value("agenteval_runs_finished_total", status="COMPLETED", outcome="AGENT_FINISHED")
        == before[1] + 1
    )


def test_a_retry_is_a_transition_but_not_a_finished_run() -> None:
    finished = value("agenteval_runs_finished_total", status="RETRYING", outcome="none")
    before = value("agenteval_run_transitions_total", code="R8", actor="worker")
    m.run_decided("R8", "worker", RunState.RETRYING)
    assert value("agenteval_run_transitions_total", code="R8", actor="worker") == before + 1
    assert value("agenteval_runs_finished_total", status="RETRYING", outcome="none") == finished


def test_a_dead_letter_counts_its_reason_and_a_failed_run() -> None:
    dead = value("agenteval_run_dead_letters_total", reason="retries_exhausted")
    failed = value("agenteval_runs_finished_total", status="FAILED", outcome="MODEL_ERROR")
    m.run_decided(
        "R10",
        "worker",
        RunState.FAILED,
        Outcome.MODEL_ERROR,
        dead_letter_reason="retries_exhausted",
    )
    assert value("agenteval_run_dead_letters_total", reason="retries_exhausted") == dead + 1
    assert value("agenteval_runs_finished_total", status="FAILED", outcome="MODEL_ERROR") == (
        failed + 1
    )


def test_a_cancellation_counts_its_reason_and_an_unknown_one_is_unknown() -> None:
    user = value("agenteval_run_cancellations_total", reason="user")
    unknown = value("agenteval_run_cancellations_total", reason="unknown")
    m.run_decided("R11", "worker", RunState.CANCELLED, None, cancel_reason="user")
    m.run_decided("R11", "worker", RunState.CANCELLED, None, cancel_reason="worker_shutdown")
    assert value("agenteval_run_cancellations_total", reason="user") == user + 1
    assert value("agenteval_run_cancellations_total", reason="unknown") == unknown + 1
    assert value("agenteval_runs_finished_total", status="CANCELLED", outcome="none") >= 2


@pytest.mark.parametrize("code", ["R4a", "R9", "R10"])
def test_reconciler_recoveries_are_counted_by_code(code: str) -> None:
    before = value("agenteval_run_transitions_total", code=code, actor="reconciler")
    lease = value(
        "agenteval_attempt_failures_total", failure_reason="lease_expired", category="lease"
    )
    m.run_recovered(code)
    assert value("agenteval_run_transitions_total", code=code, actor="reconciler") == before + 1
    abandons = code in {"R9", "R10", "R11"}  # a RUNNING recovery abandons the attempt (A3)
    assert value(
        "agenteval_attempt_failures_total", failure_reason="lease_expired", category="lease"
    ) == lease + (1 if abandons else 0)


def test_attempt_failures_carry_the_reason_and_its_derived_category() -> None:
    before = value(
        "agenteval_attempt_failures_total",
        failure_reason="model_overloaded",
        category="model_transient",
    )
    m.attempt_failed(FailureReason.MODEL_OVERLOADED)
    m.attempt_failed(None)  # a completed attempt has no failure
    assert (
        value(
            "agenteval_attempt_failures_total",
            failure_reason="model_overloaded",
            category="model_transient",
        )
        == before + 1
    )


def test_queue_wait_run_duration_and_evaluation_duration_are_observed() -> None:
    wait = value("agenteval_run_queue_wait_seconds_count")
    m.queue_wait_since(datetime.now(UTC) - timedelta(seconds=2))
    assert value("agenteval_run_queue_wait_seconds_count") == wait + 1
    assert value("agenteval_run_queue_wait_seconds_sum") >= 2

    dur = value("agenteval_run_duration_seconds_count", outcome="SUCCESS")
    m.run_duration_ms(Outcome.SUCCESS, 1500)
    m.run_duration_ms(Outcome.SUCCESS, None)  # unknown duration: nothing observed
    assert value("agenteval_run_duration_seconds_count", outcome="SUCCESS") == dur + 1

    ev = value("agenteval_evaluation_duration_seconds_count", status="SUCCEEDED")
    m.evaluation_duration_ms(EvaluationState.SUCCEEDED, 400)
    assert value("agenteval_evaluation_duration_seconds_count", status="SUCCEEDED") == ev + 1


def test_model_call_moves_latency_tokens_and_cost() -> None:
    lab = {"provider": "fake", "model": "fake-model"}
    before = (
        value("agenteval_model_call_duration_seconds_count", **lab),
        value("agenteval_model_tokens_total", **lab, direction="input"),
        value("agenteval_model_tokens_total", **lab, direction="output"),
        value("agenteval_model_cost_usd_total", **lab),
    )
    m.model_call("fake", "fake-model", latency_s=0.4, tokens_in=100, tokens_out=20, cost_usd=0.0002)
    assert value("agenteval_model_call_duration_seconds_count", **lab) == before[0] + 1
    assert value("agenteval_model_tokens_total", **lab, direction="input") == before[1] + 100
    assert value("agenteval_model_tokens_total", **lab, direction="output") == before[2] + 20
    assert value("agenteval_model_cost_usd_total", **lab) == pytest.approx(before[3] + 0.0002)


def test_an_unpriced_model_never_becomes_a_label() -> None:
    other = {"provider": "other", "model": "other"}
    before = value("agenteval_model_tokens_total", **other, direction="input")
    m.model_call(
        "fake", "model-" + uuid.uuid4().hex, latency_s=1, tokens_in=5, tokens_out=1, cost_usd=0
    )
    assert value("agenteval_model_tokens_total", **other, direction="input") == before + 5


def test_model_errors_are_counted_by_kind() -> None:
    lab = {"provider": "fake", "model": "fake-model", "kind": "overloaded"}
    before = value("agenteval_model_call_errors_total", **lab)
    m.model_error("fake", "fake-model", ModelErrorKind.OVERLOADED)
    assert value("agenteval_model_call_errors_total", **lab) == before + 1


def test_tool_calls_count_status_and_latency_and_an_invented_tool_is_unknown() -> None:
    known = frozenset({"read_file"})
    ok = value("agenteval_tool_calls_total", tool="read_file", status="ok")
    lat = value("agenteval_tool_call_duration_seconds_count", tool="read_file")
    unknown = value("agenteval_tool_calls_total", tool="unknown", status="unknown_tool")
    m.tool_call("read_file", known, ToolStatus.OK, 0.2)
    m.tool_call(uuid.uuid4().hex, known, ToolStatus.UNKNOWN_TOOL, 0.0)
    assert value("agenteval_tool_calls_total", tool="read_file", status="ok") == ok + 1
    assert value("agenteval_tool_call_duration_seconds_count", tool="read_file") == lat + 1
    assert value("agenteval_tool_calls_total", tool="unknown", status="unknown_tool") == (
        unknown + 1
    )


def test_lease_losses_artifact_saves_and_reconciler_errors() -> None:
    lost = value("agenteval_lease_losses_total", kind="run")
    saved = value("agenteval_artifact_saves_total", result="stored")
    stepped = value("agenteval_reconciler_step_errors_total", step="redispatch")
    m.lease_lost("run")
    m.artifact_save("stored")
    m.reconciler_step_error("redispatch")
    assert value("agenteval_lease_losses_total", kind="run") == lost + 1
    assert value("agenteval_artifact_saves_total", result="stored") == saved + 1
    assert value("agenteval_reconciler_step_errors_total", step="redispatch") == stepped + 1


def test_evaluation_transitions_are_counted_by_code() -> None:
    before = value("agenteval_evaluation_transitions_total", code="E7")
    m.evaluation_transition("E7")
    assert value("agenteval_evaluation_transitions_total", code="E7") == before + 1


def test_inflight_goes_up_and_back_down_even_when_the_work_fails() -> None:
    before = value("agenteval_worker_inflight", kind="run")
    with pytest.raises(RuntimeError), m.inflight("run"):
        assert value("agenteval_worker_inflight", kind="run") == before + 1
        raise RuntimeError("the run blew up")
    assert value("agenteval_worker_inflight", kind="run") == before
    m.set_concurrency(3)
    assert value("agenteval_worker_concurrency") == 3


def test_http_request_records_template_method_and_status() -> None:
    lab = {"method": "GET", "route": "/runs/{run_id}"}
    before = value("agenteval_http_requests_total", **lab, status="404")
    m.http_request("get", "/runs/{run_id}", 404, 0.01)
    assert value("agenteval_http_requests_total", **lab, status="404") == before + 1
    assert value("agenteval_http_request_duration_seconds_count", **lab) >= 1


def test_a_failing_metric_never_raises_into_the_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken:
        def labels(self, *a: object) -> "Broken":
            raise RuntimeError("registry exploded")

    monkeypatch.setattr(m, "LEASE_LOSSES", Broken())
    monkeypatch.setattr(m, "RUN_TRANSITIONS", Broken())
    m.lease_lost("run")
    m.run_decided("R7", "worker", RunState.COMPLETED, Outcome.SUCCESS)
    m.run_recovered("not-a-code")  # a bug in a call site is swallowed too


def test_hostile_label_input_collapses_into_closed_sets() -> None:
    """Whatever a caller (or a model, or a client) passes, series stay bounded."""
    junk = str(uuid.uuid4())
    m.http_request("BREW", "unmatched", 418, 0.0)
    m.run_decided(junk, junk, junk, junk, dead_letter_reason=junk, cancel_reason=junk)
    m.attempt_failed(junk)
    m.run_duration_ms(junk, 1)
    m.evaluation_transition(junk)
    m.evaluation_duration_ms(junk, 1)
    m.model_error(junk, junk, junk)
    m.tool_call(junk, frozenset(), junk, 0)
    m.lease_lost(junk)
    m.artifact_save(junk)
    m.reconciler_step_error(junk)
    m.scrape_error(junk)
    assert_no_uuid_labels()


def assert_no_uuid_labels() -> None:
    offenders = [
        (sample.name, key, val)
        for family in REGISTRY.collect()
        if family.name.startswith("agenteval_")
        for sample in family.samples
        for key, val in sample.labels.items()
        if UUID.search(val)
    ]
    assert offenders == []


def test_no_label_value_anywhere_looks_like_an_id() -> None:
    assert_no_uuid_labels()
