"use client";

import { useInfiniteQuery, useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import * as api from "./api/client";
import type {
  AgentVersionRead,
  DemoLimits,
  EvaluationCreate,
  EvaluationFilters,
  EvaluationRead,
  RunCreate,
  RunFilters,
  RunRead,
  TaskFilters,
} from "./api/types";
import { TERMINAL_RUN_STATUSES } from "./api/types";

const LIVE_MS = 10_000;

/**
 * Child queries (results, attempts, events) poll only while their parent is active, so
 * their last poll can predate the final state. Refetch everything under `key` once when
 * the parent settles.
 */
function useRefreshOnSettle(key: readonly unknown[], settled: boolean | undefined) {
  const client = useQueryClient();
  const was = useRef(settled);
  useEffect(() => {
    if (was.current === false && settled === true) void client.invalidateQueries({ queryKey: key });
    was.current = settled;
    // key is a fresh array each render; its contents are what matter.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client, settled, JSON.stringify(key)]);
}

/** GET /health once per page load: whether the API is a read-only demo. */
export const useApiHealth = () =>
  useQuery({
    queryKey: ["health"],
    queryFn: ({ signal }) => api.getHealth(signal),
    staleTime: Infinity,
  });

/** True only once the API has said it is read-only; false while unknown or on error. */
export const useReadOnly = (): boolean => {
  const data = useApiHealth().data;
  return data?.read_only === true && data.public_demo !== true;
};

export interface Capabilities {
  /** The API refuses every write: hide cancel, replay and the launch forms. */
  readOnly: boolean;
  /** Keyless public demo: launches on existing agents and datasets, within limits. */
  publicDemo: boolean;
  demoLimits: DemoLimits | null;
  /** Whether to offer launch, cancel and replay at all. */
  canLaunch: boolean;
}

/**
 * What this deployment lets a visitor do, from GET /health. The API never reports both
 * flags (read_only wins there), and neither does this. If /health fails, writes are
 * offered; the API remains the judge and answers 403 if not.
 */
export function useCapabilities(): Capabilities {
  const health = useApiHealth();
  const data = health.data;
  const readOnly = data?.read_only === true;
  const publicDemo = data?.public_demo === true && !readOnly;
  return {
    readOnly,
    publicDemo,
    demoLimits: publicDemo ? (data?.demo_limits ?? null) : null,
    // Not offered until /health has answered (or failed), so a read-only demo never flashes a form.
    canLaunch: !health.isPending && !readOnly,
  };
}

export const useOverview = (windowH: number) =>
  useQuery({
    queryKey: ["overview", windowH],
    queryFn: ({ signal }) => api.getOverview(windowH, signal),
    refetchInterval: LIVE_MS,
  });

export const useAgents = () =>
  useInfiniteQuery({
    queryKey: ["agents"],
    queryFn: ({ pageParam, signal }) => api.listAgents({ after: pageParam, limit: 50 }, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_after,
  });

export const useAgent = (id: string) =>
  useQuery({ queryKey: ["agent", id], queryFn: ({ signal }) => api.getAgent(id, signal) });

export const useAgentVersions = (id: string) =>
  useQuery({ queryKey: ["agent", id, "versions"], queryFn: ({ signal }) => api.listAgentVersions(id, signal) });

export const useTasks = (filters: Omit<TaskFilters, "after" | "limit">) =>
  useInfiniteQuery({
    queryKey: ["tasks", filters],
    queryFn: ({ pageParam, signal }) => api.listTasks({ ...filters, after: pageParam, limit: 100 }, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_after,
  });

export const useDatasets = () =>
  useQuery({
    queryKey: ["datasets"],
    queryFn: ({ signal }) => api.listDatasets({ limit: 200 }, signal),
    staleTime: 30_000,
  });

/** One task, for its name on a run page. Optional: callers fall back to the id. */
export const useTask = (id: string | undefined) =>
  useQuery({
    queryKey: ["task", id],
    queryFn: ({ signal }) => api.getTask(id as string, signal),
    enabled: !!id,
    staleTime: 60_000,
    retry: false,
  });

/** Frozen datasets: the only ones an evaluation can run on. */
export const useFrozenDatasets = () =>
  useQuery({
    queryKey: ["datasets", "FROZEN"],
    queryFn: ({ signal }) => api.listDatasets({ status: "FROZEN", limit: 200 }, signal),
    staleTime: 30_000,
  });

/** Up to 200 tasks for a picker (the API's page cap), newest first. */
export const useTaskOptions = () =>
  useQuery({
    queryKey: ["tasks", "options"],
    queryFn: ({ signal }) => api.listTasks({ limit: 200 }, signal),
    staleTime: 30_000,
  });

export const useWorkers = () =>
  useInfiniteQuery({
    queryKey: ["workers"],
    queryFn: ({ pageParam, signal }) => api.listWorkers({ after: pageParam, limit: 100 }, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_after,
    refetchInterval: 5_000,
  });

export const useEvaluations = (filters: Omit<EvaluationFilters, "after" | "limit"> = {}, enabled = true) =>
  useInfiniteQuery({
    queryKey: ["evaluations", filters],
    queryFn: ({ pageParam, signal }) => api.listEvaluations({ ...filters, after: pageParam, limit: 50 }, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_after,
    enabled,
  });

export function useEvaluation(id: string) {
  const query = useQuery({
    queryKey: ["evaluation", id],
    queryFn: ({ signal }) => api.getEvaluation(id, signal),
    refetchInterval: (q) => (q.state.data && !q.state.data.progress.complete ? 5_000 : false),
  });
  useRefreshOnSettle(["evaluation", id], query.data?.progress.complete);
  return query;
}

export const useEvaluationSummary = (id: string, live = false) =>
  useQuery({
    queryKey: ["evaluation", id, "summary"],
    queryFn: ({ signal }) => api.getEvaluationSummary(id, signal),
    refetchInterval: live ? 5_000 : false,
  });

/** Summaries for many evaluations at once (list rows). Missing ones simply stay undefined. */
export const useEvaluationSummaries = (ids: string[]) =>
  useQueries({
    queries: ids.map((id) => ({
      queryKey: ["evaluation", id, "summary"],
      queryFn: ({ signal }: { signal: AbortSignal }) => api.getEvaluationSummary(id, signal),
    })),
  });

export const useEvaluationResults = (id: string, live: boolean) =>
  useInfiniteQuery({
    queryKey: ["evaluation", id, "results"],
    queryFn: ({ pageParam, signal }) => api.listEvaluationResults(id, { after: pageParam, limit: 100 }, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_after,
    refetchInterval: live ? 5_000 : false,
  });

export const useCompare = (ids: string[]) =>
  useQuery({
    queryKey: ["compare", ids],
    queryFn: ({ signal }) => api.compareEvaluations(ids, signal),
    enabled: ids.length >= 2,
    retry: false,
  });

export const useRuns = (filters: Omit<RunFilters, "after" | "limit">) =>
  useInfiniteQuery({
    queryKey: ["runs", filters],
    queryFn: ({ pageParam, signal }) => api.listRuns({ ...filters, after: pageParam, limit: 50 }, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_after,
    refetchInterval: LIVE_MS,
  });

export function useRun(id: string) {
  const query = useQuery({
    queryKey: ["run", id],
    queryFn: ({ signal }) => api.getRun(id, signal),
    refetchInterval: (q) => {
      const run = q.state.data;
      return run && !TERMINAL_RUN_STATUSES.includes(run.status) ? 3_000 : false;
    },
  });
  useRefreshOnSettle(["run", id], query.data ? TERMINAL_RUN_STATUSES.includes(query.data.status) : undefined);
  return query;
}

export const useAttempts = (runId: string, live: boolean) =>
  useQuery({
    queryKey: ["run", runId, "attempts"],
    queryFn: ({ signal }) => api.listAttempts(runId, signal),
    refetchInterval: live ? 3_000 : false,
  });

export const useAttemptEvents = (runId: string, attemptNo: number | null, live: boolean) =>
  useInfiniteQuery({
    queryKey: ["run", runId, "attempt", attemptNo, "events"],
    queryFn: ({ pageParam, signal }) => api.listEvents(runId, attemptNo as number, { afterSeq: pageParam, limit: 200 }, signal),
    initialPageParam: -1 as number,
    getNextPageParam: (last) => last.next_after_seq,
    enabled: attemptNo !== null,
    refetchInterval: live ? 3_000 : false,
  });

export const useAttemptEvaluation = (runId: string, attemptNo: number | null) =>
  useQuery({
    queryKey: ["run", runId, "attempt", attemptNo, "evaluation"],
    queryFn: ({ signal }) => api.getAttemptEvaluation(runId, attemptNo as number, signal),
    enabled: attemptNo !== null,
    // Grading starts after the attempt ends, so keep looking for a while until it finishes.
    // Ungraded tasks never get one, hence the cap.
    refetchInterval: (q) => (q.state.data?.finished_at || q.state.dataUpdateCount > 40 ? false : 3_000),
  });

export function useCancelRun(runId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.cancelRun(runId),
    onSuccess: (run: RunRead) => {
      qc.setQueryData(["run", runId], run);
      void qc.invalidateQueries({ queryKey: ["run", runId] });
      void qc.invalidateQueries({ queryKey: ["runs"] });
    },
  });
}

export function useReplayRun(runId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (additional: number) => api.replayRun(runId, additional),
    onSuccess: (run: RunRead) => {
      qc.setQueryData(["run", runId], run);
      void qc.invalidateQueries({ queryKey: ["run", runId] });
      void qc.invalidateQueries({ queryKey: ["runs"] });
    },
  });
}

export function useCancelEvaluation(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.cancelEvaluation(id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["evaluation", id] });
      void qc.invalidateQueries({ queryKey: ["evaluations"] });
    },
  });
}

export function useCreateEvaluation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ body, key }: { body: EvaluationCreate; key: string }) => api.createEvaluation(body, key),
    onSuccess: (e: EvaluationRead) => {
      qc.setQueryData(["evaluation", e.id], e);
      void qc.invalidateQueries({ queryKey: ["evaluations"] });
      void qc.invalidateQueries({ queryKey: ["overview"] });
    },
  });
}

