"""Read-API schemas for the dashboard (Phase 6 step 3; ADR-010 C).

D11: these are projections built field by field from public data. Nothing here
can carry an evaluation spec, a lease, a storage key, a failure detail or any
other internal column; `extra="forbid"` plus explicit constructors keep it so.
List pages follow RunPage: a resource-named list plus an opaque `next_after`.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from agenteval_core.evaluation.taxonomy import DatasetStatus
from agenteval_core.states import RunStatus


class _Read(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LatestVersion(_Read):
    id: uuid.UUID
    version: int
    model_provider: str
    model: str
    tools: list[str]


class AgentSummary(_Read):
    id: uuid.UUID
    name: str
    created_at: datetime
    latest_version: LatestVersion | None  # None until a version exists


class AgentPage(_Read):
    agents: list[AgentSummary]
    next_after: str | None


class TaskListItem(_Read):
    """A task without its files, instructions, commands or evaluation spec."""

    id: uuid.UUID
    name: str
    description: str | None
    task_type: str
    difficulty: str | None
    dataset_id: uuid.UUID | None
    image: str
    timeout_s: int
    memory_mb: int
    cpu_limit: float
    created_at: datetime
    has_evaluation: bool


class TaskPage(_Read):
    tasks: list[TaskListItem]
    next_after: str | None


class DatasetSummary(_Read):
    id: uuid.UUID
    name: str
    version: str
    description: str | None
    status: DatasetStatus
    frozen_at: datetime | None
    created_at: datetime
    task_count: int


class DatasetPage(_Read):
    datasets: list[DatasetSummary]
    next_after: str | None


class WorkerRead(_Read):
    id: str
    hostname: str
    version: str
    concurrency: int
    started_at: datetime
    last_heartbeat_at: datetime
    stopped_at: datetime | None
    stop_reason: str | None
    # Not stopped, and a heartbeat within 3 x AGENTEVAL_LEASE_TTL_S.
    alive: bool


class WorkerPage(_Read):
    workers: list[WorkerRead]
    next_after: str | None


class EvaluationCounts(_Read):
    total: int
    cancel_requested: int


class RunCounts(_Read):
    total: int
    by_status: dict[RunStatus, int]


class SuccessStats(_Read):
    graded: int
    passed: int
    success_rate: float | None  # passed / graded; None when nothing was graded


class LatencyStats(_Read):
    count: int
    p50: float | None  # milliseconds; None when count is 0
    p95: float | None


class UsageStats(_Read):
    attempts: int
    cost_usd: Decimal
    tokens_in: int
    tokens_out: int


class QueueDepth(_Read):
    runs_queued: int
    runs_retrying: int
    evaluations_pending: int
    total: int


class StatsOverview(_Read):
    window_h: int
    generated_at: datetime
    evaluations: EvaluationCounts
    runs: RunCounts
    success: SuccessStats
    run_latency_ms: LatencyStats
    usage: UsageStats
    active_workers: int
    queue: QueueDepth
