import { getApiKey, rejectApiKey } from "../auth";
import type {
  AgentPage,
  ApiHealth,
  AgentRead,
  AgentVersionRead,
  AttemptEvaluationRead,
  Compare,
  DatasetPage,
  EvaluationFilters,
  EvaluationPage,
  EvaluationRead,
  EvaluationSummary,
  PageParams,
  ResultPage,
  RunAttemptRead,
  RunFilters,
  RunPage,
  RunRead,
  StatsOverview,
  TaskFilters,
  TaskPage,
  TrajectoryPage,
  WorkerPage,
} from "./types";

export const DEFAULT_API_URL = "http://localhost:8000";

export function apiBaseUrl(): string {
  const raw = process.env.NEXT_PUBLIC_API_URL || DEFAULT_API_URL;
  return raw.replace(/\/+$/, "");
}

/** An API failure: an RFC 7807 problem from the server (status > 0) or a network failure (status 0). */
export class ApiError extends Error {
  readonly status: number;
  readonly title: string | null;
  readonly detail: string | null;

  constructor(message: string, status: number, title: string | null = null, detail: string | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.title = title;
    this.detail = detail;
  }
}

type Params = Record<string, string | number | boolean | null | undefined>;

export function buildUrl(path: string, params: Params = {}): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    search.set(key, String(value));
  }
  const qs = search.toString();
  return `${apiBaseUrl()}/api/v1${path}${qs ? `?${qs}` : ""}`;
}

// 422 bodies are {"detail": [{type, loc, msg}]}; other errors are {title, status, detail: string}.
function describeProblem(body: unknown, status: number): { message: string; title: string | null; detail: string | null } {
  const fallback = `Request failed with status ${status}`;
  if (typeof body !== "object" || body === null) return { message: fallback, title: null, detail: null };
  const b = body as { title?: unknown; detail?: unknown };
  const title = typeof b.title === "string" ? b.title : null;
  let detail: string | null = null;
  if (typeof b.detail === "string") detail = b.detail;
  else if (Array.isArray(b.detail)) {
    detail = b.detail
      .map((d) => {
        if (typeof d !== "object" || d === null) return String(d);
        const item = d as { loc?: unknown[]; msg?: unknown };
        const where = Array.isArray(item.loc) ? item.loc.join(".") : "";
        return `${where ? `${where}: ` : ""}${String(item.msg ?? "")}`;
      })
      .join("; ");
  }
  const message = [title, detail].filter(Boolean).join(": ") || fallback;
  return { message, title, detail };
}

interface RequestOptions {
  method?: "GET" | "POST";
  params?: Params;
  body?: unknown;
  signal?: AbortSignal;
}

export async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const url = buildUrl(path, opts.params);
  const apiKey = getApiKey();
  let response: Response;
  try {
    response = await fetch(url, {
      method: opts.method ?? "GET",
      headers: {
        Accept: "application/json",
        ...(apiKey ? { Authorization: `Bearer ${apiKey}` } : {}),
        ...(opts.body !== undefined ? { "Content-Type": "application/json" } : {}),
      },
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
      signal: opts.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new ApiError(`Cannot reach the API at ${apiBaseUrl()}. Is it running?`, 0);
  }
  let body: unknown = null;
  const text = await response.text();
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = null;
    }
  }
  if (!response.ok) {
    const problem = describeProblem(body, response.status);
    let { message } = problem;
    if (response.status === 401) {
      // Invalid, revoked or expired (or none was sent): the gate asks for a key again.
      if (apiKey) rejectApiKey();
      message = "The API rejected the API key (it may be invalid, revoked or expired).";
    } else if (response.status === 403) {
      // The server's detail names the missing scope: "This API key lacks the write scope".
      message = problem.detail ?? "This API key lacks the scope this action needs.";
    } else if (response.status === 429) {
      const wait = response.headers.get("Retry-After");
      message = `${message}${wait ? ` Retry in ${wait} s.` : ""}`;
    }
    throw new ApiError(message, response.status, problem.title, problem.detail);
  }
  return body as T;
}

const page = (p: PageParams | undefined) => ({ after: p?.after, limit: p?.limit });

// ---- reads -------------------------------------------------------------------

