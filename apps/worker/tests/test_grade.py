"""Pure parts of the Step 5 evaluation runner: grade(), the A6 rule, and the
executor's finish_attempt field selection. No Docker, no database."""

from decimal import Decimal

import pytest

from agenteval_core.evaluation.metrics import TrajectoryMetrics
from agenteval_core.evaluation.results import AttemptFacts, CheckResult, IntegrityResult
from agenteval_core.evaluation.scoring import (
    DEFAULT_SCORING_CONFIG,
    ScoringConfig,
    ScoringWeights,
)
from agenteval_core.evaluation.spec import EvaluationSpec
from agenteval_core.evaluation.taxonomy import EvaluationStatus, FailureCategory
from agenteval_core.states import Outcome
from agenteval_worker.agent_runner import AgentAttemptResult
from agenteval_worker.evaluation_runner import WorkspaceCapture, grade, needs_workspace
from agenteval_worker.executor import _finish_fields
from agenteval_worker.sandbox.evaluation import WorkspaceSnapshot

SPEC = EvaluationSpec.model_validate(
    {
        "checks": [
            {"kind": "python_tests", "name": "hidden", "expected_tests": 2},
            {"kind": "final_answer", "name": "answer", "expected": "42", "required": False},
            {"kind": "file", "name": "report", "path": "out.txt"},
        ],
        "hidden_files": {"test_x.py": "x"},
    }
)
FACTS = AttemptFacts(
    outcome=Outcome.AGENT_FINISHED,
    final_answer="42",
    steps=3,
    cost_usd=Decimal("0.10"),
    max_steps=10,
    max_cost_usd=Decimal("1.00"),
)
METRICS = TrajectoryMetrics(tool_calls=4, tool_error_rate=0.25, ran_tests=True)
CLEAN = IntegrityResult(ok=True)


def result(name: str, kind: str, passed: bool | None, **kw: object) -> CheckResult:
    base: dict[str, object] = {"required": True, "weight": 1.0, "score": 1.0 if passed else 0.0}
    base.update(kw)
    return CheckResult(name=name, kind=kind, passed=passed, **base)  # type: ignore[arg-type]


def results(**overrides: CheckResult) -> dict[str, CheckResult]:
    # Deliberately in EXECUTION order (static first), not spec order.
    out = {
        "answer": result("answer", "final_answer", True, required=False),
        "report": result("report", "file", True),
        "hidden": result("hidden", "python_tests", True, tests_passed=2, tests_total=2),
    }
    out.update(overrides)
    return out


def test_results_come_back_in_spec_order_for_public_positions() -> None:
    graded = grade(SPEC, FACTS, results(), CLEAN, METRICS, DEFAULT_SCORING_CONFIG)
    assert [c.name for c in graded.checks] == ["hidden", "answer", "report"]


def test_passing_attempt_is_scored_with_v1() -> None:
    graded = grade(SPEC, FACTS, results(), CLEAN, METRICS, DEFAULT_SCORING_CONFIG)
    assert graded.verdict.status is EvaluationStatus.SUCCEEDED
    assert graded.verdict.category is FailureCategory.PASSED
    assert graded.score is not None
    c = graded.score.components
    # efficiency = 1 - mean(3/10, 0.10/1.00) = 0.8; reliability = 1 - 0.25
    assert (c.task_success, c.test_score, c.reliability, c.efficiency) == (
        Decimal(1), Decimal(1), Decimal("0.75000"), Decimal("0.80000"),
    )  # fmt: skip
    assert graded.score.overall == Decimal("0.95500")
    assert graded.score.scoring_config_hash == DEFAULT_SCORING_CONFIG.config_hash()


def test_failing_required_check_is_a_candidate_failure() -> None:
    failing = result(
        "hidden", "python_tests", False, failure=FailureCategory.TESTS_FAILED, score=0.5
    )
    graded = grade(SPEC, FACTS, results(hidden=failing), CLEAN, METRICS, DEFAULT_SCORING_CONFIG)
    assert graded.verdict.status is EvaluationStatus.SUCCEEDED
    assert graded.verdict.task_success is False
    assert graded.verdict.category is FailureCategory.TESTS_FAILED
    assert graded.score is not None and graded.score.components.efficiency == 0


def test_a_check_that_could_not_run_is_an_evaluator_error_and_unscored() -> None:
    errored = result("report", "file", None, error="no evidence collected", score=None)
    graded = grade(SPEC, FACTS, results(report=errored), CLEAN, METRICS, DEFAULT_SCORING_CONFIG)
    assert graded.verdict.status is EvaluationStatus.ERROR
    assert graded.verdict.category is FailureCategory.EVALUATION_ERROR
    assert graded.score is None


def test_tampering_forces_overall_zero() -> None:
    tampered = IntegrityResult(ok=False, tampered={"test_calc.py": "content changed"})
    graded = grade(SPEC, FACTS, results(), tampered, METRICS, DEFAULT_SCORING_CONFIG)
    assert graded.verdict.category is FailureCategory.TAMPERING
    assert graded.score is not None
    assert graded.score.overall == 0 and graded.score.integrity_gate_applied


def test_the_given_scoring_config_is_used() -> None:
    only_success = ScoringConfig(
        weights=ScoringWeights(
            task_success=Decimal(1), test_score=Decimal(0), reliability=Decimal(0),
            efficiency=Decimal(0),
        )
    )  # fmt: skip
    graded = grade(SPEC, FACTS, results(), CLEAN, METRICS, only_success)
    assert graded.score is not None
    assert graded.score.overall == 1
    assert graded.score.scoring_config_hash == only_success.config_hash()


def test_every_spec_check_needs_a_result() -> None:
    partial = results()
    del partial["report"]
    with pytest.raises(KeyError):
        grade(SPEC, FACTS, partial, CLEAN, METRICS, DEFAULT_SCORING_CONFIG)


@pytest.mark.parametrize(
    ("spec", "needed"),
    [
        ({"checks": [{"kind": "final_answer", "name": "a", "expected": "1"}]}, False),
        (
            {
                "checks": [{"kind": "final_answer", "name": "a", "expected": "1"}],
                "protected_paths": ["t.py"],
            },
            True,
        ),
        ({"checks": [{"kind": "file", "name": "f", "path": "o.txt"}]}, True),
        (
            {
                "checks": [{"kind": "io", "name": "i", "argv": ["python"], "expected_stdout": ""}],
            },
            True,
        ),
    ],
    ids=["final-answer-only", "protected-paths", "file-check", "executing-check"],
)
def test_a6_only_workspace_specs_need_a_container(spec: dict[str, object], needed: bool) -> None:
    assert needs_workspace(EvaluationSpec.model_validate(spec)) is needed


def test_the_workspace_capture_is_never_a_finish_attempt_field() -> None:
    snap = WorkspaceSnapshot(data=b"x" * 1024, sha256="0" * 64)
    r = AgentAttemptResult(
        outcome=Outcome.AGENT_FINISHED,
        duration_ms=5,
        sandbox_image_id="sha256:abc",
        workspace=WorkspaceCapture(snapshot=snap),
    )
    fields = _finish_fields(r)
    assert "workspace" not in fields
    assert fields["sandbox_image_id"] == "sha256:abc"
    assert fields["outcome"] is Outcome.AGENT_FINISHED
