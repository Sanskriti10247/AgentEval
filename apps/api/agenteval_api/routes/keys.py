"""API-key administration (ADR-012 A): admin scope only. There is no anonymous way to
create a key; the first one comes from `python -m agenteval_api.keys create`."""

from fastapi import APIRouter, Response, status

from agenteval_api.auth import ADMIN
from agenteval_api.deps import ApiKeyServiceDep
from agenteval_core.schemas.api_key import ApiKeyCreate, ApiKeyCreated, ApiKeyRead

router = APIRouter(prefix="/keys", tags=["keys"])


@router.post(
    "", status_code=status.HTTP_201_CREATED, response_model=ApiKeyCreated, dependencies=[ADMIN]
)
async def create_key(body: ApiKeyCreate, keys: ApiKeyServiceDep) -> ApiKeyCreated:
    """Create a key. The response is the ONLY time the full `key` is shown."""
    created = await keys.create(body.name, body.scopes, body.expires_days)
    return ApiKeyCreated(
        **ApiKeyRead.model_validate(created.key).model_dump(), key=created.plaintext
    )


@router.get("", response_model=list[ApiKeyRead], dependencies=[ADMIN])
async def list_keys(keys: ApiKeyServiceDep) -> list[ApiKeyRead]:
    """Every key, oldest first, without secrets or hashes. Revoked keys stay listed."""
    return [ApiKeyRead.model_validate(k) for k in await keys.list_all()]


@router.delete("/{prefix}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[ADMIN])
async def revoke_key(prefix: str, keys: ApiKeyServiceDep) -> Response:
    """Revoke by prefix. Idempotent (204 again); 404 for an unknown prefix."""
    await keys.revoke(prefix)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
