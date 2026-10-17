import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from agenteval_core.reliability.policy import (
    MAX_ATTEMPTS_CAP,
    REPLAY_MAX_ADDITIONAL,
    DeadLetterReason,
)
from agenteval_core.reliability.taxonomy import CancelReason, FailureReason
from agenteval_core.states import AttemptStatus, Outcome, RunStatus


class RunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: uuid.UUID
    agent_version_id: uuid.UUID | None = None
    # Execution budget (F6), pinned on the run: attempts before it is dead-lettered.
    # Omitted: the AGENTEVAL_RUN_MAX_ATTEMPTS setting.
    max_attempts: int | None = Field(default=None, ge=1, le=MAX_ATTEMPTS_CAP)


class ReplayCreate(BaseModel):
    """R14 / F10: how many more attempts a FAILED run gets (total cap 10)."""

    model_config = ConfigDict(extra="forbid")

    additional_attempts: int = Field(default=1, ge=1, le=REPLAY_MAX_ADDITIONAL)


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
    # Budget and failure (F1, F6, F10): attempts allowed so far (grows on replay), how
    # often the run was replayed, why it was dead-lettered (a FAILED run) and the
    # run-level reason when it failed without an attempt; next_attempt_at while RETRYING.
    max_attempts: int
    replay_count: int
    dead_letter_reason: DeadLetterReason | None
    failure_reason: FailureReason | None
    next_attempt_at: datetime | None
    created_at: datetime
    claimed_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    # Cancellation (F5, F15): set once a cancel was requested; reason is 'user' or 'batch'.
    cancel_requested_at: datetime | None
    cancel_reason: CancelReason | None


class RunPage(BaseModel):
    """GET /runs: newest first. `next_after` is an opaque cursor, null on the last page."""

    runs: list[RunRead]
    next_after: str | None


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
