// Hand-written mirror of the backend's public response schemas
// (packages/core/agenteval_core/schemas/*.py and docs/api/*.md).
// Conventions: UUIDs and datetimes are strings (ISO 8601); Python Decimals are
// serialised as strings ("0.070000"); floats are numbers.

export type Uuid = string;
export type Timestamp = string;
export type DecimalString = string;

export const RUN_STATUSES = [
  "QUEUED",
  "CLAIMED",
  "RUNNING",
  "RETRYING",
  "COMPLETED",
  "FAILED",
  "CANCELLED",
] as const;
export type RunStatus = (typeof RUN_STATUSES)[number];

export const TERMINAL_RUN_STATUSES: readonly RunStatus[] = ["COMPLETED", "FAILED", "CANCELLED"];

export const EVALUATION_STATUSES = ["PENDING", "RUNNING", "SUCCEEDED", "ERROR", "SKIPPED"] as const;
export type EvaluationStatus = (typeof EVALUATION_STATUSES)[number];

export type AttemptStatus = "RUNNING" | "FINISHED" | "ABANDONED" | "CANCELLED";

export type Outcome =
  | "SUCCESS"
  | "NON_ZERO_EXIT"
  | "TIMEOUT"
  | "AGENT_FINISHED"
  | "MAX_STEPS"
  | "BUDGET_EXCEEDED"
  | "MODEL_REFUSAL"
  | "MODEL_ERROR"
  | "SANDBOX_ERROR"
  | "INTERNAL_ERROR";

export type DeadLetterReason = "retries_exhausted" | "non_retryable";
export type CancelReason = "user" | "batch";
// Open string: the backend FailureReason enum has ~35 members and grows.
export type FailureReason = string;
export type FailureCategory =
  | "passed"
  | "platform_sandbox"
  | "platform_model"
  | "platform_internal"
  | "platform_cancelled"
  | "evaluation_error"
  | "tampering"
  | "max_steps"
  | "budget_exceeded"
  | "timeout"
  | "refusal"
  | "no_answer"
  | "tests_failed"
  | "wrong_answer"
  | "missing_output";

export type Difficulty = "easy" | "medium" | "hard";
export type TaskType = "command" | "agent";
export type DatasetStatus = "DRAFT" | "FROZEN";

// ---- agents ----------------------------------------------------------------

export interface LatestVersion {
  id: Uuid;
  version: number;
  model_provider: string;
  model: string;
  tools: string[];
}

export interface AgentSummary {
  id: Uuid;
  name: string;
  created_at: Timestamp;
  latest_version: LatestVersion | null;
}

export interface AgentPage {
  agents: AgentSummary[];
  next_after: string | null;
}

export interface AgentRead {
  id: Uuid;
  name: string;
  created_at: Timestamp;
}

export interface AgentVersionRead {
  id: Uuid;
  agent_id: Uuid;
  version: number;
  model_provider: string;
  model: string;
  system_prompt: string;
  tools: string[];
  shell_allowlist: string[];
  limits: Record<string, unknown>;
  model_settings: Record<string, unknown>;
  config_hash: string;
  created_at: Timestamp;
}

// ---- tasks and datasets ----------------------------------------------------

export interface TaskListItem {
  id: Uuid;
  name: string;
  description: string | null;
  task_type: string;
  difficulty: string | null;
  dataset_id: Uuid | null;
  image: string;
  timeout_s: number;
  memory_mb: number;
  cpu_limit: number;
  created_at: Timestamp;
  has_evaluation: boolean;
}

/** GET /tasks/{id}: the list fields plus what a run is given (fields not shown here are omitted). */
export interface TaskRead extends TaskListItem {
  instructions: string | null;
}

export interface TaskPage {
  tasks: TaskListItem[];
  next_after: string | null;
}

export interface DatasetSummary {
  id: Uuid;
  name: string;
  version: string;
  description: string | null;
  status: DatasetStatus;
  frozen_at: Timestamp | null;
  created_at: Timestamp;
  task_count: number;
}

export interface DatasetPage {
  datasets: DatasetSummary[];
  next_after: string | null;
}

// ---- workers ---------------------------------------------------------------

export interface WorkerRead {
  id: string;
  hostname: string;
  version: string;
  concurrency: number;
  started_at: Timestamp;
  last_heartbeat_at: Timestamp;
  stopped_at: Timestamp | null;
  stop_reason: string | null;
  alive: boolean;
}

export interface WorkerPage {
  workers: WorkerRead[];
  next_after: string | null;
}

// ---- stats -----------------------------------------------------------------
// Fields marked optional were added after the first release of the API; an older
// deployment omits them and the views fall back to what it does return.

