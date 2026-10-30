"""GET /providers: which model providers this server runs, and their list prices."""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class _Read(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProviderModel(_Read):
    id: str
    priced: bool
    # USD per million tokens at list price (agent/pricing.py); None when not priced.
    input_usd_per_mtok: Decimal | None
    output_usd_per_mtok: Decimal | None


class ProviderInfo(_Read):
    name: str
    simulated: bool  # the scripted fake provider: no model is called, costs are simulated
    available: bool  # agent versions of this provider can be launched here
    reason: str | None  # why not, when unavailable
    models: list[ProviderModel]


class ProviderCatalog(_Read):
    providers: list[ProviderInfo]
    pricing_version: str
    real_provider_concurrency: int  # per worker process
    default_real_budget_usd: Decimal  # AGENTEVAL_REAL_EVAL_MAX_COST_USD
