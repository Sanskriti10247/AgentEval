"""Scoring model.

    overall = Σ weight_i · component_i        (components in [0, 1])

scoring v1 (DEFAULT_SCORING_CONFIG) uses 0.60 task_success + 0.20 test_score
+ 0.10 reliability + 0.10 efficiency. These are BENCHMARK-POLICY weights - a
choice about what this benchmark rewards, not universal truths - so they are
configurable per evaluation, hashed, and stored with every score (D6).

Fixed rules, whatever the weights:
  - every component is stored separately; overall is recomputable
  - efficiency is 0 unless the task succeeded (failing fast earns nothing)
  - tampering forces overall to 0 (integrity gate)
  - only graded (SUCCEEDED) evaluations are scored
Arithmetic is Decimal, quantized to 5 places (NUMERIC(6,5)): reproducible.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agenteval_core.evaluation.classify import Verdict
from agenteval_core.evaluation.metrics import TrajectoryMetrics
from agenteval_core.evaluation.results import AttemptFacts, CheckResult, IntegrityResult
from agenteval_core.evaluation.taxonomy import EvaluationStatus
from agenteval_core.hashing import config_hash_of

PLACES = Decimal("0.00001")
ZERO, ONE = Decimal(0), Decimal(1)


def q(value: Decimal) -> Decimal:
    return min(max(value, ZERO), ONE).quantize(PLACES, rounding=ROUND_HALF_UP)


class ScoringWeights(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_success: Decimal = Field(ge=0, le=1)
    test_score: Decimal = Field(ge=0, le=1)
    reliability: Decimal = Field(ge=0, le=1)
    efficiency: Decimal = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _sum_to_one(self) -> ScoringWeights:
        total = self.task_success + self.test_score + self.reliability + self.efficiency
        if total != ONE:
            raise ValueError(f"weights must sum to exactly 1 (got {total})")
        return self


class ScoringConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["v1"] = "v1"
    weights: ScoringWeights

    def config_hash(self) -> str:
        return config_hash_of(self.model_dump(mode="json"))


DEFAULT_SCORING_CONFIG = ScoringConfig(
    weights=ScoringWeights(
        task_success=Decimal("0.60"),
        test_score=Decimal("0.20"),
        reliability=Decimal("0.10"),
        efficiency=Decimal("0.10"),
    )
)


class ScoreComponents(BaseModel):
    """Each in [0, 1]; None = not applicable (counts as 0 in overall)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_success: Decimal
    test_score: Decimal | None
    reliability: Decimal | None
    efficiency: Decimal


class ScoreResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    components: ScoreComponents
    overall: Decimal
    integrity_gate_applied: bool
    scoring_config_hash: str


def components(
    verdict: Verdict,
    checks: Sequence[CheckResult],
    metrics: TrajectoryMetrics | None,
    facts: AttemptFacts,
) -> ScoreComponents:
    if verdict.status is not EvaluationStatus.SUCCEEDED or verdict.task_success is None:
        raise ValueError("only graded (SUCCEEDED) evaluations are scored")
    success = ONE if verdict.task_success else ZERO

    scored = [c for c in checks if c.score is not None]
    weight_sum = sum((Decimal(str(c.weight)) for c in scored), ZERO)
    test_score = (
        q(sum((Decimal(str(c.weight)) * Decimal(str(c.score)) for c in scored), ZERO) / weight_sum)
        if scored
        else None
    )

    reliability: Decimal | None = None
    if metrics is not None:
        rate = metrics.tool_error_rate
        reliability = ONE if rate is None else q(ONE - Decimal(str(rate)))

    efficiency = ZERO
    if verdict.task_success:
        usage = []
        if facts.steps is not None and facts.max_steps:
            usage.append(Decimal(facts.steps) / Decimal(facts.max_steps))
        if facts.cost_usd is not None and facts.max_cost_usd:
            usage.append(facts.cost_usd / facts.max_cost_usd)
        efficiency = q(ONE - sum(usage, ZERO) / len(usage)) if usage else ZERO

    return ScoreComponents(
        task_success=success, test_score=test_score, reliability=reliability, efficiency=efficiency
    )


def score(
    comps: ScoreComponents,
    config: ScoringConfig = DEFAULT_SCORING_CONFIG,
    *,
    integrity: IntegrityResult,
) -> ScoreResult:
    w = config.weights
    total = (
        w.task_success * comps.task_success
        + w.test_score * (comps.test_score or ZERO)
        + w.reliability * (comps.reliability or ZERO)
        + w.efficiency * comps.efficiency
    )
    gate = not integrity.ok
    return ScoreResult(
        components=comps,
        overall=ZERO.quantize(PLACES) if gate else q(total),
        integrity_gate_applied=gate,
        scoring_config_hash=config.config_hash(),
    )
