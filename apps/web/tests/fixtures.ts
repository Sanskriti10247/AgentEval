import type {
  AgentSummary,
  AgentVersionRead,
  AttemptEvaluationRead,
  Compare,
  EvaluationContext,
  EvaluationEstimate,
  EvaluationRead,
  EvaluationSummary,
  ProviderInfo,
  ProvidersRead,
  ResultRow,
  RunAttemptRead,
  RunRead,
  StatsActivity,
  StatsOverview,
  TaskListItem,
  TrajectoryEventRead,
  WorkerRead,
} from "@/lib/api/types";

export const IDS = {
  agent: "a0000000-0000-4000-8000-000000000001",
  version: "b0000000-0000-4000-8000-000000000001",
  dataset: "d0000000-0000-4000-8000-000000000001",
  task: "c0000000-0000-4000-8000-000000000001",
  eval1: "e0000000-0000-4000-8000-000000000001",
  eval2: "e0000000-0000-4000-8000-000000000002",
  run: "f0000000-0000-4000-8000-000000000001",
};

const byStatus = { QUEUED: 1, CLAIMED: 0, RUNNING: 2, RETRYING: 1, COMPLETED: 6, FAILED: 2, CANCELLED: 1 };

export const overview: StatsOverview = {
  window_h: 24,
  generated_at: "2026-10-17T12:00:00Z",
  evaluations: { total: 3, cancel_requested: 1 },
  runs: { total: 13, by_status: byStatus },
  success: { graded: 8, passed: 6, success_rate: 0.75 },
  run_latency_ms: { count: 7, p50: 400, p95: 7200 },
  usage: { attempts: 7, cost_usd: "0.070000", tokens_in: 7000, tokens_out: 1400 },
  active_workers: 2,
  queue: { runs_queued: 1, runs_retrying: 1, evaluations_pending: 3, total: 5 },
};

export const emptyOverview: StatsOverview = {
  ...overview,
  evaluations: { total: 0, cancel_requested: 0 },
  runs: { total: 0, by_status: { QUEUED: 0, CLAIMED: 0, RUNNING: 0, RETRYING: 0, COMPLETED: 0, FAILED: 0, CANCELLED: 0 } },
  success: { graded: 0, passed: 0, success_rate: null },
  run_latency_ms: { count: 0, p50: null, p95: null },
  usage: { attempts: 0, cost_usd: "0", tokens_in: 0, tokens_out: 0 },
  active_workers: 0,
  queue: { runs_queued: 0, runs_retrying: 0, evaluations_pending: 0, total: 0 },
};

export const agent: AgentSummary = {
  id: IDS.agent,
  name: "coder-agent",
  created_at: "2026-10-01T09:00:00Z",
  latest_version: { id: IDS.version, version: 2, model_provider: "anthropic", model: "claude-test-model", tools: ["read_file", "run_tests"] },
};

export const version: AgentVersionRead = {
  id: IDS.version,
  agent_id: IDS.agent,
  version: 2,
  model_provider: "anthropic",
  model: "claude-test-model",
  system_prompt: "",
  tools: ["read_file", "run_tests"],
  shell_allowlist: ["python"],
  limits: { max_steps: 20 },
  model_settings: {},
  config_hash: "abc",
  created_at: "2026-10-02T09:00:00Z",
};

export const task: TaskListItem = {
  id: IDS.task,
  name: "fix-auth-test",
  description: null,
  task_type: "agent",
  difficulty: "hard",
  dataset_id: IDS.dataset,
  image: "agenteval-sandbox:py312",
  timeout_s: 120,
  memory_mb: 512,
  cpu_limit: 1,
  created_at: "2026-10-03T09:00:00Z",
  has_evaluation: true,
};

export const dataset = {
  id: IDS.dataset,
  name: "coding-bench",
  version: "1",
  description: null,
  status: "FROZEN" as const,
  frozen_at: "2026-10-03T10:00:00Z",
  created_at: "2026-10-03T09:00:00Z",
  task_count: 1,
};

export const worker: WorkerRead = {
  id: "worker-alpha",
  hostname: "host-1",
  version: "0.1.0",
  concurrency: 4,
  started_at: "2026-10-17T10:00:00Z",
  last_heartbeat_at: new Date(Date.now() - 12_000).toISOString(),
  stopped_at: null,
  stop_reason: null,
  alive: true,
};

export const stoppedWorker: WorkerRead = {
  ...worker,
  id: "worker-beta",
  stopped_at: "2026-10-17T11:00:00Z",
  stop_reason: "shutdown",
  alive: false,
};