/**
 * Run outcome rates over finished runs (COMPLETED or FAILED; cancelled runs are left out).
 * A rate is null when its denominator is 0. Counts sit next to the rates on purpose.
 */
export interface RunRates {
  finished: number;
  completed: number;
  failed: number;
  timed_out: number;
  retried: number;
  completion_rate: number | null;
  failure_rate: number | null;
  timeout_rate: number | null;
  retry_rate: number | null;
}

/** Summary rates add `scheduled`: every run of the evaluation that was not cancelled. */
export interface SummaryRates extends RunRates {
  scheduled: number;
}

export interface ProviderUsage {
  attempts: number;
  tokens_in: number;
  tokens_out: number;
  cost_usd: DecimalString;
  /** True for the fake provider: its cost is computed from a price table, nothing was spent. */
  simulated: boolean;
}

export interface StatsOverview {
  window_h: number;
  generated_at: Timestamp;
  evaluations: { total: number; cancel_requested: number };
  runs: { total: number; by_status: Record<RunStatus, number> };
  success: { graded: number; passed: number; success_rate: number | null };
  /** Nearest-rank percentiles over finished runs in the window. */
  run_latency_ms: { count: number; p50: number | null; p95: number | null; p99?: number | null; mean?: number | null };
  usage: {
    attempts: number;
    cost_usd: DecimalString;
    tokens_in: number;
    tokens_out: number;
    by_provider?: Record<string, ProviderUsage>;
  };
  active_workers: number;
  queue: {
    runs_queued: number;
    runs_retrying: number;
    evaluations_pending: number;
    total: number;
    /** Age in seconds of the oldest QUEUED run, null when nothing is queued. */
    oldest_queued_s?: number | null;
  };
  /** Runs that finished inside the window. */
  rates?: RunRates;
  /** stale: not stopped, but no heartbeat within the liveness window. */
  workers?: { alive: number; stale: number };
}

/** One time bucket of GET /stats/activity, keyed by run finished_at. */
export interface ActivityBucket {
  start: Timestamp;
  finished: number;
  completed: number;
  failed: number;
  cancelled: number;
  graded: number;
  passed: number;
  p95_ms: number | null;
}

export interface ActivityEvaluation {
  id: Uuid;
  name: string;
  created_at: Timestamp;
  agent_name: string;
  agent_version: number;
  provider: string;
  model: string;
  simulated: boolean;
  runs_total: number;
  graded: number;
  passed: number;
  success_rate: number | null;
  p95_ms: number | null;
}

/** GET /stats/activity: buckets oldest first and zero-filled; evaluations newest first, at most 50. */
export interface StatsActivity {
  window_h: number;
  bucket_s: number;
  generated_at: Timestamp;
  buckets: ActivityBucket[];
  evaluations: ActivityEvaluation[];
}

// ---- providers -------------------------------------------------------------

export interface ProviderModel {
  id: string;
  priced: boolean;
  input_usd_per_mtok?: DecimalString | null;
  output_usd_per_mtok?: DecimalString | null;
}

export interface ProviderInfo {
  name: string;
  simulated: boolean;
  available: boolean;
  /** Why the provider cannot be used here, when it is not available. */
  reason: string | null;
  models: ProviderModel[];
}

/** GET /providers */
export interface ProvidersRead {
  providers: ProviderInfo[];
  pricing_version: string;
  /** Real-provider runs one worker process executes at the same time. */
  real_provider_concurrency?: number;
  default_real_budget_usd?: DecimalString | null;
}

/** POST /evaluations/estimate body. Writes nothing. */
export interface EvaluationEstimateRequest {
  agent_version_id: Uuid;
  dataset_id: Uuid;
  repetitions: number;
}

/** Upper bounds for an evaluation before it is launched. */
export interface EvaluationEstimate {
  tasks: number;
  repetitions: number;
  runs: number;
  provider: string;
  model: string;
  simulated: boolean;
  available: boolean;
  max_steps_per_run: number;
  run_max_attempts: number;
  /** runs x max steps x run attempts. */
  max_model_requests: number;
  pricing_known: boolean;
  pricing_version: string | null;
  max_cost_usd_per_run: DecimalString | null;
  /** runs x per-run cost cap x run attempts; null when pricing is unknown. */
  max_cost_usd: DecimalString | null;
  /** The budget the API applies to a real-provider evaluation launched without one. */
  default_budget_usd: DecimalString | null;
  /** Real providers run one at a time per worker by default. */
  sequential: boolean;
}

// ---- runs ------------------------------------------------------------------

