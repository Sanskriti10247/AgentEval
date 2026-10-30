"""Per-agent-version limits and the tracker that enforces them.

Budgets are checked between model calls; the worst case is overshooting by
one call, which max_tokens bounds on the output side.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from agenteval_core.providers.base import Usage


class AgentLimits(BaseModel):
    """Stored as agent_versions.limits (JSON). Defaults are deliberate, not
    tuned: small enough that a runaway agent is cheap."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_steps: int = Field(default=30, ge=1, le=200)
    max_total_tokens: int = Field(default=500_000, ge=1_000, le=10_000_000)
    max_cost_usd: Decimal = Field(default=Decimal("1.00"), gt=0, le=100)
    max_tokens_per_call: int = Field(default=16_000, ge=256, le=128_000)
    tool_timeout_s: float = Field(default=60, ge=1, le=600)
    model_call_timeout_s: float = Field(default=300, ge=5, le=1800)


class Limit(StrEnum):
    MAX_STEPS = "max_steps"
    MAX_TOTAL_TOKENS = "max_total_tokens"
    MAX_COST_USD = "max_cost_usd"


@dataclass
class BudgetTracker:
    limits: AgentLimits
    steps: int = 0
    usage: Usage = field(default_factory=Usage)
    cost_usd: Decimal = Decimal("0")

    def record_call(self, usage: Usage, cost: Decimal) -> None:
        self.steps += 1
        self.usage = self.usage + usage
        self.cost_usd += cost

    @property
    def tokens_used(self) -> int:
        return self.usage.total_tokens

    def exceeded(self) -> Limit | None:
        """Checked before every model call, in this order."""
        if self.steps >= self.limits.max_steps:
            return Limit.MAX_STEPS
        if self.tokens_used >= self.limits.max_total_tokens:
            return Limit.MAX_TOTAL_TOKENS
        if self.cost_usd >= self.limits.max_cost_usd:
            return Limit.MAX_COST_USD
        return None

    def max_tokens_for_next_call(self) -> int:
        remaining = self.limits.max_total_tokens - self.tokens_used
        return max(1, min(self.limits.max_tokens_per_call, remaining))


def evaluation_budget_exhausted(spent_usd: Decimal, max_cost_usd: Decimal | None) -> bool:
    """Whether an evaluation with this cap has no budget left for another run: what its
    attempts have cost so far has reached the cap. No cap (None) never runs out."""
    return max_cost_usd is not None and spent_usd >= max_cost_usd
