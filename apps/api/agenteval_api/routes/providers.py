from fastapi import APIRouter

from agenteval_api.auth import READ
from agenteval_api.deps import SettingsDep
from agenteval_core.schemas.provider import ProviderCatalog
from agenteval_core.services.providers import catalog

router = APIRouter(prefix="/providers", tags=["providers"])


@router.get("", response_model=ProviderCatalog, dependencies=[READ])
async def list_providers(settings: SettingsDep) -> ProviderCatalog:
    """Model providers agent versions may name, whether this server runs them (and why
    not), their models with list prices, and the spending defaults for real models."""
    return catalog(settings)