export interface RunRead {
  id: Uuid;
  task_id: Uuid;
  agent_version_id: Uuid | null;
  evaluation_id: Uuid | null;
  repetition: number | null;
  status: RunStatus;
  outcome: Outcome | null;
  idempotency_key: string | null;
  attempt_count: number;
  worker_id: string | null;
  error_message: string | null;
  max_attempts: number;
  replay_count: number;
  dead_letter_reason: DeadLetterReason | null;
  failure_reason: FailureReason | null;
  next_attempt_at: Timestamp | null;
  created_at: Timestamp;
  claimed_at: Timestamp | null;
  started_at: Timestamp | null;
  finished_at: Timestamp | null;
  cancel_requested_at: Timestamp | null;
  cancel_reason: CancelReason | null;
}

export interface RunPage {
  runs: RunRead[];
  next_after: string | null;
}

export interface RunAttemptRead {
  id: Uuid;
  run_id: Uuid;
  attempt_no: number;
  worker_id: string;
  status: AttemptStatus;
  outcome: Outcome | null;
  exit_code: number | null;
  oom_killed: boolean;
  stdout: string;
  stderr: string;
  stdout_truncated: boolean;
  stderr_truncated: boolean;
  duration_ms: number | null;
  error_message: string | null;
  steps: number | null;
  tokens_in: number | null;
  tokens_out: number | null;
  cost_usd: DecimalString | null;
  final_answer: string | null;
  sandbox_image_id: string | null;
  started_at: Timestamp;
  finished_at: Timestamp | null;
}

export interface TrajectoryEventRead {
  seq: number;
  step: number;
  event_type: string;
  tool: string | null;
  status: string | null;
  error_class: string | null;
  input: Record<string, unknown> | null;
  output: Record<string, unknown> | null;
  tokens_in: number | null;
  tokens_out: number | null;
  cache_read_tokens: number | null;
  cache_write_tokens: number | null;
  cost_usd: DecimalString | null;
  duration_ms: number | null;
  ts: Timestamp;
}

export interface TrajectoryPage {
  events: TrajectoryEventRead[];
  next_after_seq: number | null;
}

// ---- evaluations -----------------------------------------------------------

export interface ScoringWeights {
  task_success: DecimalString;
  test_score: DecimalString;
  reliability: DecimalString;
  efficiency: DecimalString;
}

export interface ScoringConfig {
  version: "v1";
  weights: ScoringWeights;
}

export interface Progress {
  runs_total: number;
  runs_by_status: Record<RunStatus, number>;
  evaluations_by_status: Record<string, number>;
  awaiting_evaluation: number;
  complete: boolean;
}

/** What an evaluation ran on, resolved by the API. */
export interface EvaluationContext {
  agent_id: Uuid;
  agent_name: string;
  agent_version: number;
  agent_config_hash: string;
  provider: string;
  model: string;
  simulated: boolean;
  dataset_name: string;
  dataset_version: string;
  task_count: number;
  evaluator_version: string | null;
  pricing_version: string | null;
}

export interface EvaluationRead {
  id: Uuid;
  name: string;
  dataset_id: Uuid;
  agent_version_id: Uuid;
  repetitions: number;
  scoring_config: ScoringConfig;
  scoring_config_hash: string;
  created_at: Timestamp;
  cancel_requested_at: Timestamp | null;
  run_max_attempts: number;
  eval_max_attempts: number;
  progress: Progress;
  /** Spending cap for the whole evaluation; once reached, remaining runs fail without a model call. */
  max_cost_usd?: DecimalString | null;
  context?: EvaluationContext | null;
}

export interface EvaluationPage {
  evaluations: EvaluationRead[];
  next_after: string | null;
}

export interface PublicCheck {
  position: number;
  passed: boolean | null;
  score: number | null;
  duration_ms: number;
}

export interface PublicScore {
  overall: DecimalString;
  task_success: DecimalString;
  test_score: DecimalString | null;
  reliability: DecimalString | null;
  efficiency: DecimalString;
  integrity_gate_applied: boolean;
}

export interface TrajectoryMetrics {
  model_calls: number;
  model_errors: number;
  tool_calls: number;
  tool_status_counts: Record<string, number>;
  agent_tool_errors: number;
  tool_error_rate: number | null;
  max_identical_streak: number;
  loop_detected: boolean;
  ran_tests: boolean;
  last_tests_passed: boolean | null;
  limits_reached: string[];
}

export interface PublicEvaluation {
  status: EvaluationStatus;
  task_success: boolean | null;
  failure_category: FailureCategory | null;
  tags: string[];
  checks: PublicCheck[];
  score: PublicScore | null;
  metrics: TrajectoryMetrics | null;
}

export interface AttemptEvaluationRead extends PublicEvaluation {
  attempt_no: number;
  created_at: Timestamp;
  finished_at: Timestamp | null;
}

