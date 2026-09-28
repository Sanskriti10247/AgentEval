import uuid

from fastapi import APIRouter, Response, status

from agenteval_api.deps import AgentServiceDep
from agenteval_core.schemas.agent import (
    AgentCreate,
    AgentRead,
    AgentVersionCreate,
    AgentVersionRead,
)

router = APIRouter(prefix="/agents", tags=["agents"])


@router.post("", status_code=status.HTTP_201_CREATED, response_model=AgentRead)
async def create_agent(body: AgentCreate, agents: AgentServiceDep) -> AgentRead:
    """409 if the name is taken."""
    return AgentRead.model_validate(await agents.create_agent(body))


@router.get("/{agent_id}", response_model=AgentRead)
async def get_agent(agent_id: uuid.UUID, agents: AgentServiceDep) -> AgentRead:
    return AgentRead.model_validate(await agents.get_agent(agent_id))


@router.post(
    "/{agent_id}/versions",
    status_code=status.HTTP_201_CREATED,
    response_model=AgentVersionRead,
    responses={200: {"description": "An identical config already exists; it is returned"}},
)
async def create_version(
    agent_id: uuid.UUID, body: AgentVersionCreate, agents: AgentServiceDep, response: Response
) -> AgentVersionRead:
    """Versions are immutable. 201 for a new version, 200 when an equivalent
    config (same normalised hash) already exists, 422 if it can't run."""
    result = await agents.create_version(agent_id, body)
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return AgentVersionRead.model_validate(result.version)


@router.get("/{agent_id}/versions", response_model=list[AgentVersionRead])
async def list_versions(agent_id: uuid.UUID, agents: AgentServiceDep) -> list[AgentVersionRead]:
    return [AgentVersionRead.model_validate(v) for v in await agents.list_versions(agent_id)]


@router.get("/{agent_id}/versions/{number}", response_model=AgentVersionRead)
async def get_version(
    agent_id: uuid.UUID, number: int, agents: AgentServiceDep
) -> AgentVersionRead:
    return AgentVersionRead.model_validate(await agents.get_version(agent_id, number))
