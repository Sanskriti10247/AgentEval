"""Anthropic agent-version settings, validated by the API without the SDK.

The worker's AnthropicProvider builds requests from these; nothing else
about the request is configurable per version.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict

Effort = Literal["low", "medium", "high", "xhigh", "max"]


@dataclass(frozen=True)
class ModelCaps:
    adaptive_thinking: bool  # send thinking={"type": "adaptive"}
    effort: bool  # accepts output_config.effort


# Every model here is also in the pricing table (agent/pricing.py).
ANTHROPIC_MODELS: dict[str, ModelCaps] = {
    "claude-opus-5": ModelCaps(adaptive_thinking=True, effort=True),
    "claude-opus-5-5": ModelCaps(adaptive_thinking=True, effort=True),
    "claude-sonnet-5": ModelCaps(adaptive_thinking=True, effort=True),
    "claude-fable-5-1": ModelCaps(adaptive_thinking=True, effort=True),
    # Haiku 4.5: no adaptive thinking, and output_config.effort is an error.
    "claude-haiku-4-5": ModelCaps(adaptive_thinking=False, effort=False),
}
DEFAULT_EFFORT: Effort = "high"


class AnthropicSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    effort: Effort | None = None


def normalise_anthropic_settings(model: str, raw: dict[str, object]) -> AnthropicSettings:
    """Validate against the model's capabilities and make defaults explicit,
    so the stored config (and its hash) says exactly what will be sent.
    Raises ValueError."""
    caps = ANTHROPIC_MODELS.get(model)
    if caps is None:
        raise ValueError(f"unsupported Anthropic model {model!r}")
    settings = AnthropicSettings.model_validate(raw)
    if not caps.effort:
        if settings.effort is not None:
            raise ValueError(f"{model} does not support the effort setting")
        return settings
    return settings if settings.effort else AnthropicSettings(effort=DEFAULT_EFFORT)
