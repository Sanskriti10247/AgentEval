"""Which providers this server runs (GET /providers, the estimate, launch checks).

A provider is available when it is enabled (Settings.enabled_providers(), shared with the
worker) and, in a public demo, only if it is the fake one: the demo never spends money.
"""

from agenteval_core.agent.pricing import PRICING_VERSION, price_for
from agenteval_core.config import Settings
from agenteval_core.providers.anthropic_settings import ANTHROPIC_MODELS
from agenteval_core.schemas.provider import ProviderCatalog, ProviderInfo, ProviderModel

FAKE = "fake"
# Every provider an agent version may name (ck_agent_versions_model_provider), with the
# models it accepts, in a stable order.
KNOWN_MODELS: dict[str, tuple[str, ...]] = {
    FAKE: ("fake-model",),
    "anthropic": tuple(ANTHROPIC_MODELS),
}
_ENABLE_SETTING = {
    FAKE: "AGENTEVAL_ENABLE_FAKE_PROVIDER",
    "anthropic": "AGENTEVAL_ENABLE_ANTHROPIC_PROVIDER",
}
DEMO_ONLY_SIMULATED = "The public demo only runs simulated agents"


def is_simulated(provider: str) -> bool:
    return provider == FAKE


def is_public_demo(settings: Settings) -> bool:
    """Same rule as the API's demo module: read_only wins when both are set."""
    return settings.public_demo and not settings.read_only


def availability(settings: Settings, provider: str) -> tuple[bool, str | None]:
    """(available, reason when not). Fixed text: never a setting's value."""
    if provider not in settings.enabled_providers():
        setting = _ENABLE_SETTING.get(provider)
        detail = f" ({setting})" if setting else ""
        return False, f"Not enabled on this server{detail}"
    if is_public_demo(settings) and not is_simulated(provider):
        return False, DEMO_ONLY_SIMULATED
    return True, None


def catalog(settings: Settings) -> ProviderCatalog:
    providers = []
    for name, models in KNOWN_MODELS.items():
        available, reason = availability(settings, name)
        listed = []
        for model in models:
            price = price_for(name, model)
            listed.append(
                ProviderModel(
                    id=model,
                    priced=price is not None,
                    input_usd_per_mtok=price.input if price else None,
                    output_usd_per_mtok=price.output if price else None,
                )
            )
        providers.append(
            ProviderInfo(
                name=name,
                simulated=is_simulated(name),
                available=available,
                reason=reason,
                models=listed,
            )
        )
    return ProviderCatalog(
        providers=providers,
        pricing_version=PRICING_VERSION,
        real_provider_concurrency=settings.real_provider_concurrency,
        default_real_budget_usd=settings.real_eval_max_cost_usd,
    )
