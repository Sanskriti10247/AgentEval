"""Agents and immutable agent versions.

A version is validated completely at creation - tools, shell allowlist,
provider, pricing, limits, provider settings - so a bad config never reaches
a worker. The config is normalised (limit defaults filled in, lists sorted)
and hashed: submitting an equivalent config returns the existing version.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from agenteval_core.agent.limits import AgentLimits
from agenteval_core.agent.pricing import is_priced
from agenteval_core.config import Settings
from agenteval_core.hashing import config_hash_of
from agenteval_core.models import Agent, AgentVersion
from agenteval_core.models.base import new_id
from agenteval_core.providers.anthropic_settings import normalise_anthropic_settings
from agenteval_core.providers.fake import FakeScript
from agenteval_core.schemas.agent import AgentCreate, AgentVersionCreate
from agenteval_core.services.errors import Conflict, InvalidAgentVersion, NotFound
from agenteval_core.tools.builtin import Shell
from agenteval_core.tools.registry import unknown_tool_names

__all__ = ["AgentService", "VersionResult", "config_hash_of"]


@dataclass(frozen=True)
class VersionResult:
    version: AgentVersion
    created: bool


class AgentService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._s = session
        self._settings = settings

    async def create_agent(self, data: AgentCreate) -> Agent:
        row = (
            await self._s.execute(
                pg_insert(Agent)
                .values(id=new_id(), name=data.name)
                .on_conflict_do_nothing(index_elements=[Agent.name])
                .returning(Agent)
            )
        ).scalar_one_or_none()
        if row is None:
            raise Conflict(f"an agent named {data.name!r} already exists")
        await self._s.commit()
        return row

    async def get_agent(self, agent_id: uuid.UUID) -> Agent:
        agent = await self._s.get(Agent, agent_id)
        if agent is None:
            raise NotFound(f"agent {agent_id} not found")
        return agent

    async def create_version(self, agent_id: uuid.UUID, data: AgentVersionCreate) -> VersionResult:
        config = self._normalise(data)
        config_hash = config_hash_of(config)

        # Serialise version creation per agent: the row lock makes
        # "next version number" and "existing identical config" race-free.
        agent = (
            await self._s.execute(select(Agent).where(Agent.id == agent_id).with_for_update())
        ).scalar_one_or_none()
        if agent is None:
            raise NotFound(f"agent {agent_id} not found")
        existing = (
            await self._s.execute(
                select(AgentVersion).where(
                    AgentVersion.agent_id == agent_id, AgentVersion.config_hash == config_hash
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            await self._s.commit()  # releases the lock; commit doesn't expire `existing`
            return VersionResult(existing, created=False)

        latest = (
            await self._s.execute(
                select(func.max(AgentVersion.version)).where(AgentVersion.agent_id == agent_id)
            )
        ).scalar_one()
        version = AgentVersion(
            id=new_id(),
            agent_id=agent_id,
            version=(latest or 0) + 1,
            config_hash=config_hash,
            **config,
        )
        self._s.add(version)
        await self._s.commit()
        return VersionResult(version, created=True)

    async def get_version(self, agent_id: uuid.UUID, number: int) -> AgentVersion:
        version = (
            await self._s.execute(
                select(AgentVersion).where(
                    AgentVersion.agent_id == agent_id, AgentVersion.version == number
                )
            )
        ).scalar_one_or_none()
        if version is None:
            raise NotFound(f"agent {agent_id} has no version {number}")
        return version

    async def list_versions(self, agent_id: uuid.UUID) -> list[AgentVersion]:
        await self.get_agent(agent_id)
        rows = await self._s.execute(
            select(AgentVersion)
            .where(AgentVersion.agent_id == agent_id)
            .order_by(AgentVersion.version)
        )
        return list(rows.scalars())

    # -- validation ------------------------------------------------------------------

    def _normalise(self, data: AgentVersionCreate) -> dict[str, Any]:
        unknown = unknown_tool_names(data.tools)
        if unknown:
            raise InvalidAgentVersion(f"unknown tools: {', '.join(unknown)}")
        tools = sorted(set(data.tools))
        allowlist = sorted(set(data.shell_allowlist))
        if Shell.name in tools and not allowlist:
            raise InvalidAgentVersion("the shell tool needs a non-empty shell_allowlist")
        if allowlist and Shell.name not in tools:
            raise InvalidAgentVersion("shell_allowlist is set but the shell tool is not granted")

        if data.model_provider not in self._settings.enabled_providers():
            raise InvalidAgentVersion(f"model provider '{data.model_provider}' is not enabled")
        if not is_priced(data.model_provider, data.model):
            raise InvalidAgentVersion(
                f"no price for {data.model_provider}/{data.model}: cost could not be tracked"
            )

        try:
            limits = AgentLimits.model_validate(data.limits)
        except ValidationError as exc:
            raise InvalidAgentVersion(f"invalid limits: {_errors(exc)}") from exc
        caps = self._settings
        over = [
            f"{name} {value} > cap {cap}"
            for name, value, cap in (
                ("max_steps", limits.max_steps, caps.agent_max_steps_cap),
                ("max_total_tokens", limits.max_total_tokens, caps.agent_max_total_tokens_cap),
                ("max_cost_usd", limits.max_cost_usd, caps.agent_max_cost_usd_cap),
            )
            if value > cap
        ]
        if over:
            raise InvalidAgentVersion("limits above the global caps: " + "; ".join(over))

        settings = dict(data.model_settings)
        if data.model_provider == "fake":
            try:
                settings = FakeScript.model_validate(settings).model_dump(mode="json")
            except ValidationError as exc:
                raise InvalidAgentVersion(f"invalid fake script: {_errors(exc)}") from exc
        elif data.model_provider == "anthropic":
            try:
                settings = normalise_anthropic_settings(data.model, settings).model_dump(
                    mode="json"
                )
            except ValidationError as exc:
                raise InvalidAgentVersion(f"invalid Anthropic settings: {_errors(exc)}") from exc
            except ValueError as exc:
                raise InvalidAgentVersion(f"invalid Anthropic settings: {exc}") from exc

        return {
            "model_provider": data.model_provider,
            "model": data.model,
            "system_prompt": data.system_prompt,
            "tools": tools,
            "shell_allowlist": allowlist,
            "limits": limits.model_dump(mode="json"),
            "model_settings": settings,
        }


def _errors(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or 'value'}: {e['msg']}"
        for e in exc.errors(include_url=False)[:5]
    )
