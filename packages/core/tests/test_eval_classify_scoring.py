"""Failure classification (D10), scoring v1 (D6) and success-rate semantics (D7)."""

from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from agenteval_core.evaluation.classify import classify
from agenteval_core.evaluation.metrics import TrajectoryMetrics
from agenteval_core.evaluation.results import AttemptFacts, CheckResult, IntegrityResult
from agenteval_core.evaluation.scoring import (
    DEFAULT_SCORING_CONFIG,
    ScoringConfig,
    ScoringWeights,
    components,
    score,
)
from agenteval_core.evaluation.stats import success_stats, wilson_interval
from agenteval_core.evaluation.taxonomy import EvaluationStatus as S
from agenteval_core.evaluation.taxonomy import FailureCategory as C
from agenteval_core.reliability.taxonomy import FailureReason as FR
from agenteval_core.states import Outcome as O

CLEAN = IntegrityResult(ok=True)
TAMPERED = IntegrityResult(ok=False, tampered={"test_visible.py": "content changed"})
METRICS = TrajectoryMetrics(tool_calls=4, agent_tool_errors=1, tool_error_rate=0.25, ran_tests=True)


def check(
    name: str = "hidden",
    passed: bool | None = True,
    *,
    kind: str = "python_tests",
    failure: C | None = None,
    required: bool = True,
    score_: float | None = None,
    error: str | None = None,
    weight: float = 1.0,
) -> CheckResult:
    return CheckResult(
        name=name,
        kind=kind,
        required=required,
        weight=weight,
        passed=passed,
        score=(1.0 if passed else 0.0) if score_ is None and passed is not None else score_,
        failure=None if passed else (failure or C.TESTS_FAILED),
        error=error,
        detail="ok" if passed else "failed",
    )


def facts(outcome: O = O.AGENT_FINISHED, **kw: Any) -> AttemptFacts:
    return AttemptFacts(outcome=outcome, final_answer=kw.pop("final_answer", "done"), **kw)


# -- classification: one row per rule, in precedence order --------------------------------------

ROWS: list[tuple[str, AttemptFacts, dict[str, Any], S, C | None, bool | None]] = [
    ("sandbox error", facts(O.SANDBOX_ERROR), {}, S.SKIPPED, C.PLATFORM_SANDBOX, None),
    ("model error", facts(O.MODEL_ERROR), {}, S.SKIPPED, C.PLATFORM_MODEL, None),
    (
        "internal error",
        facts(O.INTERNAL_ERROR, error_message="boom"),
        {},
        S.SKIPPED,
        C.PLATFORM_INTERNAL,
        None,
    ),
    (
        "cancelled",  # legacy fallback: no reason yet, driven by the error_message string
        facts(O.INTERNAL_ERROR, error_message="cancelled: worker_shutdown"),
        {},
        S.SKIPPED,
        C.PLATFORM_CANCELLED,
        None,
    ),
    (
        "cancelled by reason",  # Step 3: driven by failure_reason, not the message
        facts(O.INTERNAL_ERROR, error_message="irrelevant", failure_reason=FR.WORKER_SHUTDOWN),
        {},
        S.SKIPPED,
        C.PLATFORM_CANCELLED,
        None,
    ),
    (
        "sandbox error by reason",  # a non-cancellation reason doesn't change the category
        facts(O.SANDBOX_ERROR, failure_reason=FR.SANDBOX_CREATE_FAILED),
        {},
        S.SKIPPED,
        C.PLATFORM_SANDBOX,
        None,
    ),
    (
        "unclassified legacy row still falls back to the message",
        facts(
            O.INTERNAL_ERROR,
            error_message="cancelled: worker_shutdown",
            failure_reason=FR.UNCLASSIFIED,
        ),
        {},
        S.SKIPPED,
        C.PLATFORM_CANCELLED,
        None,
    ),
    (
        "platform beats a spec",
        facts(O.MODEL_ERROR),
        {"checks": [check(passed=False)]},
        S.SKIPPED,
        C.PLATFORM_MODEL,
        None,
    ),
    (
        "check could not run",
        facts(),
        {"checks": [check(passed=None, error="lost")]},
        S.ERROR,
        C.EVALUATION_ERROR,
        None,
    ),
    (
        "integrity unavailable",
        facts(),
        {"checks": [check()], "integrity": None},
        S.ERROR,
        C.EVALUATION_ERROR,
        None,
    ),
    (
        "tampering beats passing checks",
        facts(),
        {"checks": [check()], "integrity": TAMPERED},
        S.SUCCEEDED,
        C.TAMPERING,
        False,
    ),
    ("passed", facts(), {"checks": [check()]}, S.SUCCEEDED, C.PASSED, True),
    (
        "passed despite max_steps",
        facts(O.MAX_STEPS),
        {"checks": [check()]},
        S.SUCCEEDED,
        C.PASSED,
        True,
    ),
    (
        "max_steps",
        facts(O.MAX_STEPS),
        {"checks": [check(passed=False)]},
        S.SUCCEEDED,
        C.MAX_STEPS,
        False,
    ),
    (
        "budget",
        facts(O.BUDGET_EXCEEDED),
        {"checks": [check(passed=False)]},
        S.SUCCEEDED,
        C.BUDGET_EXCEEDED,
        False,
    ),
    ("timeout", facts(O.TIMEOUT), {"checks": [check(passed=False)]}, S.SUCCEEDED, C.TIMEOUT, False),
    (
        "refusal",
        facts(O.MODEL_REFUSAL, final_answer=None),
        {"checks": [check(passed=False)]},
        S.SUCCEEDED,
        C.REFUSAL,
        False,
    ),
    (
        "no answer",
        facts(final_answer=""),
        {"checks": [check("ans", False, kind="final_answer", failure=C.NO_ANSWER)]},
        S.SUCCEEDED,
        C.NO_ANSWER,
        False,
    ),
    (
        "tests failed",
        facts(),
        {"checks": [check(passed=False)]},
        S.SUCCEEDED,
        C.TESTS_FAILED,
        False,
    ),
    (
        "wrong answer",
        facts(),
        {"checks": [check("ans", False, kind="final_answer", failure=C.WRONG_ANSWER)]},
        S.SUCCEEDED,
        C.WRONG_ANSWER,
        False,
    ),
    (
        "missing output",
        facts(),
        {"checks": [check("out", False, kind="file", failure=C.MISSING_OUTPUT)]},
        S.SUCCEEDED,
        C.MISSING_OUTPUT,
        False,
    ),
    (
        "optional failure doesn't matter",
        facts(),
        {"checks": [check(), check("extra", False, required=False)]},
        S.SUCCEEDED,
        C.PASSED,
        True,
    ),
    ("no spec, finished", facts(), {"has_spec": False}, S.SKIPPED, None, None),
    ("no spec, max_steps", facts(O.MAX_STEPS), {"has_spec": False}, S.SKIPPED, C.MAX_STEPS, None),
]


