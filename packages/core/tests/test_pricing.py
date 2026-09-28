from decimal import Decimal

import pytest

from agenteval_core.agent.pricing import PRICES, cost_usd, is_priced, price_for
from agenteval_core.providers.base import Usage


def test_opus_5_matches_list_price() -> None:
    p = price_for("anthropic", "claude-opus-5")
    assert p is not None
    assert (p.input, p.cache_write, p.cache_read, p.output) == (
        Decimal("5"),
        Decimal("6.25"),
        Decimal("0.50"),
        Decimal("25"),
    )


def test_cost_of_one_million_tokens_of_each_kind() -> None:
    p = price_for("anthropic", "claude-opus-5")
    assert p is not None
    assert cost_usd(p, Usage(input_tokens=1_000_000)) == Decimal("5.000000")
    assert cost_usd(p, Usage(output_tokens=1_000_000)) == Decimal("25.000000")
    assert cost_usd(p, Usage(cache_write_tokens=1_000_000)) == Decimal("6.250000")
    assert cost_usd(p, Usage(cache_read_tokens=1_000_000)) == Decimal("0.500000")


def test_mixed_usage_matches_hand_calculation() -> None:
    # 10k uncached + 40k cache reads + 2k cache writes in, 1.5k out on Opus 5:
    # 10000*5 + 40000*0.5 + 2000*6.25 + 1500*25 = 50000+20000+12500+37500 = 120000 / 1e6
    p = price_for("anthropic", "claude-opus-5")
    assert p is not None
    u = Usage(
        input_tokens=10_000, output_tokens=1_500, cache_read_tokens=40_000, cache_write_tokens=2_000
    )
    assert cost_usd(p, u) == Decimal("0.120000")


def test_rounds_half_up_to_six_places() -> None:
    p = price_for("anthropic", "claude-haiku-4-5")  # $1/MTok input -> $0.000001 per token
    assert p is not None
    assert cost_usd(p, Usage(input_tokens=1)) == Decimal("0.000001")
    # 1 cache-read token at $0.10/MTok = 0.0000001 -> rounds to 0.000000
    assert cost_usd(p, Usage(cache_read_tokens=1)) == Decimal("0.000000")
    # 5 cache-read tokens = 0.0000005 -> half up -> 0.000001
    assert cost_usd(p, Usage(cache_read_tokens=5)) == Decimal("0.000001")


def test_model_specific_cache_read_rates() -> None:
    # Standard 0.1x, but 0.05x on Opus 5.5 and 0.025x on Fable 5.1.
    ratios = {
        m: PRICES[("anthropic", m)]
        for m in ("claude-opus-5", "claude-opus-5-5", "claude-fable-5-1")
    }
    assert ratios["claude-opus-5"].cache_read / ratios["claude-opus-5"].input == Decimal("0.1")
    assert ratios["claude-opus-5-5"].cache_read / ratios["claude-opus-5-5"].input == Decimal("0.05")
    assert ratios["claude-fable-5-1"].cache_read / ratios["claude-fable-5-1"].input == Decimal(
        "0.025"
    )


@pytest.mark.parametrize("key", sorted(PRICES))
def test_every_price_is_positive_and_5m_write_is_1_25x(key: tuple[str, str]) -> None:
    p = PRICES[key]
    assert min(p.input, p.cache_write, p.cache_read, p.output) > 0
    assert p.cache_write == p.input * Decimal("1.25")


def test_unknown_models_are_not_priced() -> None:
    assert price_for("anthropic", "claude-opus-5-20260401") is None  # never guess date suffixes
    assert not is_priced("openai", "gpt-x")
    assert is_priced("fake", "fake-model")
