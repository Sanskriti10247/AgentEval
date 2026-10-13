import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from agenteval_core.states import AttemptStatus, Outcome, RunStatus


class RunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: uuid.UUID
    agent_version_id: uuid.UUID | None = None


class RunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    task_id: uuid.UUID
    agent_version_id: uuid.UUID | None
    # Phase 4: set for runs created by an evaluation batch.
    evaluation_id: uuid.UUID | None
    repetition: int | None
    status: RunStatus
    outcome: Outcome | None
    idempotency_key: str | None
    attempt_count: int
    worker_id: str | None
    error_message: str | None
    created_at: datetime
    claimed_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None


class RunAttemptRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    run_id: uuid.UUID
    attempt_no: int
    worker_id: str
    status: AttemptStatus
    outcome: Outcome | None
    exit_code: int | None
    oom_killed: bool
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    duration_ms: int | None
    error_message: str | None
    # Agent attempts only (null for command tasks).
    steps: int | None
    tokens_in: int | None
    tokens_out: int | None
    cost_usd: Decimal | None
    final_answer: str | None
    # The image ID the agent's sandbox actually ran (agent attempts).
    sandbox_image_id: str | None
    started_at: datetime
    finished_at: datetime | None