export function useCreateRun() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ body, key }: { body: RunCreate; key: string }) => api.createRun(body, key),
    onSuccess: (run: RunRead) => {
      qc.setQueryData(["run", run.id], run);
      void qc.invalidateQueries({ queryKey: ["runs"] });
      void qc.invalidateQueries({ queryKey: ["overview"] });
    },
  });
}

export interface VersionInfo {
  versionId: string;
  agentId: string;
  agentName: string;
  version: number;
  model: string;
  provider: string;
  createdAt: string;
}

/**
 * Resolve agent_version_id -> agent name and version. The read API does not embed the
 * agent in an evaluation, so this lists agents and then each agent's versions. Fine at
 * dashboard scale (tens of agents); callers fall back to the short id when absent.
 */
export function useVersionIndex(): { index: Map<string, VersionInfo>; isLoading: boolean } {
  const agents = useQuery({
    queryKey: ["agents", "all"],
    queryFn: ({ signal }) => api.listAgents({ limit: 200 }, signal),
    staleTime: 30_000,
  });
  const list = agents.data?.agents ?? [];
  const versions = useQueries({
    queries: list.map((a) => ({
      queryKey: ["agent", a.id, "versions"],
      queryFn: ({ signal }: { signal: AbortSignal }) => api.listAgentVersions(a.id, signal),
      staleTime: 30_000,
    })),
  });
  const index = new Map<string, VersionInfo>();
  list.forEach((a, i) => {
    const vs: AgentVersionRead[] = versions[i]?.data ?? [];
    for (const v of vs) {
      index.set(v.id, {
        versionId: v.id,
        agentId: a.id,
        agentName: a.name,
        version: v.version,
        model: v.model,
        provider: v.model_provider,
        createdAt: v.created_at,
      });
    }
  });
  return { index, isLoading: agents.isLoading || versions.some((v) => v.isLoading) };
}

/** Evaluations of any of the given agent versions (one filtered query per version, newest first overall). */
export function useEvaluationsForVersions(versionIds: string[]) {
  const results = useQueries({
    queries: versionIds.map((id) => ({
      queryKey: ["evaluations", "version", id],
      queryFn: ({ signal }: { signal: AbortSignal }) => api.listEvaluations({ agent_version_id: id, limit: 50 }, signal),
    })),
  });
  const evaluations = results
    .flatMap((r) => r.data?.evaluations ?? [])
    .sort((a, b) => (a.created_at < b.created_at ? 1 : a.created_at > b.created_at ? -1 : 0));
  return {
    evaluations,
    isLoading: results.some((r) => r.isLoading),
    isError: results.some((r) => r.isError),
    error: results.find((r) => r.isError)?.error ?? null,
    refetch: () => Promise.all(results.map((r) => r.refetch())),
  };
}
