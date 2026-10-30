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
from agenteval_core.reliability.policy import MAX_ATTEMPTS_CAP
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
    # Budgets pinned on the batch (F6, F9, G1): run_max_attempts is copied onto every
    # batch run, eval_max_attempts onto each of their evaluations. Omitted: the
    # AGENTEVAL_RUN_MAX_ATTEMPTS / AGENTEVAL_EVAL_MAX_ATTEMPTS settings.
    run_max_attempts: int | None = Field(default=None, ge=1, le=MAX_ATTEMPTS_CAP)
    eval_max_attempts: int | None = Field(default=None, ge=1, le=MAX_ATTEMPTS_CAP)
    # Spending cap in USD for all of the batch's attempts together, checked by the worker
    # before each run starts its agent loop. Omitted for a real-model version: the
    # AGENTEVAL_REAL_EVAL_MAX_COST_USD setting. Omitted for a fake-model version: no cap.
    max_cost_usd: Decimal | None = Field(default=None, gt=0, le=1000, decimal_places=6)


class EvaluationEstimateRequest(BaseModel):
    """POST /evaluations/estimate: what an evaluation would cost at most. Writes nothing."""

    model_config = ConfigDict(extra="forbid")

    agent_version_id: uuid.UUID
    dataset_id: uuid.UUID
    repetitions: int = Field(default=1, ge=1, le=10)
    # Omitted: AGENTEVAL_RUN_MAX_ATTEMPTS, as for POST /evaluations.
    run_max_attempts: int | None = Field(default=None, ge=1, le=MAX_ATTEMPTS_CAP)


class EvaluationEstimate(_Read):
    """Upper bounds, not predictions: every run is assumed to use every step and its whole
    per-attempt cost cap, on every attempt it may get."""

    tasks: int
    repetitions: int
    runs: int
    provider: str
    model: str
    simulated: bool  # the fake provider: no real model is called, costs are simulated
    available: bool  # this server would run it (provider enabled, demo rules)
    max_steps_per_run: int
    run_max_attempts: int
    max_model_requests: int  # runs x max_steps x run_max_attempts
    pricing_known: bool
    pricing_version: str
    max_cost_usd_per_run: Decimal  # the version's limits.max_cost_usd (per attempt)
    max_cost_usd: Decimal | None  # runs x max_cost_usd_per_run x run_max_attempts
    default_budget_usd: Decimal | None  # the cap a real-model evaluation gets if none is sent
    sequential: bool  # real-model runs are limited per worker (real_provider_concurrency)


class EvaluationContext(_Read):
    """What was evaluated, resolved from the batch's agent version and dataset."""

    agent_id: uuid.UUID
    agent_name: str
    agent_version: int
    agent_config_hash: str
    provider: str
    model: str
    simulated: bool  # provider "fake": costs come from the fake price row
    dataset_name: str
    dataset_version: str
    task_count: int
    evaluator_version: str
    pricing_version: str


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
    # Set once a user cancelled the batch (F15); the batch's runs follow.
    cancel_requested_at: datetime | None = None
    run_max_attempts: int
    eval_max_attempts: int
    progress: Progress
    max_cost_usd: Decimal | None = None  # the batch's spending cap; None means no cap
    context: EvaluationContext | None = None  # None only if the referenced rows are gone


class EvaluationPage(_Read):
    """GET /evaluations: newest first; each batch carries its progress summary."""

    evaluations: list[EvaluationRead]
    next_after: str | None


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
    """Attempt duration_ms over finished attempts: nearest-rank percentiles (the value
    at rank ceil(p x count), 1-based), the arithmetic mean and how many there were."""

    p50: int
    p95: int
    p99: int
    mean: float
    count: int


class FinishRates(_Read):
    """Run-level rates over finished runs (COMPLETED or FAILED; CANCELLED is not
    finished). Each rate is None when `finished` is 0."""

    finished: int
    completed: int
    failed: int
    timed_out: int  # finished with outcome TIMEOUT
    retried: int  # finished on an attempt number above 1
    completion_rate: float | None
    failure_rate: float | None
    timeout_rate: float | None
    retry_rate: float | None


class RunRates(FinishRates):
    scheduled: int  # runs not CANCELLED


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
    rates: RunRates
    # The agent version uses the fake provider: cost_usd is simulated (fake price row).
    simulated: bool


class CompareEntry(_Read):
    evaluation_id: uuid.UUID
    agent_version_id: uuid.UUID
    summary: Summary
    context: EvaluationContext | None
    # Whether the Wilson 95% intervals of this entry and the first do not overlap.
    # None for the first entry, or when either interval is None (nothing graded).
    distinguishable_from_first: bool | None


class TaskComparison(_Read):
    task_id: uuid.UUID
    success_rate: dict[uuid.UUID, float | None]
    delta_vs_first: dict[uuid.UUID, float | None]


class CompareDifference(_Read):
    field: str  # provider | model | agent_config_hash | agent_name | repetitions
    values: dict[uuid.UUID, str | int | None]


class Compare(_Read):
    dataset_id: uuid.UUID
    scoring_config_hash: str
    evaluations: list[CompareEntry]
    per_task: list[TaskComparison]
    differences: list[CompareDifference]
    warnings: list[str]


class AttemptEvaluationRead(PublicEvaluation):
    """PublicEvaluation (from public_view) plus where and when - nothing else."""

    attempt_no: int
    created_at: datetime
    finished_at: datetime | None
