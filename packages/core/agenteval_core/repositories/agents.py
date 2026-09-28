import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from agenteval_core.models import AgentVersion


class AgentVersionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get(self, version_id: uuid.UUID) -> AgentVersion | None:
        return await self._s.get(AgentVersion, version_id)
