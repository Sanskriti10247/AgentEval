import pytest
from pydantic import ValidationError

from agenteval_core.agent.pricing import is_priced
from agenteval_core.providers.anthropic_settings import (
    ANTHROPIC_MODELS,
    DEFAULT_EFFORT,
    normalise_anthropic_settings,
)


def test_every_supported_model_is_priced() -> None:
    assert all(is_priced("anthropic", m) for m in ANTHROPIC_MODELS)


@pytest.mark.parametrize("model", ["claude-opus-5", "claude-opus-5-5", "claude-sonnet-5"])
def test_effort_default_is_made_explicit(model: str) -> None:
    assert normalise_anthropic_settings(model, {}).effort == DEFAULT_EFFORT == "high"
    assert normalise_anthropic_settings(model, {"effort": "low"}).effort == "low"


def test_haiku_takes_no_effort() -> None:
    assert normalise_anthropic_settings("claude-haiku-4-5", {}).effort is None
    with pytest.raises(ValueError, match="does not support the effort"):
        normalise_anthropic_settings("claude-haiku-4-5", {"effort": "high"})


@pytest.mark.parametrize(
    ("model", "raw"),
    [
        ("claude-opus-5", {"effort": "extreme"}),
        ("claude-opus-5", {"temperature": 0.2}),  # sampling params are not configurable
        ("claude-opus-5", {"fallbacks": "default"}),  # refusal fallbacks stay off
    ],
)
def test_invalid_settings(model: str, raw: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        normalise_anthropic_settings(model, raw)


def test_unknown_or_date_suffixed_models_are_rejected() -> None:
    for model in ("claude-opus-5-20260401", "gpt-9", "claude-3-opus"):
        with pytest.raises(ValueError, match="unsupported"):
            normalise_anthropic_settings(model, {})
