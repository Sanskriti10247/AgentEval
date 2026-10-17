"""API-key persistence (ADR-012 A)."""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from agenteval_core.models import ApiKey

# last_used_at is written at most once per this interval per key (no write per request).
LAST_USED_INTERVAL = timedelta(minutes=1)


class ApiKeyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(
        self,
        *,
        name: str,
        prefix: str,
        secret_sha256: bytes,
        scopes: list[str],
        expires_at: datetime | None,
    ) -> ApiKey:
        key = ApiKey(
            name=name,
            prefix=prefix,
            secret_sha256=secret_sha256,
            scopes=scopes,
            expires_at=expires_at,
        )
        self._s.add(key)
        await self._s.flush()
        await self._s.refresh(key)  # server-side created_at
        return key

    async def by_prefix(self, prefix: str) -> ApiKey | None:
        return (
            await self._s.execute(select(ApiKey).where(ApiKey.prefix == prefix))
        ).scalar_one_or_none()

    async def list_all(self) -> list[ApiKey]:
        rows = await self._s.execute(select(ApiKey).order_by(ApiKey.created_at, ApiKey.id))
        return list(rows.scalars())

    async def revoke(self, key_id: uuid.UUID) -> None:
        """Idempotent: the first revocation time stands."""
        await self._s.execute(
            update(ApiKey)
            .where(ApiKey.id == key_id, ApiKey.revoked_at.is_(None))
            .values(revoked_at=text("now()"))
        )

    async def touch(self, key_id: uuid.UUID) -> bool:
        """Set last_used_at = now() unless it was set within LAST_USED_INTERVAL. The
        condition is in the statement, so concurrent API processes cannot both write."""
        res = await self._s.execute(
            update(ApiKey)
            .where(
                ApiKey.id == key_id,
                (ApiKey.last_used_at.is_(None))
                | (ApiKey.last_used_at < func.now() - LAST_USED_INTERVAL),
            )
            .values(last_used_at=text("now()"))
        )
        return bool(res.rowcount)  # type: ignore[attr-defined]
