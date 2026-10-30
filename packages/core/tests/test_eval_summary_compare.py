"""Summary rates and latency, comparison helpers, the launch estimate's arithmetic and the
evaluation budget: the pure parts behind GET /evaluations/{id}/summary, /compare,
POST /evaluations/estimate and the worker's budget check."""

import uuid
from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest

from agenteval_core.agent.limits import AgentLimits, evaluation_budget_exhausted
from agenteval_core.config import Settings
from agenteval_core.evaluation.classify import platform_category
from agenteval_core.evaluation.results import AttemptFacts
from agenteval_core.evaluation.stats import success_stats
from agenteval_core.evaluation.taxonomy import PLATFORM_CATEGORIES, EvaluationStatus
from agenteval_core.evaluation.taxonomy import FailureCategory as C
from agenteval_core.reliability.taxonomy import (
    ATTEMPT_REASONS,
    FailureReason,
    ReasonCategory,
    category_of,
    is_retryable,
    outcome_allowed,
)
from agenteval_core.repositories.batches import ResultRecord
from agenteval_core.services.evaluations import (
    compare_differences,
    compare_warnings,
    intervals_disjoint,
    latency,
    nearest_rank,
    summarize,
    upper_bounds,
)
from agenteval_core.services.providers import availability, catalog
from agenteval_core.states import Outcome

EVAL = uuid.uuid4()
TASK = uuid.uuid4()


def record(
    status: str = "COMPLETED",
    *,
    outcome: str | None = "AGENT_FINISHED",
    attempt_no: int | None = 1,
    duration_ms: int | None = 100,
    eval_status: str | None = "SUCCEEDED",
    category: str | None = "passed",
    reason: str | None = None,
) -> ResultRecord:
    return ResultRecord(
        run_id=uuid.uuid4(),
        task_id=TASK,
        repetition=1,
        run_status=status,
        run_outcome=outcome,
        run_error_message=None,
        attempt_failure_reason=reason,
        attempt_no=attempt_no,
        duration_ms=duration_ms,
        steps=3,
        tokens_in=10,
        tokens_out=5,
        cost_usd=Decimal("0.001000"),
        eval_status=eval_status,
        task_success=None if eval_status != "SUCCEEDED" else category == "passed",
        failure_category=category,
        failure_tags=[],
        checks=None,
        metrics=None,
        integrity_ok=True,
        score_components=None,
        score_overall=None,
    )


def unfinished(status: str) -> ResultRecord:
    return record(
        status,
        outcome=None,
        attempt_no=None,
        duration_ms=None,
        eval_status=None,
        category=None,
    )


# -- nearest rank ------------------------------------------------------------------------------


def test_nearest_rank_is_the_value_at_rank_ceil_p_n() -> None:
    values = list(range(1, 101))  # 1..100
    assert nearest_rank(values, 0.50) == 50
    assert nearest_rank(values, 0.95) == 95
    assert nearest_rank(values, 0.99) == 99
    assert nearest_rank([7], 0.99) == 7
    # n = 10: p99 -> rank ceil(9.9) = 10, the maximum; p50 -> rank 5 (no interpolation).
    ten = [10, 20, 30, 40, 50, 60, 70, 80, 90, 1000]
    assert (nearest_rank(ten, 0.5), nearest_rank(ten, 0.95), nearest_rank(ten, 0.99)) == (
        50,
        1000,
        1000,
    )
    # n = 7 (the read API's seeded latencies): p95 is the maximum, p50 the 4th value.
    seven = [100, 200, 300, 400, 500, 600, 700]
    assert (nearest_rank(seven, 0.5), nearest_rank(seven, 0.95)) == (400, 700)


def test_latency_summary() -> None:
    assert latency([]) is None
    got = latency([100, 200, 300, 1000])
    assert got is not None
    assert (got.p50, got.p95, got.p99, got.count) == (200, 1000, 1000, 4)
    assert got.mean == pytest.approx(400.0)


# -- summary rates ----------------------------------------------------------------------------


def test_an_empty_batch_has_counts_of_zero_and_no_rates() -> None:
    s = summarize(EVAL, [], {})
    assert s.complete and s.latency_ms is None and not s.simulated
    assert s.rates.model_dump() == {
        "scheduled": 0,
        "finished": 0,
        "completed": 0,
        "failed": 0,
        "timed_out": 0,
        "retried": 0,
        "completion_rate": None,
        "failure_rate": None,
        "timeout_rate": None,
        "retry_rate": None,
    }


