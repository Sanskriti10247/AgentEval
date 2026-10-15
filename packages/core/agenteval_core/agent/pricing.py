"""List-price cost estimates for model usage.

Prices are USD per million tokens, copied from
https://platform.claude.com/docs/en/about-claude/pricing on PRICING_VERSION.
Each cache price is stored explicitly (not derived from a multiplier)
because the read multiplier differs by model (0.1x standard, 0.05x Opus 5.5,
0.025x Fable 5.1).

These are estimates at standard list price: no batch/volume discounts, no
inference_geo multiplier (the runtime never sets it), 5-minute cache writes
only (the runtime uses the default TTL). Raw token counts are always stored,
so cost can be recomputed if a price changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from agenteval_core.providers.base import Usage

PRICING_VERSION = "2026-09-28"
_PER_MTOK = Decimal(1_000_000)
_QUANTUM = Decimal("0.000001")  # matches NUMERIC(12, 6) in the schema


@dataclass(frozen=True)
class ModelPrice:
    input: Decimal
    cache_write: Decimal  # 5-minute TTL
    cache_read: Decimal
    output: Decimal


def _p(inp: str, cache_write: str, cache_read: str, out: str) -> ModelPrice:
    return ModelPrice(Decimal(inp), Decimal(cache_write), Decimal(cache_read), Decimal(out))


PRICES: dict[tuple[str, str], ModelPrice] = {
    ("anthropic", "claude-opus-5"): _p("5", "6.25", "0.50", "25"),
    ("anthropic", "claude-opus-5-5"): _p("4", "5", "0.20", "20"),
    ("anthropic", "claude-sonnet-5"): _p("2", "2.50", "0.20", "10"),
    ("anthropic", "claude-haiku-4-5"): _p("1", "1.25", "0.10", "5"),
    ("anthropic", "claude-fable-5-1"): _p("10", "12.50", "0.25", "50"),
    # Deterministic, non-zero prices so budget/cost tests exercise real arithmetic.
    ("fake", "fake-model"): _p("1", "1.25", "0.10", "5"),
}


def price_for(provider: str, model: str) -> ModelPrice | None:
    return PRICES.get((provider, model))


def is_priced(provider: str, model: str) -> bool:
    """Agent versions may only use priced models, so cost is never unknown."""
    return (provider, model) in PRICES


def cost_usd(price: ModelPrice, usage: Usage) -> Decimal:
    total = (
        usage.input_tokens * price.input
        + usage.cache_write_tokens * price.cache_write
        + usage.cache_read_tokens * price.cache_read
        + usage.output_tokens * price.output
    ) / _PER_MTOK
    return total.quantize(_QUANTUM, rounding=ROUND_HALF_UP)
