from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from agenteval_core.providers.base import Usage
from agenteval_core.reliability.taxonomy import FailureReason
from agenteval_core.states import Outcome


@dataclass(frozen=True)
class AgentResult:
    outcome: Outcome
    reason: str | None
    final_answer: str | None
    steps: int
    usage: Usage
    cost_usd: Decimal
    duration_ms: int
    # Set exactly for a failed outcome (MODEL_ERROR, SANDBOX_ERROR or
    # INTERNAL_ERROR); None for a completed one. Never inferred from `reason`
    # (free text) - the runtime sets it at each _Stop site.
    failure_reason: FailureReason | None = None