def test_an_incomplete_batch_rates_only_its_finished_runs() -> None:
    records = [
        record(duration_ms=100),
        record(duration_ms=300, category="tests_failed"),
        unfinished("QUEUED"),
        unfinished("RUNNING"),
        unfinished("CANCELLED"),
    ]
    s = summarize(EVAL, records, {}, simulated=True)
    assert not s.complete and s.in_progress == 2 and s.simulated
    r = s.rates
    assert (r.scheduled, r.finished, r.completed, r.failed) == (4, 2, 2, 0)
    assert (r.completion_rate, r.failure_rate, r.timeout_rate, r.retry_rate) == (1.0, 0.0, 0.0, 0.0)
    assert s.latency_ms is not None
    assert (s.latency_ms.count, s.latency_ms.p50, s.latency_ms.p99) == (2, 100, 300)
    assert s.latency_ms.mean == 200.0


def test_failed_timed_out_and_retried_runs() -> None:
    records = [
        record(duration_ms=100),  # passed on attempt 1
        record(attempt_no=2, duration_ms=200),  # passed after a retry
        record(outcome="TIMEOUT", duration_ms=900, category="timeout"),
        record(  # a platform failure: FAILED, excluded from the pass rate
            "FAILED",
            outcome="SANDBOX_ERROR",
            attempt_no=3,
            duration_ms=50,
            eval_status="SKIPPED",
            category="platform_sandbox",
            reason="sandbox_lost",
        ),
        record(  # FAILED before any attempt existed
            "FAILED",
            outcome="INTERNAL_ERROR",
            attempt_no=None,
            duration_ms=None,
            eval_status=None,
            category=None,
            reason=None,
        ),
    ]
    s = summarize(EVAL, records, {})
    r = s.rates
    assert (r.scheduled, r.finished, r.completed, r.failed, r.timed_out, r.retried) == (
        5,
        5,
        3,
        2,
        1,
        2,
    )
    assert r.completion_rate == pytest.approx(0.6)
    assert r.failure_rate == pytest.approx(0.4)
    assert r.timeout_rate == pytest.approx(0.2)
    assert r.retry_rate == pytest.approx(0.4)
    # Pass rate stays D7: passed / graded, platform failures out of the denominator.
    assert (s.graded, s.passed, s.platform_failures) == (3, 2, 2)
    assert s.success_rate == pytest.approx(2 / 3)
    # Latency over the finished attempts that exist (4: the attempt-less run has none).
    assert s.latency_ms is not None
    assert (s.latency_ms.count, s.latency_ms.p50, s.latency_ms.p95) == (4, 100, 900)


# -- compare -----------------------------------------------------------------------------------


def test_intervals_disjoint() -> None:
    assert intervals_disjoint(None, (0.1, 0.2)) is None
    assert intervals_disjoint((0.1, 0.2), None) is None
    assert intervals_disjoint((0.1, 0.3), (0.4, 0.9)) is True
    assert intervals_disjoint((0.4, 0.9), (0.1, 0.3)) is True
    assert intervals_disjoint((0.1, 0.5), (0.4, 0.9)) is False
    assert intervals_disjoint((0.1, 0.4), (0.4, 0.9)) is False  # touching overlaps


def test_differences_list_only_the_fields_that_differ() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    same: dict[str, Any] = {
        "provider": "fake",
        "model": "fake-model",
        "agent_config_hash": "h1",
        "agent_name": "coder",
        "repetitions": 3,
    }
    assert compare_differences([(a, same), (b, same)]) == []
    other = {**same, "model": "other", "repetitions": 1}
    diffs = compare_differences([(a, same), (b, other)])
    assert [(d.field, d.values) for d in diffs] == [
        ("model", {a: "fake-model", b: "other"}),
        ("repetitions", {a: 3, b: 1}),
    ]


def test_warnings_for_mixed_simulation_small_samples_and_incomplete_batches() -> None:
    graded = [record() for _ in range(30)]
    big = summarize(EVAL, graded, {}, simulated=True)
    small = summarize(EVAL, graded[:4] + [unfinished("RUNNING")], {}, simulated=False)
    assert compare_warnings([("a", big), ("b", big)]) == []
    assert compare_warnings([("a", big), ("b", small)]) == [
        "Mixes simulated (fake-model) and real-model evaluations; their scores and costs "
        "are not comparable",
        "b has only 4 graded attempts; treat differences as indicative",
        "b is not complete; its numbers can still change",
    ]