@pytest.mark.parametrize(
    ("label", "f", "kw", "status", "category", "success"), ROWS, ids=[r[0] for r in ROWS]
)
def test_classification(
    label: str,
    f: AttemptFacts,
    kw: dict[str, Any],
    status: S,
    category: C | None,
    success: bool | None,
) -> None:
    args: dict[str, Any] = {"has_spec": True, "integrity": CLEAN, "metrics": METRICS, **kw}
    v = classify(f, **args)
    assert (v.status, v.category, v.task_success) == (status, category, success)


def test_secondary_matches_become_tags() -> None:
    v = classify(
        facts(O.MAX_STEPS),
        has_spec=True,
        integrity=TAMPERED,
        metrics=METRICS,
        checks=[check(passed=False)],
    )
    assert v.category is C.TAMPERING
    assert {"max_steps", "tests_failed"} <= set(v.tags)


def test_diagnostic_tags() -> None:
    noisy = TrajectoryMetrics(
        tool_calls=8, agent_tool_errors=4, tool_error_rate=0.5, loop_detected=True, model_errors=2
    )
    normalized = IntegrityResult(ok=True, normalized_paths=["calc.py"])
    v = classify(facts(), has_spec=True, checks=[check()], integrity=normalized, metrics=noisy)
    assert v.category is C.PASSED
    assert set(v.tags) == {
        "loop_detected",
        "high_tool_error_rate",
        "never_ran_tests",
        "model_retries",
        "permissions_normalized",
    }


def test_command_outcomes_are_rejected() -> None:
    with pytest.raises(ValueError, match="D4"):
        classify(facts(O.SUCCESS), has_spec=True, checks=[check()], integrity=CLEAN)


# -- scoring v1 -----------------------------------------------------------------------------------


def test_default_config_is_v1_benchmark_policy() -> None:
    w = DEFAULT_SCORING_CONFIG.weights
    assert (w.task_success, w.test_score, w.reliability, w.efficiency) == (
        Decimal("0.60"),
        Decimal("0.20"),
        Decimal("0.10"),
        Decimal("0.10"),
    )
    assert len(DEFAULT_SCORING_CONFIG.config_hash()) == 64


def test_full_score_by_hand() -> None:
    f = facts(steps=6, max_steps=30, cost_usd=Decimal("0.10"), max_cost_usd=Decimal("1.00"))
    checks = [check()]
    v = classify(f, has_spec=True, checks=checks, integrity=CLEAN, metrics=METRICS)
    comps = components(v, checks, METRICS, f)
    # efficiency = 1 - mean(6/30, 0.10/1.00) = 1 - 0.15 = 0.85; reliability = 1 - 0.25
    assert comps.model_dump() == {
        "task_success": Decimal(1),
        "test_score": Decimal("1.00000"),
        "reliability": Decimal("0.75000"),
        "efficiency": Decimal("0.85000"),
    }
    result = score(comps, integrity=CLEAN)
    # 0.6*1 + 0.2*1 + 0.1*0.75 + 0.1*0.85 = 0.96
    assert result.overall == Decimal("0.96000") and not result.integrity_gate_applied


