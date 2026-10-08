"""The ONLY projection of an evaluation allowed to leave the evaluator (D11).

API responses (Step 6) and anything model-visible must be built from
PublicEvaluation, never from CheckResult / Verdict / IntegrityResult, which
carry evaluator-only configuration (check names, paths, expected test counts,
match modes, argv, stdin, summaries).

Structurally free of evaluator configuration: every field is a number, a
boolean, an enum from a closed vocabulary, or a trajectory metric. Checks are
identified only by their position, and carry no name, kind, expected/actual
text, detail or counts.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from agenteval_core.evaluation.classify import Verdict
from agenteval_core.evaluation.metrics import TrajectoryMetrics
from agenteval_core.evaluation.results import CheckResult
from agenteval_core.evaluation.scoring import ScoreResult
from agenteval_core.evaluation.taxonomy import (
    TAG_VOCABULARY,
    EvaluationStatus,
    FailureCategory,
)


class _Public(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PublicCheck(_Public):
    position: int  # 1-based order of the check in the spec
    passed: bool | None  # None: the check could not run
    score: float | None
    duration_ms: int


class PublicScore(_Public):
    overall: Decimal
    task_success: Decimal
    test_score: Decimal | None
    reliability: Decimal | None
    efficiency: Decimal
    integrity_gate_applied: bool


class PublicEvaluation(_Public):
    status: EvaluationStatus
    task_success: bool | None
    failure_category: FailureCategory | None
    tags: list[str]  # members of TAG_VOCABULARY only (checked below)
    checks: list[PublicCheck]
    score: PublicScore | None
    metrics: TrajectoryMetrics | None


def public_view(
    verdict: Verdict,
    checks: Sequence[CheckResult],
    *,
    score: ScoreResult | None = None,
    metrics: TrajectoryMetrics | None = None,
) -> PublicEvaluation:
    unknown = set(verdict.tags) - TAG_VOCABULARY
    if unknown:  # a closed vocabulary: refuse rather than leak free text
        raise ValueError(f"tags outside the public vocabulary: {sorted(unknown)}")
    public_score = None
    if score is not None:
        c = score.components
        public_score = PublicScore(
            overall=score.overall,
            task_success=c.task_success,
            test_score=c.test_score,
            reliability=c.reliability,
            efficiency=c.efficiency,
            integrity_gate_applied=score.integrity_gate_applied,
        )
    return PublicEvaluation(
        status=verdict.status,
        task_success=verdict.task_success,
        failure_category=verdict.category,
        tags=sorted(verdict.tags),
        checks=[
            PublicCheck(position=i, passed=c.passed, score=c.score, duration_ms=c.duration_ms)
            for i, c in enumerate(checks, start=1)
        ],
        score=public_score,
        metrics=metrics,
    )