/** The API's liveness probe, which also says whether it is a read-only deployment. */
export async function getHealth(signal?: AbortSignal): Promise<ApiHealth> {
  let response: Response;
  try {
    response = await fetch(`${apiBaseUrl()}/health`, { headers: { Accept: "application/json" }, signal });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new ApiError(`Cannot reach the API at ${apiBaseUrl()}. Is it running?`, 0);
  }
  if (!response.ok) throw new ApiError(`Request failed with status ${response.status}`, response.status);
  return (await response.json()) as ApiHealth;
}

export const getOverview = (windowH = 24, signal?: AbortSignal) =>
  request<StatsOverview>("/stats/overview", { params: { window_h: windowH }, signal });

export const listAgents = (p?: PageParams, signal?: AbortSignal) =>
  request<AgentPage>("/agents", { params: page(p), signal });
export const getAgent = (id: string, signal?: AbortSignal) => request<AgentRead>(`/agents/${id}`, { signal });
export const listAgentVersions = (id: string, signal?: AbortSignal) =>
  request<AgentVersionRead[]>(`/agents/${id}/versions`, { signal });

export const listTasks = (f: TaskFilters = {}, signal?: AbortSignal) =>
  request<TaskPage>("/tasks", {
    params: { dataset_id: f.dataset_id, task_type: f.task_type, difficulty: f.difficulty, ...page(f) },
    signal,
  });

export const listDatasets = (p?: PageParams & { status?: "DRAFT" | "FROZEN" }, signal?: AbortSignal) =>
  request<DatasetPage>("/datasets", { params: { status: p?.status, ...page(p) }, signal });

export const listWorkers = (p?: PageParams, signal?: AbortSignal) =>
  request<WorkerPage>("/workers", { params: page(p), signal });

export const listEvaluations = (f: EvaluationFilters = {}, signal?: AbortSignal) =>
  request<EvaluationPage>("/evaluations", {
    params: { agent_version_id: f.agent_version_id, dataset_id: f.dataset_id, ...page(f) },
    signal,
  });
export const getEvaluation = (id: string, signal?: AbortSignal) =>
  request<EvaluationRead>(`/evaluations/${id}`, { signal });
export const getEvaluationSummary = (id: string, signal?: AbortSignal) =>
  request<EvaluationSummary>(`/evaluations/${id}/summary`, { signal });
export const listEvaluationResults = (id: string, p?: PageParams, signal?: AbortSignal) =>
  request<ResultPage>(`/evaluations/${id}/results`, { params: page(p), signal });
export const compareEvaluations = (ids: string[], signal?: AbortSignal) =>
  request<Compare>("/evaluations/compare", { params: { ids: ids.join(",") }, signal });

export const listRuns = (f: RunFilters = {}, signal?: AbortSignal) =>
  request<RunPage>("/runs", {
    params: {
      status: f.status,
      dead_letter: f.dead_letter,
      evaluation_id: f.evaluation_id,
      task_id: f.task_id,
      ...page(f),
    },
    signal,
  });
export const getRun = (id: string, signal?: AbortSignal) => request<RunRead>(`/runs/${id}`, { signal });
export const listAttempts = (id: string, signal?: AbortSignal) =>
  request<RunAttemptRead[]>(`/runs/${id}/attempts`, { signal });
export const listEvents = (
  runId: string,
  attemptNo: number,
  opts: { afterSeq?: number | null; limit?: number } = {},
  signal?: AbortSignal,
) =>
  request<TrajectoryPage>(`/runs/${runId}/attempts/${attemptNo}/events`, {
    params: { after_seq: opts.afterSeq, limit: opts.limit },
    signal,
  });

/** The attempt's public evaluation, or null when none is recorded (the API answers 404). */
export async function getAttemptEvaluation(
  runId: string,
  attemptNo: number,
  signal?: AbortSignal,
): Promise<AttemptEvaluationRead | null> {
  try {
    return await request<AttemptEvaluationRead>(`/runs/${runId}/attempts/${attemptNo}/evaluation`, { signal });
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return null;
    throw err;
  }
}

// ---- writes ------------------------------------------------------------------

export const cancelRun = (id: string) => request<RunRead>(`/runs/${id}/cancel`, { method: "POST" });
export const replayRun = (id: string, additionalAttempts = 1) =>
  request<RunRead>(`/runs/${id}/replay`, { method: "POST", body: { additional_attempts: additionalAttempts } });
export const cancelEvaluation = (id: string) =>
  request<EvaluationRead>(`/evaluations/${id}/cancel`, { method: "POST" });