def test_failure_earns_no_efficiency() -> None:
    f = facts(steps=1, max_steps=30, cost_usd=Decimal("0.001"), max_cost_usd=Decimal("1"))
    checks = [check(passed=False, score_=0.5)]
    v = classify(f, has_spec=True, checks=checks, integrity=CLEAN, metrics=METRICS)
    comps = components(v, checks, METRICS, f)
    assert (comps.task_success, comps.efficiency, comps.test_score) == (
        Decimal(0),
        Decimal(0),
        Decimal("0.50000"),
    )
    assert score(comps, integrity=CLEAN).overall == Decimal("0.17500")  # 0.2*0.5 + 0.1*0.75


def test_tampering_forces_zero() -> None:
    checks = [check()]
    v = classify(facts(), has_spec=True, checks=checks, integrity=TAMPERED, metrics=METRICS)
    result = score(components(v, checks, METRICS, facts()), integrity=TAMPERED)
    assert result.overall == Decimal("0.00000") and result.integrity_gate_applied


def test_test_score_is_the_weighted_mean_of_check_scores() -> None:
    checks = [check("a", passed=False, score_=0.5, weight=3.0), check("b", weight=1.0)]
    v = classify(facts(), has_spec=True, checks=checks, integrity=CLEAN, metrics=METRICS)
    assert components(v, checks, METRICS, facts()).test_score == Decimal("0.62500")


def test_no_tool_calls_means_full_reliability() -> None:
    v = classify(facts(), has_spec=True, checks=[check()], integrity=CLEAN)
    comps = components(v, [check()], TrajectoryMetrics(), facts())
    assert comps.reliability == Decimal(1)


def test_only_graded_evaluations_are_scored() -> None:
    v = classify(facts(O.MODEL_ERROR), has_spec=True)
    with pytest.raises(ValueError, match="SUCCEEDED"):
        components(v, [], None, facts())


def test_custom_weights_and_validation() -> None:
    cfg = ScoringConfig(
        weights=ScoringWeights(
            task_success=Decimal(1),
            test_score=Decimal(0),
            reliability=Decimal(0),
            efficiency=Decimal(0),
        )
    )
    assert cfg.config_hash() != DEFAULT_SCORING_CONFIG.config_hash()
    checks = [check(passed=False, score_=0.9)]
    v = classify(facts(), has_spec=True, checks=checks, integrity=CLEAN, metrics=METRICS)
    assert score(components(v, checks, METRICS, facts()), cfg, integrity=CLEAN).overall == 0
    with pytest.raises(ValidationError, match="sum to exactly 1"):
        ScoringWeights(
            task_success=Decimal("0.5"),
            test_score=Decimal("0.2"),
            reliability=Decimal("0.1"),
            efficiency=Decimal("0.1"),
        )
    with pytest.raises(ValidationError):
        ScoringWeights(
            task_success=Decimal("1.2"),
            test_score=Decimal("-0.2"),
            reliability=Decimal(0),
            efficiency=Decimal(0),
        )


def test_weights_from_json_floats_are_exact() -> None:
    cfg = ScoringConfig.model_validate(
        {"weights": {"task_success": 0.6, "test_score": 0.2, "reliability": 0.1, "efficiency": 0.1}}
    )
    assert (
        cfg.config_hash() == ScoringConfig.model_validate_json(cfg.model_dump_json()).config_hash()
    )


# -- success rate (D7) ----------------------------------------------------------------------------


def test_success_stats_exclude_platform_and_evaluator_failures() -> None:
    results = (
        [(S.SUCCEEDED, C.PASSED)] * 6
        + [(S.SUCCEEDED, C.TESTS_FAILED)] * 2
        + [(S.SUCCEEDED, C.MAX_STEPS)] * 2  # agent's fault: in the denominator
        + [(S.SKIPPED, C.PLATFORM_MODEL)] * 3
        + [(S.ERROR, C.EVALUATION_ERROR)]
        + [(S.SKIPPED, None)]
    )
    st = success_stats(results)
    assert (st.attempts, st.graded, st.passed, st.success_rate) == (15, 10, 6, 0.6)
    assert (st.platform_failures, st.platform_failure_rate) == (3, 0.2)
    assert (st.evaluation_errors, st.ungraded) == (1, 1)
    assert st.ci95 is not None and st.ci95[0] < 0.6 < st.ci95[1]


def test_wilson_reference_values() -> None:
    low, high = wilson_interval(6, 10) or (0, 0)
    assert (round(low, 4), round(high, 4)) == (0.3127, 0.8318)  # standard Wilson 95% values
    assert wilson_interval(0, 0) is None
    assert wilson_interval(0, 5) is not None and wilson_interval(0, 5)[0] == 0.0  # type: ignore[index]
    with pytest.raises(ValueError):
        wilson_interval(6, 5)


def test_no_graded_attempts() -> None:
    st = success_stats([(S.SKIPPED, C.PLATFORM_SANDBOX)])
    assert (st.success_rate, st.ci95, st.platform_failure_rate) == (None, None, 1.0)