# -- estimate arithmetic and provider availability --------------------------------------------


def test_upper_bounds_follow_the_version_limits() -> None:
    defaults = AgentLimits.model_validate({})  # 30 steps, 1.00 USD per attempt
    assert upper_bounds(10, defaults, 1, priced=True) == (300, Decimal("10.00"))
    custom = AgentLimits(max_steps=12, max_cost_usd=Decimal("0.25"))
    assert upper_bounds(6, custom, 3, priced=True) == (216, Decimal("4.50"))
    assert upper_bounds(6, custom, 3, priced=False) == (216, None)
    assert upper_bounds(0, custom, 3, priced=True) == (0, Decimal("0.00"))


def test_availability_and_catalog() -> None:
    base = Settings(_env_file=None)  # type: ignore[call-arg]
    assert availability(base, "fake") == (True, None)
    ok, reason = availability(base, "anthropic")
    assert not ok and reason == ("Not enabled on this server (AGENTEVAL_ENABLE_ANTHROPIC_PROVIDER)")
    enabled = base.model_copy(update={"enable_anthropic_provider": True})
    assert availability(enabled, "anthropic") == (True, None)
    demo = enabled.model_copy(update={"public_demo": True})
    assert availability(demo, "anthropic") == (
        False,
        "The public demo only runs simulated agents",
    )
    assert availability(demo, "fake") == (True, None)

    body = catalog(base).model_dump(mode="json")
    assert body["pricing_version"] and body["real_provider_concurrency"] == 1
    assert body["default_real_budget_usd"] == "2.00"
    fake, anthropic = body["providers"]
    assert fake == {
        "name": "fake",
        "simulated": True,
        "available": True,
        "reason": None,
        "models": [
            {
                "id": "fake-model",
                "priced": True,
                "input_usd_per_mtok": "1",
                "output_usd_per_mtok": "5",
            }
        ],
    }
    assert anthropic["name"] == "anthropic" and not anthropic["simulated"]
    assert {"id": "claude-haiku-4-5", "priced": True, "input_usd_per_mtok": "1",
            "output_usd_per_mtok": "5"} in anthropic["models"]  # fmt: skip


# -- evaluation budget -------------------------------------------------------------------------


def test_budget_exhaustion() -> None:
    assert not evaluation_budget_exhausted(Decimal("5"), None)
    assert not evaluation_budget_exhausted(Decimal("1.999999"), Decimal("2"))
    assert evaluation_budget_exhausted(Decimal("2"), Decimal("2.000000"))
    assert evaluation_budget_exhausted(Decimal("2.5"), Decimal("2"))


def test_budget_exhaustion_is_a_non_retryable_platform_failure() -> None:
    reason = FailureReason.EVALUATION_BUDGET_EXHAUSTED
    assert reason in ATTEMPT_REASONS
    assert category_of(reason) is ReasonCategory.CONFIG
    assert not is_retryable(reason)
    assert outcome_allowed(reason, Outcome.INTERNAL_ERROR)
    assert not outcome_allowed(reason, Outcome.MODEL_ERROR)
    category = platform_category(AttemptFacts(Outcome.INTERNAL_ERROR, None, failure_reason=reason))
    assert category is C.PLATFORM_INTERNAL and category in PLATFORM_CATEGORIES
    # So it never enters the pass-rate denominator.
    stats = success_stats([(EvaluationStatus.SKIPPED, category)])
    assert (stats.graded, stats.platform_failures, stats.success_rate) == (0, 1, None)


def test_a_budget_stopped_run_counts_as_failed_not_graded() -> None:
    stopped = replace(
        record(
            "FAILED",
            outcome="INTERNAL_ERROR",
            duration_ms=0,
            eval_status="SKIPPED",
            category="platform_internal",
        ),
        attempt_failure_reason="evaluation_budget_exhausted",
    )
    s = summarize(EVAL, [record(), stopped], {})
    assert (s.graded, s.passed, s.platform_failures, s.success_rate) == (1, 1, 1, 1.0)
    assert (s.rates.failed, s.rates.failure_rate) == (1, 0.5)
