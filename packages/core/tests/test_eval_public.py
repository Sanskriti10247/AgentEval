"""D11, strict: the public projection carries no evaluator-only data at all."""

import typing
from decimal import Decimal
from enum import Enum
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import BaseModel

from agenteval_core.evaluation.checks import evidence_for, judge, judge_static
from agenteval_core.evaluation.classify import Verdict, classify
from agenteval_core.evaluation.integrity import assess, baseline
from agenteval_core.evaluation.metrics import TrajectoryMetrics
from agenteval_core.evaluation.public import PublicEvaluation, public_view
from agenteval_core.evaluation.results import AttemptFacts, CheckResult
from agenteval_core.evaluation.scoring import components, score
from agenteval_core.evaluation.spec import EvaluationSpec
from agenteval_core.evaluation.taxonomy import TAG_VOCABULARY, EvaluationStatus
from agenteval_core.states import Outcome
from agenteval_core.tools.workspace import ExecOutput


def _string_fields(model: type[BaseModel], prefix: str = "") -> list[str]:
    """Every field whose type admits a free-form str (enums don't count)."""
    found = []
    for name, info in model.model_fields.items():
        for arg in typing.get_args(info.annotation) or (info.annotation,):
            if isinstance(arg, type) and issubclass(arg, BaseModel):
                found += _string_fields(arg, f"{prefix}{name}.")
            elif (
                arg is str or typing.get_origin(arg) in (list, dict) and str in typing.get_args(arg)
            ):
                found.append(prefix + name)
            elif isinstance(arg, type) and issubclass(arg, Enum):
                continue
    return found


def test_the_public_model_has_no_free_text_fields() -> None:
    """Structural guarantee: only `tags` holds strings, and those come from a
    closed vocabulary that public_view() enforces. TrajectoryMetrics' string
    fields are tool statuses and runtime limit names (agent behaviour, not
    evaluator configuration)."""
    assert _string_fields(PublicEvaluation) == [
        "tags",
        "metrics.tool_status_counts",
        "metrics.limits_reached",
    ]


def test_public_checks_carry_only_position_and_outcome() -> None:
    from agenteval_core.evaluation.public import PublicCheck

    assert set(PublicCheck.model_fields) == {"position", "passed", "score", "duration_ms"}


def test_tags_outside_the_vocabulary_are_refused() -> None:
    verdict = Verdict(EvaluationStatus.SKIPPED, None, None, ("protected:calc.py",), "x")
    with pytest.raises(ValueError, match="vocabulary"):
        public_view(verdict, [])


# Evaluator-only data, each value carrying a unique marker.
NAMES = st.from_regex(r"SECRETNAME[A-Z]{6}", fullmatch=True)


@settings(max_examples=100)
@given(NAMES, st.integers(1, 999), st.text(max_size=20))
def test_no_evaluator_configuration_reaches_the_public_view(
    marker: str, expected_tests: int, answer: str
) -> None:
    path = f"{marker}_out.txt"
    protected = f"{marker}_protected.py"
    spec = EvaluationSpec.model_validate(
        {
            "checks": [
                {
                    "kind": "python_tests",
                    "name": f"{marker}tests",
                    "pattern": f"{marker}*.py",
                    "expected_tests": expected_tests,
                },
                {
                    "kind": "io",
                    "name": f"{marker}io",
                    "argv": ["python", f"{marker}.py"],
                    "stdin": marker,
                    "expected_stdout": marker,
                    "match": "contains",
                },
                {"kind": "final_answer", "name": f"{marker}ans", "expected": marker},
                {
                    "kind": "file",
                    "name": f"{marker}file",
                    "path": path,
                    "mode": "equals",
                    "expected": marker,
                },
            ],
            "hidden_files": {f"{marker}hidden.py": f"# {marker}"},
            "protected_paths": [protected],
            "timeout_s": 600,
        }
    )
    task_files = {protected: "original"}
    leaky = ExecOutput(
        exit_code=1, stdout=marker, stderr=f"{marker}\nRan 1 test in 0.1s\n\nFAILED (failures=1)\n"
    )
    evidence = {
        path: evidence_for(path, answer.encode()),
        protected: evidence_for(protected, b"edited"),
    }
    checks: list[CheckResult] = []
    for c in spec.checks:
        if c.kind in ("final_answer", "file"):
            checks.append(judge_static(c, final_answer=answer, evidence=evidence))
        else:
            checks.append(judge(c, leaky))
    integrity = assess(baseline(task_files, spec.protected_paths), evidence)
    facts = AttemptFacts(outcome=Outcome.AGENT_FINISHED, final_answer=answer)
    metrics = TrajectoryMetrics()
    verdict = classify(facts, has_spec=True, checks=checks, integrity=integrity, metrics=metrics)
    scored = score(components(verdict, checks, metrics, facts), integrity=integrity)

    # The internal records DO name checks and paths (that's allowed internally)...
    assert marker in "".join(c.name for c in checks) and marker in verdict.detail
    # ...the public projection never does.
    public = public_view(verdict, checks, score=scored, metrics=metrics).model_dump_json()
    assert marker not in public
    assert set(public_view(verdict, checks).tags) <= TAG_VOCABULARY
    assert f'"expected_tests":{expected_tests}' not in public


def test_public_view_of_a_graded_attempt() -> None:
    check = CheckResult(
        name="hidden",
        kind="python_tests",
        required=True,
        weight=1.0,
        passed=True,
        score=1.0,
        duration_ms=840,
    )
    facts = AttemptFacts(outcome=Outcome.AGENT_FINISHED, final_answer="done")
    from agenteval_core.evaluation.results import IntegrityResult

    clean = IntegrityResult(ok=True)
    verdict = classify(
        facts, has_spec=True, checks=[check], integrity=clean, metrics=TrajectoryMetrics()
    )
    view = public_view(
        verdict, [check], score=score(components(verdict, [check], None, facts), integrity=clean)
    )
    data: dict[str, Any] = view.model_dump()
    assert data["status"] == "SUCCEEDED" and data["failure_category"] == "passed"
    assert data["checks"] == [{"position": 1, "passed": True, "score": 1.0, "duration_ms": 840}]
    assert data["score"]["overall"] == Decimal(
        "0.80000"
    )  # 0.6 + 0.2, no metrics, no efficiency inputs
