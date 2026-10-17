"""API keys: create (the plaintext is returned once), list, revoke, verify (ADR-012 A)."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from agenteval_core import auth
from agenteval_core.auth import AuthFailure, Scope
from agenteval_core.models import ApiKey
from agenteval_core.repositories.api_keys import ApiKeyRepository
from agenteval_core.services.errors import InvalidApiKeyRequest, NotFound

# A digest to compare against when the prefix is unknown, so a miss costs the same
# comparison as a hit (the response time does not reveal which prefixes exist).
_DUMMY_DIGEST = auth.hash_secret("0" * 43)


@dataclass(frozen=True)
class CreatedKey:
    key: ApiKey
    plaintext: str


@dataclass(frozen=True)
class VerifiedKey:
    id: uuid.UUID
    name: str
    prefix: str
    scopes: frozenset[Scope]


class ApiKeyService:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session
        self._keys = ApiKeyRepository(session)

    async def create(
        self,
        name: str,
        scopes: list[Scope],
        expires_days: int | None = None,
        *,
        now: datetime | None = None,
    ) -> CreatedKey:
        name = name.strip()
        if not name or len(name) > 100:
            raise InvalidApiKeyRequest("name must be 1-100 characters")
        if not scopes:
            raise InvalidApiKeyRequest("at least one scope is required")
        if expires_days is not None and expires_days < 1:
            raise InvalidApiKeyRequest("expires_days must be at least 1")
        generated = auth.generate_key()
        expires_at = (
            (now or datetime.now(UTC)) + timedelta(days=expires_days) if expires_days else None
        )
        key = await self._keys.add(
            name=name,
            prefix=generated.prefix,
            secret_sha256=generated.secret_sha256,
            scopes=[s.value for s in sorted(set(scopes), key=list(Scope).index)],
            expires_at=expires_at,
        )
        await self._s.commit()
        return CreatedKey(key, generated.plaintext)

    async def list_all(self) -> list[ApiKey]:
        return await self._keys.list_all()

    async def revoke(self, prefix: str) -> ApiKey:
        """Idempotent. 404 for an unknown prefix."""
        key = await self._keys.by_prefix(prefix) if auth.is_valid_prefix(prefix) else None
        if key is None:
            raise NotFound(f"API key {prefix!r} not found")
        await self._keys.revoke(key.id)
        await self._s.commit()
        await self._s.refresh(key)
        return key

    async def verify(self, plaintext: str, *, now: datetime | None = None) -> VerifiedKey:
        """The key's identity, or AuthFailure. The secret is compared first, in
        constant time; revoked and expired are reported only for a key whose secret
        matched, so the reason never leaks the existence of a prefix."""
        parsed = auth.parse_key(plaintext)
        if parsed is None:
            raise AuthFailure("invalid")
        key = await self._keys.by_prefix(parsed.prefix)
        stored = key.secret_sha256 if key is not None else _DUMMY_DIGEST
        matched = auth.secrets_match(auth.hash_secret(parsed.secret), stored)
        if key is None or not matched:
            raise AuthFailure("invalid")
        moment = now or datetime.now(UTC)
        if key.revoked_at is not None:
            raise AuthFailure("revoked")
        if key.expires_at is not None and key.expires_at <= moment:
            raise AuthFailure("expired")
        verified = VerifiedKey(
            key.id, key.name, key.prefix, frozenset(Scope(s) for s in key.scopes)
        )
        stale = key.last_used_at is None or key.last_used_at <= moment - timedelta(minutes=1)
        if stale and await self._keys.touch(key.id):
            await self._s.commit()
        else:
            await self._s.rollback()  # end the read transaction; nothing to write
        return verified
