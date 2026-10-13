"""Evaluation API schemas (Phase 4 step 6).

D11: evaluation results appear ONLY as PublicEvaluation, built by
evaluation.public.public_view(). Everything else here is ids, closed-
vocabulary enums and numbers. No field can hold hidden files, check names,
expected values, evaluator configuration, process output or failure details.
scoring_config is benchmark policy the requester supplied (R3), not hidden
evaluator configuration.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from agenteval_core.evaluation.public import PublicEvaluation
from agenteval_core.evaluation.scoring import ScoringConfig
from agenteval_core.states import Outcome, RunStatus

MAX_BATCH_RUNS = 1000
MAX_COMPARE = 5


class _Read(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvaluationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    dataset_id: uuid.UUID
    agent_version_id: uuid.UUID
    repetitions: int = Field(default=1, ge=1, le=10)
    # Default: scoring v1 (DEFAULT_SCORING_CONFIG). Weights must sum to 1.
    scoring_config: ScoringConfig | None = None


class Progress(_Read):
    """Derived from the runs and their evaluations; never stored."""

    runs_total: int
    runs_by_status: dict[RunStatus, int]
    evaluations_by_status: dict[str, int]  # EvaluationStatus values
    # Finished runs whose evaluation is missing, PENDING or RUNNING.
    awaiting_evaluation: int
    complete: bool


class EvaluationRead(_Read):
    id: uuid.UUID
    name: str
    dataset_id: uuid.UUID
    agent_version_id: uuid.UUID
    repetitions: int
    scoring_config: ScoringConfig
    scoring_config_hash: str
    created_at: datetime
    progress: Progress


class ResultRow(_Read):
    run_id: uuid.UUID
    task_id: uuid.UUID
    repetition: int
    run_status: RunStatus
    run_outcome: Outcome | None
    attempt_no: int | None
    duration_ms: int | None
    steps: int | None
    tokens_in: int | None
    tokens_out: int | None
    cost_usd: Decimal | None
    evaluation: PublicEvaluation | None  # only ever from public_view()


class ResultPage(_Read):
    results: list[ResultRow]
    next_after: str | None  # opaque cursor for the next page


class MeanScore(_Read):
    overall: Decimal
    task_success: Decimal
    test_score: Decimal | None
    reliability: Decimal | None
    efficiency: Decimal


class Latency(_Read):
    """Attempt duration_ms, nearest-rank percentiles over finished attempts."""

    p50: int
    p95: int


class CostTotals(_Read):
    total: Decimal
    mean_per_attempt: Decimal | None


class TokenTotals(_Read):
    tokens_in: int
    tokens_out: int


class TaskSummary(_Read):
    task_id: uuid.UUID
    task_name: str
    difficulty: str | None
    attempts: int
    graded: int
    passed: int
    success_rate: float | None
    mean_overall: Decimal | None


class Summary(_Read):
    """D7 semantics (evaluation.stats.success_stats): success_rate = passed /
    graded. Platform failures, evaluation errors and ungraded attempts
    (tasks without a spec, R7) are excluded and reported separately;
    in_progress attempts are not counted at all yet."""

    evaluation_id: uuid.UUID
    complete: bool
    attempts: int  # finished attempts with a known evaluation result
    graded: int
    passed: int
    success_rate: float | None
    ci95: tuple[float, float] | None
    platform_failures: int
    platform_failure_rate: float | None
    evaluation_errors: int
    ungraded: int
    in_progress: int
    mean_score: MeanScore | None  # over graded attempts, under this batch's config
    latency_ms: Latency | None
    cost_usd: CostTotals
    tokens: TokenTotals
    failure_categories: dict[str, int]  # FailureCategory values only
    per_task: list[TaskSummary]


class CompareEntry(_Read):
    evaluation_id: uuid.UUID
    agent_version_id: uuid.UUID
    summary: Summary


class TaskComparison(_Read):
    task_id: uuid.UUID
    success_rate: dict[uuid.UUID, float | None]
    delta_vs_first: dict[uuid.UUID, float | None]


class Compare(_Read):
    dataset_id: uuid.UUID
    scoring_config_hash: str
    evaluations: list[CompareEntry]
    per_task: list[TaskComparison]


class AttemptEvaluationRead(PublicEvaluation):
    """PublicEvaluation (from public_view) plus where and when - nothing else."""

    attempt_no: int
    created_at: datetime
    finished_at: datetime | None
