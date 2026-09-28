"""Model providers available to this worker."""

from agenteval_core.config import Settings
from agenteval_core.providers.base import ModelProvider
from agenteval_core.providers.fake import FakeProvider


def build_providers(settings: Settings) -> dict[str, ModelProvider]:
    """FakeProvider by default; Anthropic only when explicitly enabled, and
    then a key is required up front (fail at startup, not on the first run).
    Building the client makes no network call."""
    providers: dict[str, ModelProvider] = {}
    if settings.enable_fake_provider:
        providers["fake"] = FakeProvider()
    if settings.enable_anthropic_provider:
        if settings.anthropic_api_key is None:
            raise RuntimeError(
                "AGENTEVAL_ENABLE_ANTHROPIC_PROVIDER is set but ANTHROPIC_API_KEY is not"
            )
        from agenteval_worker.providers.anthropic import AnthropicProvider, make_client

        providers["anthropic"] = AnthropicProvider(
            make_client(settings.anthropic_api_key.get_secret_value())
        )
    return providers