export interface ResultRow {
  run_id: Uuid;
  task_id: Uuid;
  repetition: number;
  run_status: RunStatus;
  run_outcome: Outcome | null;
  attempt_no: number | null;
  duration_ms: number | null;
  steps: number | null;
  tokens_in: number | null;
  tokens_out: number | null;
  cost_usd: DecimalString | null;
  evaluation: PublicEvaluation | null;
}

export interface ResultPage {
  results: ResultRow[];
  next_after: string | null;
}

export interface TaskSummary {
  task_id: Uuid;
  task_name: string;
  difficulty: string | null;
  attempts: number;
  graded: number;
  passed: number;
  success_rate: number | null;
  mean_overall: DecimalString | null;
}

export interface EvaluationSummary {
  evaluation_id: Uuid;
  complete: boolean;
  attempts: number;
  graded: number;
  passed: number;
  success_rate: number | null;
  ci95: [number, number] | null;
  platform_failures: number;
  platform_failure_rate: number | null;
  evaluation_errors: number;
  ungraded: number;
  in_progress: number;
  mean_score: {
    overall: DecimalString;
    task_success: DecimalString;
    test_score: DecimalString | null;
    reliability: DecimalString | null;
    efficiency: DecimalString;
  } | null;
  /** Nearest-rank percentiles over attempt durations; count is the sample size. */
  latency_ms: { p50: number; p95: number; p99?: number; mean?: number; count?: number } | null;
  cost_usd: { total: DecimalString; mean_per_attempt: DecimalString | null };
  tokens: { tokens_in: number; tokens_out: number };
  failure_categories: Record<string, number>;
  per_task: TaskSummary[];
  rates?: SummaryRates;
  simulated?: boolean;
}

export interface CompareEntry {
  evaluation_id: Uuid;
  agent_version_id: Uuid;
  summary: EvaluationSummary;
  context?: EvaluationContext | null;
  /** True when the 95% intervals of this entry and the first do not overlap; null for the first. */
  distinguishable_from_first?: boolean | null;
}

export interface CompareDifference {
  field: string;
  values: Record<Uuid, string | number | null>;
}

export interface TaskComparison {
  task_id: Uuid;
  success_rate: Record<Uuid, number | null>;
  delta_vs_first: Record<Uuid, number | null>;
}

export interface Compare {
  dataset_id: Uuid;
  scoring_config_hash: string;
  evaluations: CompareEntry[];
  per_task: TaskComparison[];
  differences?: CompareDifference[];
  warnings?: string[];
}

// ---- filters ---------------------------------------------------------------

export interface PageParams {
  after?: string | null;
  limit?: number;
}
export interface TaskFilters extends PageParams {
  dataset_id?: Uuid;
  task_type?: TaskType;
  difficulty?: Difficulty;
}
export interface EvaluationFilters extends PageParams {
  agent_version_id?: Uuid;
  dataset_id?: Uuid;
}
export interface RunFilters extends PageParams {
  status?: RunStatus;
  dead_letter?: boolean;
  evaluation_id?: Uuid;
  task_id?: Uuid;
}

// ---- writes ----------------------------------------------------------------

/** POST /evaluations (EvaluationCreate). Omitted budgets and scoring use the API's defaults. */
export interface EvaluationCreate {
  name: string;
  dataset_id: Uuid;
  agent_version_id: Uuid;
  repetitions: number;
  run_max_attempts?: number;
  eval_max_attempts?: number;
  /** Budget for the whole evaluation in USD, as a decimal string. */
  max_cost_usd?: DecimalString;
}

/** POST /runs (RunCreate). Agent tasks need agent_version_id; command tasks must not send one. */
export interface RunCreate {
  task_id: Uuid;
  agent_version_id?: Uuid;
  max_attempts?: number;
}

/** The API's hard cap on repetitions per evaluation (EvaluationCreate.repetitions le=10). */
export const MAX_REPETITIONS = 10;

// ---- health ----------------------------------------------------------------

export interface DemoLimits {
  max_repetitions: number;
  max_active_runs: number;
  /** Runs the worker executes at the same time; the rest wait in the queue. */
  concurrent_runs?: number;
}

/**
 * GET /health (outside /api/v1, no key needed).
 * read_only: the API refuses every write.
 * public_demo: no key needed, and visitors may launch evaluations and runs on existing
 * agents and datasets (within demo_limits), cancel and replay; creating agents, tasks
 * and datasets is refused.
 */
export interface ApiHealth {
  status: string;
  read_only?: boolean;
  public_demo?: boolean;
  demo_limits?: DemoLimits | null;
}
