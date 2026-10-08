"""Property tests (hypothesis): invariants that must hold for ANY input."""

from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from agenteval_core.evaluation.checks import evidence_for, judge, judge_static, plan
from agenteval_core.evaluation.classify import classify
from agenteval_core.evaluation.matchers import match_text, normalize
from agenteval_core.evaluation.metrics import TrajectoryMetrics
from agenteval_core.evaluation.parsers import parse_pytest, parse_unittest
from agenteval_core.evaluation.results import AttemptFacts, CheckResult, IntegrityResult
from agenteval_core.evaluation.scoring import (
    ScoreComponents,
    ScoringConfig,
    ScoringWeights,
    components,
    score,
)
from agenteval_core.evaluation.spec import EvaluationSpec
from agenteval_core.evaluation.stats import success_stats, wilson_interval
from agenteval_core.evaluation.taxonomy import (
    PLATFORM_CATEGORIES,
    EvaluationStatus,
    FailureCategory,
)
from agenteval_core.hashing import config_hash_of
from agenteval_core.states import Outcome
from agenteval_core.tools.workspace import ExecOutput

unit = st.decimals(min_value=0, max_value=1, places=5, allow_nan=False, allow_infinity=False)
AGENT_OUTCOMES = [
    Outcome.AGENT_FINISHED,
    Outcome.MAX_STEPS,
    Outcome.BUDGET_EXCEEDED,
    Outcome.TIMEOUT,
    Outcome.MODEL_REFUSAL,
    Outcome.MODEL_ERROR,
    Outcome.SANDBOX_ERROR,
    Outcome.INTERNAL_ERROR,
]


@st.composite
def weights(draw: st.DrawFn) -> ScoringWeights:
    a = draw(unit)
    b = draw(unit.filter(lambda x: x <= 1 - a))
    c = draw(unit.filter(lambda x: x <= 1 - a - b))
    return ScoringWeights(task_success=a, test_score=b, reliability=c, efficiency=1 - a - b - c)


@st.composite
def comps(draw: st.DrawFn) -> ScoreComponents:
    success = draw(st.sampled_from([Decimal(0), Decimal(1)]))
    return ScoreComponents(
        task_success=success,
        test_score=draw(st.none() | unit),
        reliability=draw(st.none() | unit),
        efficiency=draw(unit) if success else Decimal(0),
    )


@given(comps(), weights(), st.booleans())
def test_overall_is_in_unit_interval_and_gated(
    c: ScoreComponents, w: ScoringWeights, tampered: bool
) -> None:
    integrity = IntegrityResult(
        ok=not tampered, tampered={"x": "content changed"} if tampered else {}
    )
    r = score(c, ScoringConfig(weights=w), integrity=integrity)
    assert Decimal(0) <= r.overall <= Decimal(1)
    assert r.overall.as_tuple().exponent == -5  # always 5 places (NUMERIC(6,5))
    if tampered:
        assert r.overall == 0


@given(comps(), weights())
def test_success_never_lowers_the_score(c: ScoreComponents, w: ScoringWeights) -> None:
    clean = IntegrityResult(ok=True)
    failed = c.model_copy(update={"task_success": Decimal(0), "efficiency": Decimal(0)})
    succeeded = c.model_copy(update={"task_success": Decimal(1)})
    cfg = ScoringConfig(weights=w)
    assert (
        score(succeeded, cfg, integrity=clean).overall
        >= score(failed, cfg, integrity=clean).overall
    )


@given(comps(), weights())
def test_scoring_is_deterministic(c: ScoreComponents, w: ScoringWeights) -> None:
    cfg, clean = ScoringConfig(weights=w), IntegrityResult(ok=True)
    assert score(c, cfg, integrity=clean) == score(c, cfg, integrity=clean)
    roundtrip = ScoringConfig.model_validate_json(cfg.model_dump_json())
    assert roundtrip.config_hash() == cfg.config_hash()