export const run: RunRead = {
  id: IDS.run,
  task_id: IDS.task,
  agent_version_id: IDS.version,
  evaluation_id: IDS.eval1,
  repetition: 1,
  status: "FAILED",
  outcome: "MODEL_ERROR",
  idempotency_key: null,
  attempt_count: 2,
  worker_id: "worker-alpha",
  error_message: "model overloaded",
  max_attempts: 2,
  replay_count: 0,
  dead_letter_reason: "retries_exhausted",
  failure_reason: "model_overloaded",
  next_attempt_at: null,
  created_at: "2026-10-17T10:00:00Z",
  claimed_at: "2026-10-17T10:00:01Z",
  started_at: "2026-10-17T10:00:02Z",
  finished_at: "2026-10-17T10:00:09Z",
  cancel_requested_at: null,
  cancel_reason: null,
};

export const runningRun: RunRead = {
  ...run,
  id: "f0000000-0000-4000-8000-000000000002",
  status: "RUNNING",
  outcome: null,
  error_message: null,
  dead_letter_reason: null,
  failure_reason: null,
  finished_at: null,
};

export const attempt: RunAttemptRead = {
  id: "11111111-0000-4000-8000-000000000001",
  run_id: IDS.run,
  attempt_no: 2,
  worker_id: "worker-alpha",
  status: "FINISHED",
  outcome: "MODEL_ERROR",
  exit_code: null,
  oom_killed: false,
  stdout: "",
  stderr: "",
  stdout_truncated: false,
  stderr_truncated: false,
  duration_ms: 3400,
  error_message: "model overloaded",
  steps: 3,
  tokens_in: 1200,
  tokens_out: 240,
  cost_usd: "0.002400",
  final_answer: null,
  sandbox_image_id: "sha256:deadbeef",
  started_at: "2026-10-17T10:00:05Z",
  finished_at: "2026-10-17T10:00:09Z",
};

export const events: TrajectoryEventRead[] = [
  {
    seq: 0, step: 1, event_type: "model_call", tool: null, status: "ok", error_class: null, input: null, output: null,
    tokens_in: 400, tokens_out: 80, cache_read_tokens: 0, cache_write_tokens: 0, cost_usd: "0.000800", duration_ms: 842,
    ts: "2026-10-17T10:00:06Z",
  },
  {
    seq: 1, step: 1, event_type: "tool_call", tool: "run_tests", status: "error", error_class: "ToolTimeout",
    input: { cmd: "pytest" }, output: { text: "timed out" },
    tokens_in: null, tokens_out: null, cache_read_tokens: null, cache_write_tokens: null, cost_usd: null, duration_ms: 30000,
    ts: "2026-10-17T10:00:07Z",
  },
];

export const attemptEvaluation: AttemptEvaluationRead = {
  attempt_no: 2,
  created_at: "2026-10-17T10:00:10Z",
  finished_at: "2026-10-17T10:00:11Z",
  status: "SUCCEEDED",
  task_success: false,
  failure_category: "tests_failed",
  tags: ["ran_tests"],
  checks: [{ position: 1, passed: false, score: 0, duration_ms: 120 }],
  score: { overall: "0.12345", task_success: "0", test_score: "0.50000", reliability: "1.00000", efficiency: "0.80000", integrity_gate_applied: false },
  metrics: {
    model_calls: 3, model_errors: 0, tool_calls: 2, tool_status_counts: { ok: 1, error: 1 }, agent_tool_errors: 1,
    tool_error_rate: 0.5, max_identical_streak: 1, loop_detected: false, ran_tests: true, last_tests_passed: false, limits_reached: [],
  },
};

const progress = {
  runs_total: 4,
  runs_by_status: { QUEUED: 0, CLAIMED: 0, RUNNING: 1, RETRYING: 1, COMPLETED: 1, FAILED: 0, CANCELLED: 1 },
  evaluations_by_status: { PENDING: 0, RUNNING: 0, SUCCEEDED: 1, ERROR: 0, SKIPPED: 0 },
  awaiting_evaluation: 0,
  complete: false,
};

export const evaluation: EvaluationRead = {
  id: IDS.eval1,
  name: "calc-demo",
  dataset_id: IDS.dataset,
  agent_version_id: IDS.version,
  repetitions: 2,
  scoring_config: { version: "v1", weights: { task_success: "0.60", test_score: "0.20", reliability: "0.10", efficiency: "0.10" } },
  scoring_config_hash: "6a5118ea0000",
  created_at: "2026-10-17T09:00:00Z",
  cancel_requested_at: null,
  run_max_attempts: 3,
  eval_max_attempts: 3,
  progress,
};

