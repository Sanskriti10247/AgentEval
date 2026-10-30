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
from agenteval_core.schemas.evaluation import FinishRates
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
    # Milliseconds; None when count is 0. Nearest-rank percentiles (percentile_disc).
    p50: float | None
    p95: float | None
    p99: float | None
    mean: float | None


class ProviderUsage(_Read):
    attempts: int
    tokens_in: int
    tokens_out: int
    cost_usd: Decimal
    simulated: bool  # the fake provider: this cost is simulated, not money spent


class UsageStats(_Read):
    attempts: int
    cost_usd: Decimal  # every attempt, simulated and real together
    tokens_in: int
    tokens_out: int
    # Agent attempts per model provider (command-task attempts have none).
    by_provider: dict[str, ProviderUsage]


class QueueDepth(_Read):
    runs_queued: int
    runs_retrying: int
    evaluations_pending: int
    total: int
    oldest_queued_s: float | None  # age of the oldest QUEUED run; None when none is


class WorkerLiveness(_Read):
    alive: int  # not stopped, heartbeat within 3 x AGENTEVAL_LEASE_TTL_S
    stale: int  # not stopped, heartbeat older than that


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
    rates: FinishRates  # runs that finished inside the window
    workers: WorkerLiveness


class ActivityBucket(_Read):
    start: datetime
    finished: int
    completed: int
    failed: int
    cancelled: int
    graded: int
    passed: int
    p95_ms: float | None  # nearest-rank run latency of the bucket's finished runs


class ActivityEvaluation(_Read):
    id: uuid.UUID
    name: str
    created_at: datetime
    agent_name: str
    agent_version: int
    provider: str
    model: str
    simulated: bool
    runs_total: int
    graded: int
    passed: int
    success_rate: float | None
    p95_ms: float | None  # nearest-rank over the latest attempts' duration_ms


class StatsActivity(_Read):
    window_h: int
    bucket_s: int
    generated_at: datetime
    buckets: list[ActivityBucket]  # oldest first, every bucket, empty ones zero-filled
    evaluations: list[ActivityEvaluation]  # created in the window, newest first, at most 50