@given(st.dictionaries(st.text(max_size=5), st.integers() | st.text(max_size=5), max_size=6))
def test_hash_ignores_key_order(d: dict) -> None:  # type: ignore[type-arg]
    assert config_hash_of(d) == config_hash_of(dict(reversed(list(d.items()))))


@st.composite
def check_results(draw: st.DrawFn) -> list[CheckResult]:
    out = []
    for i in range(draw(st.integers(1, 4))):
        passed = draw(st.booleans() | st.none())
        out.append(
            CheckResult(
                name=f"c{i}",
                kind=draw(st.sampled_from(["python_tests", "io", "file", "final_answer"])),
                required=i == 0 or draw(st.booleans()),
                weight=1.0,
                passed=passed,
                score=None if passed is None else float(passed),
                error="x" if passed is None else None,
                failure=None if passed else FailureCategory.TESTS_FAILED,
            )
        )
    return out


@settings(max_examples=300)
@given(
    st.sampled_from(AGENT_OUTCOMES),
    check_results(),
    st.booleans(),
    st.booleans(),
    st.text(max_size=3) | st.none(),
)
def test_classification_invariants(
    outcome: Outcome, checks: list[CheckResult], tampered: bool, has_spec: bool, answer: str | None
) -> None:
    integrity = IntegrityResult(ok=not tampered, tampered={"x": "missing"} if tampered else {})
    v = classify(
        AttemptFacts(outcome=outcome, final_answer=answer),
        has_spec=has_spec,
        checks=checks,
        integrity=integrity,
        metrics=TrajectoryMetrics(),
    )
    if outcome in (Outcome.MODEL_ERROR, Outcome.SANDBOX_ERROR, Outcome.INTERNAL_ERROR):
        assert v.status is EvaluationStatus.SKIPPED and v.category in PLATFORM_CATEGORIES
    if v.status is EvaluationStatus.SUCCEEDED:
        assert v.task_success == (v.category is FailureCategory.PASSED)
        assert v.task_success == (not tampered and all(c.passed for c in checks if c.required))
        if tampered:
            assert v.category is FailureCategory.TAMPERING
    else:
        assert v.task_success is None
    if v.category is not None:
        assert v.category.value not in v.tags  # the primary category is never also a tag
    if v.status is EvaluationStatus.SUCCEEDED:
        result = score(
            components(v, checks, TrajectoryMetrics(), AttemptFacts(outcome=outcome)),
            integrity=integrity,
        )
        assert 0 <= result.overall <= 1


@given(st.integers(0, 500), st.integers(0, 500))
def test_wilson_bounds(k: int, extra: int) -> None:
    n = k + extra
    ci = wilson_interval(k, n)
    if n == 0:
        assert ci is None
    else:
        assert ci is not None and 0 <= ci[0] <= k / n <= ci[1] <= 1


@given(
    st.lists(
        st.sampled_from(
            [
                (EvaluationStatus.SUCCEEDED, FailureCategory.PASSED),
                (EvaluationStatus.SUCCEEDED, FailureCategory.TESTS_FAILED),
                (EvaluationStatus.SKIPPED, FailureCategory.PLATFORM_MODEL),
                (EvaluationStatus.ERROR, FailureCategory.EVALUATION_ERROR),
                (EvaluationStatus.SKIPPED, None),
            ]
        ),
        max_size=50,
    )
)
def test_success_stats_partition_attempts(rows: list) -> None:  # type: ignore[type-arg]
    s = success_stats(rows)
    assert s.graded + s.platform_failures + s.evaluation_errors + s.ungraded == s.attempts
    assert s.passed <= s.graded


@given(st.text(max_size=40))
def test_normalize_is_idempotent_and_exact_implies_normalized(text: str) -> None:
    assert normalize(normalize(text)) == normalize(text)
    assert match_text("normalized", text, text).ok and match_text("exact", text, text).ok