export const summary: EvaluationSummary = {
  evaluation_id: IDS.eval1,
  complete: false,
  attempts: 4,
  graded: 4,
  passed: 3,
  success_rate: 0.75,
  ci95: [0.3, 0.95],
  platform_failures: 1,
  platform_failure_rate: 0.2,
  evaluation_errors: 0,
  ungraded: 0,
  in_progress: 2,
  mean_score: { overall: "0.84970", task_success: "0.75000", test_score: "0.80000", reliability: "1.00000", efficiency: "0.84970" },
  latency_ms: { p50: 1200, p95: 7200 },
  cost_usd: { total: "0.160000", mean_per_attempt: "0.040000" },
  tokens: { tokens_in: 1200, tokens_out: 240 },
  failure_categories: { passed: 3, tests_failed: 1 },
  per_task: [
    { task_id: IDS.task, task_name: "fix-auth-test", difficulty: "hard", attempts: 4, graded: 4, passed: 3, success_rate: 0.75, mean_overall: "0.84970" },
  ],
};

export const result: ResultRow = {
  run_id: IDS.run,
  task_id: IDS.task,
  repetition: 1,
  run_status: "COMPLETED",
  run_outcome: "AGENT_FINISHED",
  attempt_no: 1,
  duration_ms: 2500,
  steps: 3,
  tokens_in: 300,
  tokens_out: 60,
  cost_usd: "0.000600",
  evaluation: attemptEvaluation,
};

export const evaluation2: EvaluationRead = { ...evaluation, id: IDS.eval2, name: "calc-demo-b" };

export const compare: Compare = {
  dataset_id: IDS.dataset,
  scoring_config_hash: "6a5118ea0000",
  evaluations: [
    { evaluation_id: IDS.eval1, agent_version_id: IDS.version, summary },
    {
      evaluation_id: IDS.eval2,
      agent_version_id: IDS.version,
      summary: {
        ...summary,
        evaluation_id: IDS.eval2,
        success_rate: 0.5,
        passed: 2,
        failure_categories: { passed: 2, wrong_answer: 2 },
      },
    },
  ],
  per_task: [
    { task_id: IDS.task, success_rate: { [IDS.eval1]: 0.75, [IDS.eval2]: 0.5 }, delta_vs_first: { [IDS.eval1]: 0, [IDS.eval2]: -0.25 } },
  ],
};

// ---- fields added with providers, estimates and activity (all optional on the web side) ----

export const overviewFull: StatsOverview = {
  ...overview,
  run_latency_ms: { count: 8, p50: 400, p95: 7200, p99: 9100, mean: 1850.5 },
  rates: {
    finished: 8, completed: 7, failed: 1, timed_out: 1, retried: 2,
    completion_rate: 0.875, failure_rate: 0.125, timeout_rate: 0.125, retry_rate: 0.25,
  },
  usage: {
    ...overview.usage,
    by_provider: {
      fake: { attempts: 5, tokens_in: 5000, tokens_out: 1000, cost_usd: "0.050000", simulated: true },
      anthropic: { attempts: 2, tokens_in: 2000, tokens_out: 400, cost_usd: "0.020000", simulated: false },
    },
  },
  queue: { ...overview.queue, oldest_queued_s: 95 },
  workers: { alive: 2, stale: 1 },
};

export const activity: StatsActivity = {
  window_h: 24,
  bucket_s: 3600,
  generated_at: "2026-10-17T12:00:00Z",
  buckets: [
    { start: "2026-10-17T10:00:00Z", finished: 3, completed: 2, failed: 1, cancelled: 0, graded: 2, passed: 1, p95_ms: 900 },
    { start: "2026-10-17T11:00:00Z", finished: 5, completed: 5, failed: 0, cancelled: 1, graded: 5, passed: 4, p95_ms: 1200 },
  ],
  evaluations: [
    {
      id: IDS.eval2, name: "calc-demo-b", created_at: "2026-10-17T11:00:00Z", agent_name: "coder-agent", agent_version: 2,
      provider: "anthropic", model: "claude-test-model", simulated: false,
      runs_total: 4, graded: 4, passed: 2, success_rate: 0.5, p95_ms: 7000,
    },
    {
      id: IDS.eval1, name: "calc-demo", created_at: "2026-10-17T09:00:00Z", agent_name: "fake-agent", agent_version: 1,
      provider: "fake", model: "fake-model", simulated: true,
      runs_total: 10, graded: 8, passed: 6, success_rate: 0.75, p95_ms: 800,
    },
  ],
};

