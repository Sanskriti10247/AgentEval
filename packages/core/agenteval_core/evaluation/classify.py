"""Failure classification (D10): one primary category per attempt, the
other matching rules and diagnostics as tags.

Precedence, first match wins:
  1. platform_*         the attempt FAILED (never fairly tested) -> SKIPPED
  2. evaluation_error   a check or the integrity check could not run -> ERROR
  3. tampering          a protected path changed (score forced to 0)
  4. passed             every required check passed
  5. max_steps / budget_exceeded / timeout   the attempt hit a limit
  6. refusal / no_answer                     agent behaviour
  7. tests_failed / wrong_answer / missing_output   the first failing required check
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from agenteval_core.evaluation.metrics import TrajectoryMetrics
from agenteval_core.evaluation.results import AttemptFacts, CheckResult, IntegrityResult
from agenteval_core.evaluation.taxonomy import EvaluationStatus, FailureCategory
from agenteval_core.states import Outcome

# Agent-task outcomes only (D4: command tasks are not evaluated).
_PLATFORM = {
    Outcome.SANDBOX_ERROR: FailureCategory.PLATFORM_SANDBOX,
    Outcome.MODEL_ERROR: FailureCategory.PLATFORM_MODEL,
    Outcome.INTERNAL_ERROR: FailureCategory.PLATFORM_INTERNAL,
}
_LIMITS = {
    Outcome.MAX_STEPS: FailureCategory.MAX_STEPS,
    Outcome.BUDGET_EXCEEDED: FailureCategory.BUDGET_EXCEEDED,
    Outcome.TIMEOUT: FailureCategory.TIMEOUT,
}
_AGENT_OUTCOMES = {Outcome.AGENT_FINISHED, Outcome.MODEL_REFUSAL, *_LIMITS, *_PLATFORM}
HIGH_TOOL_ERROR_RATE = 0.25
HIGH_TOOL_ERROR_MIN_CALLS = 4


@dataclass(frozen=True)
class Verdict:
    status: EvaluationStatus
    task_success: bool | None
    category: FailureCategory | None
    tags: tuple[str, ...]
    detail: str


def platform_category(facts: AttemptFacts) -> FailureCategory | None:
    category = _PLATFORM.get(facts.outcome)
    if category is FailureCategory.PLATFORM_INTERNAL and (facts.error_message or "").startswith(
        "cancelled:"
    ):
        return FailureCategory.PLATFORM_CANCELLED
    return category


def classify(
    facts: AttemptFacts,
    *,
    has_spec: bool,
    checks: Sequence[CheckResult] = (),
    integrity: IntegrityResult | None = None,
    metrics: TrajectoryMetrics | None = None,
) -> Verdict:
    if facts.outcome not in _AGENT_OUTCOMES:
        raise ValueError(f"{facts.outcome} is not an agent-task outcome (D4)")
    diagnostics = _diagnostics(metrics, integrity)

    platform = platform_category(facts)
    if platform is not None:
        return Verdict(
            EvaluationStatus.SKIPPED,
            None,
            platform,
            diagnostics,
            "not graded: the platform failed, so the agent was never fairly tested",
        )

    behaviour = _LIMITS.get(facts.outcome) or (
        FailureCategory.REFUSAL if facts.outcome is Outcome.MODEL_REFUSAL else None
    )
    if not has_spec:
        return Verdict(
            EvaluationStatus.SKIPPED,
            None,
            behaviour,
            diagnostics,
            "not graded: the task has no evaluation spec",
        )

    errored = [c for c in checks if c.error is not None]
    if errored or integrity is None:
        what = errored[0].name if errored else "integrity"
        return Verdict(
            EvaluationStatus.ERROR,
            None,
            FailureCategory.EVALUATION_ERROR,
            diagnostics,
            f"the evaluator could not run {what!r}",
        )

    required = [c for c in checks if c.required]
    if not required:
        raise ValueError("a graded evaluation needs at least one required check result")
    failing = [c for c in required if not c.passed]
    task_success = integrity.ok and not failing

    matches: list[FailureCategory] = []
    if not integrity.ok:
        matches.append(FailureCategory.TAMPERING)
    if task_success:
        matches.append(FailureCategory.PASSED)
    if behaviour is not None:
        matches.append(behaviour)
    if (
        facts.outcome is Outcome.AGENT_FINISHED
        and not facts.final_answer
        and any(c.kind == "final_answer" for c in checks)
    ):
        matches.append(FailureCategory.NO_ANSWER)
    matches += [c.failure or FailureCategory.TESTS_FAILED for c in failing]

    category = matches[0]
    # Secondary matches become tags; the primary category is never repeated as a tag.
    tags = tuple(sorted(({m.value for m in matches[1:]} - {category.value}) | set(diagnostics)))
    return Verdict(
        EvaluationStatus.SUCCEEDED,
        task_success,
        category,
        tags,
        _detail(category, failing, integrity),
    )


def _diagnostics(
    metrics: TrajectoryMetrics | None, integrity: IntegrityResult | None
) -> tuple[str, ...]:
    tags: set[str] = set()
    if metrics is not None:
        if metrics.loop_detected:
            tags.add("loop_detected")
        if (
            metrics.tool_error_rate is not None
            and metrics.tool_calls >= HIGH_TOOL_ERROR_MIN_CALLS
            and metrics.tool_error_rate >= HIGH_TOOL_ERROR_RATE
        ):
            tags.add("high_tool_error_rate")
        if not metrics.ran_tests:
            tags.add("never_ran_tests")
        if metrics.model_errors:
            tags.add("model_retries")
    if integrity is not None and integrity.normalized_paths:
        tags.add("permissions_normalized")
    return tuple(sorted(tags))


def _detail(
    category: FailureCategory, failing: Sequence[CheckResult], integrity: IntegrityResult
) -> str:
    if category is FailureCategory.PASSED:
        return "all required checks passed"
    if category is FailureCategory.TAMPERING:
        return "protected files changed: " + ", ".join(sorted(integrity.tampered))
    if failing:
        first = failing[0]
        return f"{category.value}: check {first.name!r}: {first.detail}"
    return category.value