@given(st.integers(0, 300), st.integers(0, 50), st.integers(0, 50), st.integers(0, 20))
def test_unittest_summary_round_trip(passed: int, failures: int, errors: int, skipped: int) -> None:
    total = passed + failures + errors + skipped
    bits = [
        f"{k}={v}"
        for k, v in (("failures", failures), ("errors", errors), ("skipped", skipped))
        if v
    ]
    verdict = "FAILED" if failures or errors else "OK"
    line = f"{verdict} ({', '.join(bits)})" if bits else verdict
    counts = parse_unittest(
        f"{'-' * 70}\nRan {total} test{'s' * (total != 1)} in 0.01s\n\n{line}\n"
    )
    assert counts is not None
    assert (counts.total, counts.passed, counts.failed, counts.skipped) == (
        total,
        passed,
        failures + errors,
        skipped,
    )


@given(st.integers(0, 300), st.integers(0, 50), st.integers(0, 20))
def test_pytest_summary_round_trip(passed: int, failed: int, skipped: int) -> None:
    parts = [
        f"{n} {w}" for n, w in ((passed, "passed"), (failed, "failed"), (skipped, "skipped")) if n
    ]
    if not parts:
        return
    counts = parse_pytest(f"{', '.join(parts)} in 0.12s\n")
    assert counts is not None and (counts.passed, counts.failed, counts.skipped) == (
        passed,
        failed,
        skipped,
    )


# -- D11: hidden evaluation data never reaches results --------------------------------------------

secret = st.text(alphabet="ABCDEFGHJKMNPQRSTUVWXYZ23456789", min_size=12, max_size=16).map(
    lambda s: f"SECRET{s}"
)


@settings(max_examples=150)
@given(secret, st.text(max_size=30), st.integers(-1, 3))
def test_hidden_data_never_appears_in_results(
    token: str, agent_output: str, exit_code: int
) -> None:
    """Hidden files, expected values and hidden stdin all carry the token; the
    candidate even echoes it. No CheckResult, verdict or score may contain it."""
    spec = EvaluationSpec.model_validate(
        {
            "checks": [
                {"kind": "python_tests", "name": "hidden", "expected_tests": 1},
                {
                    "kind": "io",
                    "name": "io",
                    "argv": ["python", "s.py"],
                    "stdin": token,
                    "expected_stdout": token,
                },
                {
                    "kind": "command",
                    "name": "cmd",
                    "argv": ["sh", "c.sh"],
                    "trusted_environment": True,
                },
                {"kind": "final_answer", "name": "ans", "match": "contains", "expected": token},
                {
                    "kind": "file",
                    "name": "f",
                    "path": "out.txt",
                    "mode": "equals",
                    "expected": token,
                },
            ],
            "hidden_files": {"test_hidden.py": f"# {token}\nassert True\n"},
            "timeout_s": 600,
        }
    )
    leaky = ExecOutput(
        exit_code=exit_code,
        stdout=f"{agent_output}{token}",
        stderr=f'File "/eval/test_hidden.py", line 1\n# {token}',
    )
    results = []
    for c in spec.checks:
        if c.kind in ("final_answer", "file"):
            results.append(
                judge_static(
                    c,
                    final_answer=agent_output,
                    evidence={"out.txt": evidence_for("out.txt", agent_output.encode())},
                )
            )
        else:
            plan(c)
            results.append(judge(c, leaky))
    integrity = IntegrityResult(ok=True)
    verdict = classify(
        AttemptFacts(outcome=Outcome.AGENT_FINISHED, final_answer=agent_output),
        has_spec=True,
        checks=results,
        integrity=integrity,
        metrics=TrajectoryMetrics(),
    )
    blob = "".join(r.model_dump_json() for r in results) + repr(verdict)
    if verdict.status is EvaluationStatus.SUCCEEDED:
        blob += score(
            components(
                verdict, results, TrajectoryMetrics(), AttemptFacts(outcome=Outcome.AGENT_FINISHED)
            ),
            integrity=integrity,
        ).model_dump_json()
    assert token not in blob