export const emptyActivity: StatsActivity = {
  ...activity,
  buckets: activity.buckets.map((b) => ({ ...b, finished: 0, completed: 0, failed: 0, cancelled: 0, graded: 0, passed: 0, p95_ms: null })),
  evaluations: [],
};

const fakeProvider: ProviderInfo = {
  name: "fake", simulated: true, available: true, reason: null,
  models: [{ id: "fake-model", priced: true, input_usd_per_mtok: "1", output_usd_per_mtok: "5" }],
};
const anthropicProvider: ProviderInfo = {
  name: "anthropic", simulated: false, available: true, reason: null,
  models: [{ id: "claude-test-model", priced: true, input_usd_per_mtok: "1", output_usd_per_mtok: "5" }],
};

/** Both providers enabled. */
export const providers: ProvidersRead = {
  providers: [fakeProvider, anthropicProvider],
  pricing_version: "2026-09-28",
  real_provider_concurrency: 1,
  default_real_budget_usd: "2.00",
};

/** A server that only runs the fake model. */
export const providersFakeOnly: ProvidersRead = {
  ...providers,
  providers: [
    fakeProvider,
    { ...anthropicProvider, available: false, reason: "Not enabled on this server (AGENTEVAL_ENABLE_ANTHROPIC_PROVIDER)" },
  ],
};

export const fakeVersion: AgentVersionRead = {
  ...version,
  id: "b0000000-0000-4000-8000-0000000000f1",
  version: 3,
  model_provider: "fake",
  model: "fake-model",
  limits: { max_steps: 30, max_cost_usd: "1.00" },
  config_hash: "fffeee",
};

/** POST /evaluations/estimate as the API would answer it for a body and provider. */
export function estimateFor(body: { agent_version_id: string; repetitions: number }, tasks = 5): EvaluationEstimate {
  const simulated = body.agent_version_id === fakeVersion.id;
  const runs = tasks * body.repetitions;
  return {
    tasks, repetitions: body.repetitions, runs,
    provider: simulated ? "fake" : "anthropic", model: simulated ? "fake-model" : "claude-test-model",
    simulated, available: true,
    max_steps_per_run: simulated ? 30 : 20, run_max_attempts: 1,
    max_model_requests: runs * (simulated ? 30 : 20),
    pricing_known: true, pricing_version: "2026-09-28",
    max_cost_usd_per_run: "1.00", max_cost_usd: `${runs}.00`,
    default_budget_usd: simulated ? null : "2.00",
    sequential: !simulated,
  };
}

export const context: EvaluationContext = {
  agent_id: IDS.agent, agent_name: "coder-agent", agent_version: 2, agent_config_hash: "abc",
  provider: "fake", model: "fake-model", simulated: true,
  dataset_name: "coding-bench", dataset_version: "1", task_count: 1,
  evaluator_version: "grader-1.4", pricing_version: "2026-09-28",
};

export const evaluationWithContext: EvaluationRead = { ...evaluation, context, max_cost_usd: null };

export const summaryFull: EvaluationSummary = {
  ...summary,
  latency_ms: { p50: 1200, p95: 7200, p99: 7900, mean: 2100.5, count: 4 },
  rates: {
    scheduled: 3, finished: 2, completed: 1, failed: 1, timed_out: 0, retried: 1,
    completion_rate: 0.5, failure_rate: 0.5, timeout_rate: 0, retry_rate: 0.5,
  },
  simulated: true,
};

export const compareFull: Compare = {
  ...compare,
  evaluations: [
    { ...compare.evaluations[0]!, context, distinguishable_from_first: null },
    {
      ...compare.evaluations[1]!,
      context: { ...context, provider: "anthropic", model: "claude-test-model", simulated: false, agent_version: 3 },
      distinguishable_from_first: false,
    },
  ],
  differences: [
    { field: "provider", values: { [IDS.eval1]: "fake", [IDS.eval2]: "anthropic" } },
    { field: "model", values: { [IDS.eval1]: "fake-model", [IDS.eval2]: "claude-test-model" } },
  ],
  warnings: [
    "These evaluations mix simulated and real models; their numbers are not comparable.",
    "coder-agent v2 has only 4 graded attempts; treat differences as indicative.",
  ],
};
