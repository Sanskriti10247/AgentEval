"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, type ReactNode } from "react";
import { Bar, BarChart, CartesianGrid, ErrorBar, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { AXIS_LABEL, AXIS_STROKE, AXIS_TICK, GRID_STROKE, TooltipBox } from "@/components/charts";
import {
  Button,
  ButtonLink,
  CategoryBadge,
  Empty,
  Gate,
  IdLink,
  isSimulated,
  KeyValue,
  Live,
  LoadMore,
  Meter,
  Mono,
  Notice,
  Num,
  PageHeader,
  ProviderMark,
  RateCell,
  Readouts,
  RunStatusBadge,
  Section,
  Stat,
  StatusMark,
  Table,
  Td,
  Th,
  Timestamp,
} from "@/components/ui";
import { flatten } from "@/components/pages-util";
import { StatusStrip } from "@/components/views/dashboard";
import { describeWriteError } from "@/components/launch/write-error";
import {
  RUN_STATUSES,
  TERMINAL_RUN_STATUSES,
  type Compare,
  type DatasetSummary,
  type EvaluationRead,
  type EvaluationSummary,
  type SummaryRates,
} from "@/lib/api/types";
import {
  formatCost,
  formatDuration,
  formatInt,
  formatPercent,
  formatScore,
  formatTimestamp,
  shortId,
} from "@/lib/format";
import {
  useCancelEvaluation,
  useCapabilities,
  useCompare,
  useDatasets,
  useEvaluation,
  useEvaluationResults,
  useEvaluations,
  useEvaluationSummaries,
  useEvaluationSummary,
  useVersionIndex,
  type VersionInfo,
} from "@/lib/hooks";
import type { Tone } from "@/lib/status";

function AgentCell({ versionId, index }: { versionId: string; index: Map<string, VersionInfo> }) {
  const info = index.get(versionId);
  if (!info) return <IdLink id={versionId} />;
  return (
    <span title={`${info.provider}/${info.model} (version ${versionId})`} className="whitespace-nowrap">
      <Link href={`/agents/${info.agentId}`}>{info.agentName}</Link> <Mono className="text-[12px] text-faint">v{info.version}</Mono>
    </span>
  );
}

export function progressSummary(e: EvaluationRead) {
  const by = e.progress.runs_by_status;
  const done = TERMINAL_RUN_STATUSES.reduce((n, s) => n + (by[s] ?? 0), 0);
  const state = e.cancel_requested_at ? "CANCELLED" : e.progress.complete ? "COMPLETE" : "RUNNING";
  return { done, total: e.progress.runs_total, state };
}

const STATE_TONE: Record<string, Tone> = { COMPLETE: "good", CANCELLED: "muted", RUNNING: "active" };

function EvalStateBadge({ state }: { state: string }) {
  return <StatusMark tone={STATE_TONE[state] ?? "neutral"}>{state}</StatusMark>;
}

export function EvaluationRows({
  evaluations,
  summaries,
  index,
  datasetNames,
  selected,
  onToggle,
}: {
  evaluations: EvaluationRead[];
  summaries: (EvaluationSummary | undefined)[];
  index: Map<string, VersionInfo>;
  datasetNames?: Map<string, string>;
  selected?: string[];
  onToggle?: (id: string) => void;
}) {
  return (
    <Table caption="Evaluations">
      <thead>
        <tr>
          {onToggle ? (
            <Th className="w-6">
              <span className="sr-only">Compare</span>
            </Th>
          ) : null}
          <Th>Evaluation</Th>
          <Th className="hidden sm:table-cell">Agent</Th>
          <Th className="hidden lg:table-cell">Dataset</Th>
          <Th>Progress</Th>
          <Th className="text-right">Success</Th>
          <Th className="hidden text-right md:table-cell">p50</Th>
          <Th className="hidden text-right md:table-cell">Cost / task</Th>
          <Th className="hidden xl:table-cell">Created</Th>
        </tr>
      </thead>
      <tbody>
        {evaluations.map((e, i) => {
          const s = summaries[i];
          const p = progressSummary(e);
          const checked = selected?.includes(e.id) ?? false;
          return (
            <tr key={e.id} className={checked ? "bg-accent-soft" : "hover:bg-sunken/50"}>
              {onToggle ? (
                <Td>
                  <input
                    type="checkbox"
                    aria-label={`Select ${e.name} for comparison`}
                    checked={checked}
                    onChange={() => onToggle(e.id)}
                  />
                </Td>
              ) : null}
              <Td>
                <Link href={`/evaluations/${e.id}`} className="font-medium">
                  {e.name}
                </Link>
                <div className="font-mono text-[11px] text-faint">{shortId(e.id)}</div>
              </Td>
              <Td className="hidden sm:table-cell">
                <AgentCell versionId={e.agent_version_id} index={index} />
              </Td>
              <Td className="hidden whitespace-nowrap lg:table-cell">{datasetNames?.get(e.dataset_id) ?? <IdLink id={e.dataset_id} />}</Td>
              <Td className="min-w-[8.5rem]">
                <div className="flex items-baseline justify-between gap-3">
                  <EvalStateBadge state={p.state} />
                  <span className="font-mono text-[11.5px] text-faint tabular-nums">
                    {p.done}/{p.total}
                  </span>
                </div>
                <div className="mt-1.5">
                  <StatusStrip byStatus={e.progress.runs_by_status} total={e.progress.runs_total} height={3} />
                </div>
              </Td>
              <Td className="text-right">
                <RateCell rate={s?.success_rate} text={formatPercent(s?.success_rate)} />
              </Td>
              <Num className="hidden md:table-cell">{formatDuration(s?.latency_ms?.p50)}</Num>
              <Num className="hidden md:table-cell">{formatCost(s?.cost_usd.mean_per_attempt)}</Num>
              <Td className="hidden xl:table-cell">
                <Timestamp value={formatTimestamp(e.created_at)} />
              </Td>
            </tr>
          );
        })}
      </tbody>
    </Table>
  );
}

export function EvaluationsView() {
  const router = useRouter();
  const query = useEvaluations();
  const caps = useCapabilities();
  const evaluations = flatten(query.data, "evaluations");
  const summaries = useEvaluationSummaries(evaluations.map((e) => e.id));
  const { index } = useVersionIndex();
  const datasets = useDatasets();
  const datasetNames = new Map((datasets.data?.datasets ?? []).map((d) => [d.id, `${d.name} v${d.version}`]));
  const [selected, setSelected] = useState<string[]>([]);
  const toggle = (id: string) =>
    setSelected((cur) => (cur.includes(id) ? cur.filter((x) => x !== id) : cur.length >= 5 ? cur : [...cur, id]));
  const canCompare = selected.length >= 2;

  return (
    <>
      <PageHeader
        eyebrow="Benchmarks"
        title="Evaluations"
        subtitle="Each evaluation runs one agent version over every task of a frozen dataset, a set number of times, and grades every attempt. Tick two to five rows on the same dataset to compare them."
        actions={
          <>
            <Button disabled={!canCompare} onClick={() => router.push(`/evaluations/compare?ids=${selected.join(",")}`)}>
              Compare selected ({selected.length})
            </Button>
            {caps.canLaunch ? (
              <ButtonLink href="/evaluations/new" variant="primary">
                New evaluation
              </ButtonLink>
            ) : null}
          </>
        }
      />
      <Gate query={query}>
        {evaluations.length === 0 ? (
          <Empty
            title="No evaluations yet"
            action={caps.canLaunch ? <ButtonLink href="/evaluations/new" variant="primary">New evaluation</ButtonLink> : undefined}
          >
            {caps.canLaunch
              ? "Pick an agent version and a frozen dataset, and the platform queues one run per task and repetition."
              : "Evaluations are started through the API (POST /api/v1/evaluations); they appear here as soon as they are queued."}
          </Empty>
        ) : (
          <>
            <EvaluationRows
              evaluations={evaluations}
              summaries={summaries.map((s) => s.data)}
              index={index}
              datasetNames={datasetNames}
              selected={selected}
              onToggle={toggle}
            />
            <LoadMore
              hasNext={!!query.hasNextPage}
              loading={query.isFetchingNextPage}
              onClick={() => void query.fetchNextPage()}
              shown={evaluations.length}
            />
          </>
        )}
      </Gate>
    </>
  );
}

/** Success rate on a 0..100% scale, with the 95% interval drawn as a band behind the mark. */
function RateWithInterval({ rate, ci }: { rate: number | null; ci: [number, number] | null }) {
  return (
    <div className="relative h-[9px]" aria-hidden="true">
      <div className="absolute inset-x-0 top-[3px] h-[3px] bg-sunken" />
      {ci ? (
        <div
          className="absolute top-[3px] h-[3px] bg-rule-strong"
          style={{ left: `${ci[0] * 100}%`, width: `${Math.max(0, ci[1] - ci[0]) * 100}%` }}
        />
      ) : null}
      {rate !== null ? <div className="absolute top-0 h-[9px] w-[2px] -translate-x-1/2 bg-ink" style={{ left: `${rate * 100}%` }} /> : null}
    </div>
  );
}

export function EvaluationDetailView({ id }: { id: string }) {
  const evaluation = useEvaluation(id);
  const e = evaluation.data;
  const live = !!e && !e.progress.complete && !e.cancel_requested_at;
  const summary = useEvaluationSummary(id, live);
  const results = useEvaluationResults(id, live);
  const cancel = useCancelEvaluation(id);
  const caps = useCapabilities();
  const { index } = useVersionIndex();
  const datasets = useDatasets();
  const dataset = datasets.data?.datasets.find((d) => d.id === e?.dataset_id);
  const s = summary.data;
  const rows = flatten(results.data, "results");
  const taskNames = new Map((s?.per_task ?? []).map((t) => [t.task_id, t.task_name]));
  const canCancel = !!e && !e.cancel_requested_at && !e.progress.complete;
  const info = e ? index.get(e.agent_version_id) : undefined;
  const categoryTotal = s ? Object.values(s.failure_categories).reduce((a, b) => a + b, 0) : 0;
  const simulated = s?.simulated ?? e?.context?.simulated ?? (info ? isSimulated(info.provider) : undefined);
  const costLabel = simulated ? " (simulated)" : "";

  return (
    <>
      <PageHeader
        eyebrow={
          <>
            <Link href="/evaluations" className="no-underline hover:text-ink">
              Evaluations
            </Link>{" "}
            / {shortId(id)}
          </>
        }
        title={e?.name ?? "Evaluation"}
        subtitle={
          e ? (
            <>
              {info ? (
                <>
                  <Link href={`/agents/${info.agentId}`}>{info.agentName}</Link> v{info.version}
                </>
              ) : (
                <Mono>{shortId(e.agent_version_id)}</Mono>
              )}{" "}
              on {dataset ? `${dataset.name} v${dataset.version}` : <Mono>{shortId(e.dataset_id)}</Mono>}, {e.repetitions}{" "}
              repetition{e.repetitions === 1 ? "" : "s"} per task. Queued <Mono className="text-[12.5px]">{formatTimestamp(e.created_at)}</Mono> UTC.
            </>
          ) : undefined
        }
        actions={
          e ? (
            <>
              {live ? <Live /> : <EvalStateBadge state={progressSummary(e).state} />}
              {caps.readOnly ? null : (
                <Button variant="danger" disabled={!canCancel || cancel.isPending} onClick={() => cancel.mutate()}>
                  {cancel.isPending ? "Cancelling…" : "Cancel evaluation"}
                </Button>
              )}
            </>
          ) : null
        }
      />
      <Gate query={evaluation}>
        {e ? (
          <div className="space-y-10">
            {cancel.isError ? <Notice title={describeWriteError(cancel.error).title}>{describeWriteError(cancel.error).body}</Notice> : null}

            <Gate query={summary} loadingLabel="Loading summary">
              {s ? (
                <div className="space-y-6">
                  <Readouts>
                    <Stat
                      label="Success rate"
                      value={formatPercent(s.success_rate)}
                      meter={<RateWithInterval rate={s.success_rate} ci={s.ci95} />}
                      hint={s.ci95 ? `95% CI ${formatPercent(s.ci95[0])} to ${formatPercent(s.ci95[1])}` : "nothing graded yet"}
                    />
                    <Stat label="Passed / graded" value={`${s.passed} / ${s.graded}`} hint={`${s.attempts} attempt${s.attempts === 1 ? "" : "s"}${s.complete ? "" : " so far"}`} />
                    <Stat
                      label="Mean score"
                      value={formatScore(s.mean_score?.overall)}
                      meter={<Meter value={s.mean_score ? Number(s.mean_score.overall) : null} />}
                      hint="weighted, 0 to 1"
                    />
                    <Stat
                      label={`Cost / task${costLabel}`}
                      value={formatCost(s.cost_usd.mean_per_attempt)}
                      hint={simulated ? `${formatCost(s.cost_usd.total)} total, priced from the fake model's table; nothing was spent` : `${formatCost(s.cost_usd.total)} total`}
                    />
                  </Readouts>
                  <LatencyReadouts latency={s.latency_ms} />
                  {s.rates ? <RateReadouts rates={s.rates} /> : null}
                  <Readouts>
                    <Stat label="Platform failures" value={formatInt(s.platform_failures)} hint="sandbox or model trouble; not counted against the agent" />
                    <Stat label="Evaluation errors" value={formatInt(s.evaluation_errors)} hint="the grader itself failed" />
                    <Stat label="Ungraded / in progress" value={`${s.ungraded} / ${s.in_progress}`} />
                    <Stat label="Tokens in / out" value={`${formatInt(s.tokens.tokens_in)} / ${formatInt(s.tokens.tokens_out)}`} />
                  </Readouts>
                </div>
              ) : null}
            </Gate>

            <Section title="What ran">
              <ContextBlock e={e} info={info} dataset={dataset} spent={s?.cost_usd.total ?? null} simulated={simulated} />
            </Section>

            <Section title="Progress" note={`${progressSummary(e).done} of ${e.progress.runs_total} runs finished`}>
              <ProgressBar e={e} />
              <div className="mt-5 grid gap-x-10 gap-y-6 md:grid-cols-2">
                <Table caption="Runs by status">
                  <thead>
                    <tr>
                      <Th>Run status</Th>
                      <Th className="text-right">Runs</Th>
                    </tr>
                  </thead>
                  <tbody>
                    {RUN_STATUSES.map((st) => (
                      <tr key={st}>
                        <Td>
                          <RunStatusBadge status={st} />
                        </Td>
                        <Num>{e.progress.runs_by_status[st] ?? 0}</Num>
                      </tr>
                    ))}
                  </tbody>
                </Table>
                <Table caption="Evaluations by status">
                  <thead>
                    <tr>
                      <Th>Grading status</Th>
                      <Th className="text-right">Count</Th>
                    </tr>
                  </thead>
                  <tbody>
                    {Object.entries(e.progress.evaluations_by_status).map(([k, v]) => (
                      <tr key={k}>
                        <Td className="font-mono text-[12px] text-ink-2">{k}</Td>
                        <Num>{v}</Num>
                      </tr>
                    ))}
                    <tr>
                      <Td className="text-muted">awaiting evaluation</Td>
                      <Num>{e.progress.awaiting_evaluation}</Num>
                    </tr>
                  </tbody>
                </Table>
              </div>
            </Section>

            {s && s.per_task.length > 0 ? (
              <Section title="Per task">
                <Table caption="Per-task results">
                  <thead>
                    <tr>
                      <Th>Task</Th>
                      <Th className="hidden sm:table-cell">Difficulty</Th>
                      <Th className="hidden text-right sm:table-cell">Attempts</Th>
                      <Th className="text-right">Passed / graded</Th>
                      <Th className="text-right">Success</Th>
                      <Th className="hidden text-right md:table-cell">Mean score</Th>
                    </tr>
                  </thead>
                  <tbody>
                    {s.per_task.map((t) => (
                      <tr key={t.task_id}>
                        <Td>
                          <Link href={`/runs?evaluation_id=${e.id}&task_id=${t.task_id}`} className="no-underline hover:underline">
                            {t.task_name}
                          </Link>
                        </Td>
                        <Td className="hidden text-muted sm:table-cell">{t.difficulty ?? "–"}</Td>
                        <Num className="hidden sm:table-cell">{t.attempts}</Num>
                        <Num>
                          {t.passed} / {t.graded}
                        </Num>
                        <Td className="text-right">
                          <RateCell rate={t.success_rate} text={formatPercent(t.success_rate)} />
                        </Td>
                        <Num className="hidden md:table-cell">{formatScore(t.mean_overall)}</Num>
                      </tr>
                    ))}
                  </tbody>
                </Table>
              </Section>
            ) : null}

            {s && categoryTotal > 0 ? (
              <Section title="Outcomes by category" note="every graded attempt lands in exactly one category">
                <div className="max-w-2xl">
                  <Table caption="Failure categories">
                    <thead>
                      <tr>
                        <Th>Category</Th>
                        <Th className="text-right">Attempts</Th>
                        <Th className="w-1/2">
                          <span className="sr-only">Share</span>
                        </Th>
                      </tr>
                    </thead>
                    <tbody>
                      {Object.entries(s.failure_categories)
                        .sort((a, b) => b[1] - a[1])
                        .map(([k, v]) => (
                          <tr key={k}>
                            <Td>
                              <CategoryBadge category={k} />
                            </Td>
                            <Num>{v}</Num>
                            <Td>
                              <Meter value={v / categoryTotal} tone={k === "passed" ? "good" : "ink"} />
                            </Td>
                          </tr>
                        ))}
                    </tbody>
                  </Table>
                </div>
              </Section>
            ) : null}

            <Section title="Results" note={live ? "updates every five seconds while runs are in flight" : undefined}>
              <Gate query={results} loadingLabel="Loading results">
                {rows.length === 0 ? (
                  <Empty>No runs in this evaluation.</Empty>
                ) : (
                  <>
                    <Table caption="Run results">
                      <thead>
                        <tr>
                          <Th>Task</Th>
                          <Th className="text-right">Rep</Th>
                          <Th>Run</Th>
                          <Th>Status</Th>
                          <Th className="hidden lg:table-cell">Outcome</Th>
                          <Th>Category</Th>
                          <Th className="text-right">Score</Th>
                          <Th className="hidden text-right md:table-cell">Duration</Th>
                          <Th className="hidden text-right md:table-cell">Cost{simulated ? " (sim.)" : ""}</Th>
                        </tr>
                      </thead>
                      <tbody>
                        {rows.map((r) => (
                          <tr key={r.run_id} className="hover:bg-sunken/50">
                            <Td>{taskNames.get(r.task_id) ?? <IdLink id={r.task_id} />}</Td>
                            <Num className="text-faint">{r.repetition}</Num>
                            <Td>
                              <IdLink id={r.run_id} href={`/runs/${r.run_id}`} />
                            </Td>
                            <Td>
                              <RunStatusBadge status={r.run_status} />
                            </Td>
                            <Td className="hidden font-mono text-[11.5px] text-muted lg:table-cell">{r.run_outcome ?? "–"}</Td>
                            <Td>{r.evaluation ? <CategoryBadge category={r.evaluation.failure_category} /> : <span className="text-faint">–</span>}</Td>
                            <Num>{formatScore(r.evaluation?.score?.overall)}</Num>
                            <Num className="hidden md:table-cell">{formatDuration(r.duration_ms)}</Num>
                            <Num className="hidden md:table-cell">{formatCost(r.cost_usd)}</Num>
                          </tr>
                        ))}
                      </tbody>
                    </Table>
                    <LoadMore
                      hasNext={!!results.hasNextPage}
                      loading={results.isFetchingNextPage}
                      onClick={() => void results.fetchNextPage()}
                      shown={rows.length}
                    />
                  </>
                )}
              </Gate>
            </Section>

            <Section title="Configuration">
              <KeyValue
                columns={2}
                items={[
                  ["Agent version id", <IdLink key="a" id={e.agent_version_id} />],
                  ["Repetitions", <Mono key="r">{e.repetitions}</Mono>],
                  [
                    "Attempt budgets",
                    <Mono key="b">
                      runs {e.run_max_attempts}, grading {e.eval_max_attempts}
                    </Mono>,
                  ],
                  [
                    "Scoring weights",
                    <Mono key="w" className="text-[12px]">
                      success {e.scoring_config.weights.task_success} · tests {e.scoring_config.weights.test_score} · reliability{" "}
                      {e.scoring_config.weights.reliability} · efficiency {e.scoring_config.weights.efficiency}
                    </Mono>,
                  ],
                  ["Created", <Timestamp key="c" value={formatTimestamp(e.created_at)} />],
                  ["Cancel requested", <Timestamp key="cr" value={formatTimestamp(e.cancel_requested_at)} />],
                ]}
              />
            </Section>
          </div>
        ) : null}
      </Gate>
    </>
  );
}

function LatencyReadouts({ latency }: { latency: EvaluationSummary["latency_ms"] }) {
  const n = latency?.count;
  const nHint = n !== undefined ? `n = ${formatInt(n)} attempts` : undefined;
  return (
    <Readouts>
      <Stat label="Latency p50" value={formatDuration(latency?.p50)} hint={latency ? nHint : "no finished attempts yet"} />
      <Stat label="Latency p95" value={formatDuration(latency?.p95)} hint="nearest rank" />
      <Stat label="Latency p99" value={formatDuration(latency?.p99)} hint={latency && latency.p99 === undefined ? "not reported by this API" : "nearest rank"} />
      <Stat label="Latency mean" value={formatDuration(latency?.mean)} hint={latency && latency.mean === undefined ? "not reported by this API" : undefined} />
    </Readouts>
  );
}

function RateReadouts({ rates: r }: { rates: SummaryRates }) {
  const of = (n: number) => (r.finished ? `${n} of ${r.finished} finished runs` : "no finished runs yet");
  return (
    <Readouts>
      <Stat label="Completion rate" value={formatPercent(r.completion_rate)} hint={`${of(r.completed)}; ${r.scheduled} scheduled`} />
      <Stat label="Failure rate" value={formatPercent(r.failure_rate)} hint={of(r.failed)} />
      <Stat label="Timeout rate" value={formatPercent(r.timeout_rate)} hint={of(r.timed_out)} />
      <Stat label="Retry rate" value={formatPercent(r.retry_rate)} hint={`${of(r.retried)} needed more than one attempt`} />
    </Readouts>
  );
}

/** Agent, model, dataset and pricing an evaluation ran with, from its context (or local lookups on an older API). */
function ContextBlock({
  e,
  info,
  dataset,
  spent,
  simulated,
}: {
  e: EvaluationRead;
  info: VersionInfo | undefined;
  dataset: DatasetSummary | undefined;
  spent: string | null;
  simulated: boolean | undefined;
}) {
  const c = e.context ?? null;
  const agentId = c?.agent_id ?? info?.agentId;
  const agentName = c?.agent_name ?? info?.agentName;
  const agentVersion = c?.agent_version ?? info?.version;
  const provider = c?.provider ?? info?.provider;
  const model = c?.model ?? info?.model;
  const datasetName = c ? `${c.dataset_name} v${c.dataset_version}` : dataset ? `${dataset.name} v${dataset.version}` : null;
  const taskCount = c?.task_count ?? dataset?.task_count;
  const budget = e.max_cost_usd ?? null;
  const items: [string, ReactNode][] = [
    [
      "Agent",
      agentName && agentId ? (
        <span key="a">
          <Link href={`/agents/${agentId}`}>{agentName}</Link> <Mono className="text-[12px] text-faint">v{agentVersion}</Mono>
        </span>
      ) : (
        <IdLink key="a" id={e.agent_version_id} />
      ),
    ],
    [
      "Model",
      model ? (
        <span key="m" className="inline-flex flex-wrap items-center gap-2">
          <Mono className="text-[12.5px]">
            {provider}/{model}
          </Mono>
          <ProviderMark provider={provider} simulated={simulated} />
        </span>
      ) : (
        "–"
      ),
    ],
    ["Dataset", datasetName ? `${datasetName}${taskCount !== undefined ? `, ${taskCount} task${taskCount === 1 ? "" : "s"}` : ""}` : <IdLink key="d" id={e.dataset_id} />],
    ["Evaluator version", <Mono key="ev" className="text-[12px]">{c?.evaluator_version ?? "–"}</Mono>],
    ["Scoring config", <Mono key="sc" className="text-[12px]">{e.scoring_config.version} · {shortId(e.scoring_config_hash)}</Mono>],
    ["Pricing version", <Mono key="pv" className="text-[12px]">{c?.pricing_version ?? "–"}</Mono>],
    [
      "Budget",
      budget ? (
        <Mono key="b" className="text-[12.5px]">
          {formatCost(budget)}
        </Mono>
      ) : (
        <span key="b" className="text-faint">{simulated ? "none (simulated, no spend)" : "none set"}</span>
      ),
    ],
    [
      simulated ? "Spent (simulated)" : "Spent",
      <span key="sp">
        <Mono className="text-[12.5px]">{formatCost(spent)}</Mono>
        {budget && spent !== null && Number(budget) > 0 ? (
          <span className="ml-2 inline-flex w-24 align-middle">
            <Meter value={Number(spent) / Number(budget)} tone={Number(spent) >= Number(budget) ? "bad" : "ink"} label={`${formatPercent(Number(spent) / Number(budget))} of budget`} />
          </span>
        ) : null}
      </span>,
    ],
  ];
  return <KeyValue columns={2} items={items} />;
}

function ProgressBar({ e }: { e: EvaluationRead }) {
  const total = e.progress.runs_total;
  const { done } = progressSummary(e);
  return (
    <div role="img" aria-label={`${done} of ${total} runs finished`}>
      <StatusStrip byStatus={e.progress.runs_by_status} total={total} height={8} />
      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1">
        {RUN_STATUSES.filter((st) => (e.progress.runs_by_status[st] ?? 0) > 0).map((st) => (
          <span key={st} className="inline-flex items-baseline gap-1.5 font-mono text-[11px] text-faint" aria-hidden="true">
            <span className="inline-block size-[7px] translate-y-[-1px]" style={{ background: `var(--tone-${toneVar(st)})` }} />
            {st.toLowerCase()} {e.progress.runs_by_status[st]}
          </span>
        ))}
      </div>
    </div>
  );
}

const TONE_VAR: Record<string, string> = {
  QUEUED: "neutral",
  CLAIMED: "info",
  RUNNING: "active",
  RETRYING: "warn",
  COMPLETED: "good",
  FAILED: "bad",
  CANCELLED: "muted",
};
const toneVar = (st: string) => TONE_VAR[st] ?? "neutral";

export function CompareView({ ids }: { ids: string[] }) {
  const compare = useCompare(ids);
  const { index } = useVersionIndex();
  const datasets = useDatasets();
  const evalNames = useEvaluations({}, ids.length >= 2);
  const evalById = new Map(flatten(evalNames.data, "evaluations").map((e) => [e.id, e]));
  const c = compare.data;

  if (ids.length < 2) {
    return (
      <>
        <PageHeader eyebrow="Benchmarks" title="Compare evaluations" />
        <Empty>
          Pick 2 to 5 evaluations on the <Link href="/evaluations">Evaluations</Link> page, or open
          /evaluations/compare?ids=a,b,c. They need the same dataset and scoring settings, otherwise the numbers would
          not mean the same thing.
        </Empty>
      </>
    );
  }

  const contextById = new Map((c?.evaluations ?? []).map((x) => [x.evaluation_id, x.context ?? null]));
  const label = (evalId: string, versionId: string) => {
    const ctx = contextById.get(evalId);
    if (ctx) return `${ctx.agent_name} v${ctx.agent_version}`;
    const info = index.get(versionId);
    const name = evalById.get(evalId)?.name;
    return info ? `${info.agentName} v${info.version}` : (name ?? shortId(evalId));
  };
  const categories = c
    ? [...new Set(c.evaluations.flatMap((x) => Object.keys(x.summary.failure_categories)))].sort((a, b) =>
        a === "passed" ? -1 : b === "passed" ? 1 : a.localeCompare(b),
      )
    : [];
  const dataset = datasets.data?.datasets.find((d) => d.id === c?.dataset_id);

  return (
    <>
      <PageHeader
        eyebrow={
          <>
            <Link href="/evaluations" className="no-underline hover:text-ink">
              Evaluations
            </Link>{" "}
            / compare
          </>
        }
        title="Compare evaluations"
        subtitle={
          c ? (
            <>
              Dataset {dataset ? `${dataset.name} v${dataset.version}` : <Mono>{shortId(c.dataset_id)}</Mono>}, scoring{" "}
              <Mono className="text-[12.5px]">{shortId(c.scoring_config_hash)}</Mono>. Per-task deltas are in points against the first column.
            </>
          ) : undefined
        }
      />
      <Gate query={compare}>
        {c ? (
          <div className="space-y-10">
            <CompareWarnings c={c} label={label} />

            <Section title="Success rate" note="bar: observed rate · whisker: Wilson 95% interval · n: graded attempts">
              <SuccessChart
                rows={c.evaluations.map((x) => ({
                  name: label(x.evaluation_id, x.agent_version_id),
                  rate: x.summary.success_rate,
                  ci: x.summary.ci95,
                  passed: x.summary.passed,
                  graded: x.summary.graded,
                }))}
              />
            </Section>

            {c.differences && c.differences.length > 0 ? (
              <Section title="What differs" note="configuration that is not the same across these evaluations">
                <Table caption="Configuration differences">
                  <thead>
                    <tr>
                      <Th>Field</Th>
                      {c.evaluations.map((x) => (
                        <Th key={x.evaluation_id} className="normal-case! tracking-normal!">
                          <span className="font-sans text-[12.5px] text-ink-2">{label(x.evaluation_id, x.agent_version_id)}</span>
                        </Th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {c.differences.map((d) => (
                      <tr key={d.field}>
                        <Td className="whitespace-nowrap font-mono text-[12px] text-muted">{d.field.replace(/_/g, " ")}</Td>
                        {c.evaluations.map((x) => {
                          const v = d.values[x.evaluation_id];
                          const text = v === null || v === undefined ? "–" : String(v);
                          return (
                            <Td key={x.evaluation_id} className="break-all font-mono text-[12px] text-ink-2">
                              {d.field === "agent_config_hash" ? text.slice(0, 12) : text}
                            </Td>
                          );
                        })}
                      </tr>
                    ))}
                  </tbody>
                </Table>
              </Section>
            ) : null}

            <Section title="Headline">
              <Table caption="Evaluation comparison">
                <thead>
                  <tr>
                    <Th>Metric</Th>
                    {c.evaluations.map((x) => (
                      <Th key={x.evaluation_id} className="text-right normal-case! tracking-normal!">
                        <Link href={`/evaluations/${x.evaluation_id}`} className="font-sans text-[12.5px] text-ink">
                          {label(x.evaluation_id, x.agent_version_id)}
                        </Link>
                      </Th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  <MetricRow label="Success rate" cells={c.evaluations.map((x) => formatPercent(x.summary.success_rate))} strong />
                  <MetricRow
                    label="95% CI"
                    cells={c.evaluations.map((x) =>
                      x.summary.ci95 ? `${formatPercent(x.summary.ci95[0])} to ${formatPercent(x.summary.ci95[1])}` : "–",
                    )}
                  />
                  <MetricRow label="Passed / graded" cells={c.evaluations.map((x) => `${x.summary.passed} / ${x.summary.graded}`)} />
                  {c.evaluations.some((x) => x.distinguishable_from_first !== undefined) ? (
                    <MetricRow
                      label="Differs from first at 95%"
                      cells={c.evaluations.map((x, i) =>
                        i === 0 ? "baseline" : x.distinguishable_from_first === true ? "yes" : x.distinguishable_from_first === false ? "no" : "–",
                      )}
                    />
                  ) : null}
                  <MetricRow
                    label="Model"
                    cells={c.evaluations.map((x) =>
                      x.context ? (
                        <span key="m" className="inline-flex items-center gap-1.5">
                          {x.context.model} <ProviderMark provider={x.context.provider} simulated={x.context.simulated} />
                        </span>
                      ) : (
                        "–"
                      ),
                    )}
                  />
                  <MetricRow label="Mean score" cells={c.evaluations.map((x) => formatScore(x.summary.mean_score?.overall))} />
                  <MetricRow label="Latency p50" cells={c.evaluations.map((x) => formatDuration(x.summary.latency_ms?.p50))} />
                  <MetricRow label="Latency p95" cells={c.evaluations.map((x) => formatDuration(x.summary.latency_ms?.p95))} />
                  <MetricRow label="Latency p99" cells={c.evaluations.map((x) => formatDuration(x.summary.latency_ms?.p99))} />
                  <MetricRow label="Cost / task" cells={c.evaluations.map((x) => formatCost(x.summary.cost_usd.mean_per_attempt))} />
                  <MetricRow label="Total cost" cells={c.evaluations.map((x) => formatCost(x.summary.cost_usd.total))} />
                  <MetricRow label="Platform failures" cells={c.evaluations.map((x) => formatInt(x.summary.platform_failures))} />
                </tbody>
              </Table>
            </Section>

            <Section title="Outcomes by category">
              {categories.length === 0 ? (
                <Empty>Nothing graded yet, so there are no outcomes to compare.</Empty>
              ) : (
                <Table caption="Failure categories by evaluation">
                  <thead>
                    <tr>
                      <Th>Category</Th>
                      {c.evaluations.map((x) => (
                        <Th key={x.evaluation_id} className="text-right normal-case! tracking-normal!">
                          <span className="font-sans text-[12.5px] text-ink-2">{label(x.evaluation_id, x.agent_version_id)}</span>
                        </Th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {categories.map((cat) => (
                      <tr key={cat}>
                        <Td>
                          <CategoryBadge category={cat} />
                        </Td>
                        {c.evaluations.map((x) => {
                          const n = x.summary.failure_categories[cat] ?? 0;
                          const total = Object.values(x.summary.failure_categories).reduce((a, b) => a + b, 0);
                          return (
                            <Td key={x.evaluation_id} className="text-right">
                              <span className="inline-flex items-center gap-2">
                                <span className={`font-mono text-[12.5px] tabular-nums ${n ? "text-ink" : "text-faint"}`}>{n}</span>
                                <Meter value={total ? n / total : 0} className="w-12" tone={cat === "passed" ? "good" : "ink"} />
                              </span>
                            </Td>
                          );
                        })}
                      </tr>
                    ))}
                  </tbody>
                </Table>
              )}
            </Section>

            <Section title="Per-task success rate">
              <Table caption="Per-task success rate">
                <thead>
                  <tr>
                    <Th>Task</Th>
                    {c.evaluations.map((x) => (
                      <Th key={x.evaluation_id} className="text-right normal-case! tracking-normal!">
                        <span className="font-sans text-[12.5px] text-ink-2">{label(x.evaluation_id, x.agent_version_id)}</span>
                      </Th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {c.per_task.map((t) => {
                    const name = c.evaluations
                      .flatMap((x) => x.summary.per_task)
                      .find((p) => p.task_id === t.task_id)?.task_name;
                    return (
                      <tr key={t.task_id}>
                        <Td>{name ?? <IdLink id={t.task_id} />}</Td>
                        {c.evaluations.map((x, i) => {
                          const rate = t.success_rate[x.evaluation_id] ?? null;
                          const delta = t.delta_vs_first[x.evaluation_id] ?? null;
                          return (
                            <Td key={x.evaluation_id} className="whitespace-nowrap text-right font-mono text-[12.5px] tabular-nums">
                              {formatPercent(rate)}
                              {i > 0 && delta !== null ? (
                                <span className={`ml-1.5 text-[11.5px] ${delta < 0 ? "text-bad" : delta > 0 ? "text-good" : "text-faint"}`}>
                                  {delta > 0 ? "+" : ""}
                                  {Math.round(delta * 100)} pt
                                </span>
                              ) : null}
                            </Td>
                          );
                        })}
                      </tr>
                    );
                  })}
                </tbody>
              </Table>
            </Section>
          </div>
        ) : null}
      </Gate>
    </>
  );
}

/** Warnings from the API and an explicit note for every pair the data cannot tell apart. */
function CompareWarnings({ c, label }: { c: Compare; label: (evalId: string, versionId: string) => string }) {
  const first = c.evaluations[0];
  const overlaps = first ? c.evaluations.slice(1).filter((x) => x.distinguishable_from_first === false) : [];
  const warnings = c.warnings ?? [];
  if (!first || (warnings.length === 0 && overlaps.length === 0)) return null;
  const firstName = label(first.evaluation_id, first.agent_version_id);
  return (
    <div className="space-y-3">
      {warnings.length > 0 ? (
        <Notice tone="warn" title="Read this comparison with care">
          <ul className="list-disc space-y-0.5 pl-4">
            {warnings.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
        </Notice>
      ) : null}
      {overlaps.map((x) => (
        <Notice key={x.evaluation_id} tone="info" title={`${label(x.evaluation_id, x.agent_version_id)}: not distinguishable from ${firstName} at 95%`}>
          The 95% intervals overlap, so this data does not show that one success rate is higher than the other. More
          repetitions would narrow the intervals.
        </Notice>
      ))}
    </div>
  );
}

interface SuccessRow {
  name: string;
  rate: number | null;
  ci: [number, number] | null;
  passed: number;
  graded: number;
}

function SuccessChart({ rows }: { rows: SuccessRow[] }) {
  const data = rows.map((r) => ({
    ...r,
    label: `${r.name} (n=${r.graded})`,
    value: r.rate ?? 0,
    // ErrorBar takes [below, above] offsets from the value.
    err: r.ci && r.rate !== null ? [Math.max(0, r.rate - r.ci[0]), Math.max(0, r.ci[1] - r.rate)] : [0, 0],
  }));
  return (
    <div style={{ height: 56 + rows.length * 34 }} aria-hidden="true" className="max-w-3xl">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} layout="vertical" margin={{ top: 4, right: 16, bottom: 18, left: 0 }} barCategoryGap={12}>
          <CartesianGrid stroke={GRID_STROKE} horizontal={false} />
          <XAxis
            type="number"
            domain={[0, 1]}
            ticks={[0, 0.25, 0.5, 0.75, 1]}
            tickFormatter={(v: number) => `${Math.round(v * 100)}%`}
            stroke={AXIS_STROKE}
            tick={AXIS_TICK}
            tickLine={false}
            label={{ value: "Success rate (passed / graded)", position: "insideBottom", offset: -12, style: AXIS_LABEL }}
          />
          <YAxis
            type="category"
            dataKey="label"
            width={170}
            stroke={AXIS_STROKE}
            tick={{ fontSize: 12, fill: "var(--ink-2)" }}
            tickLine={false}
          />
          <Tooltip
            cursor={{ fill: "var(--sunken)" }}
            content={({ active, payload }) => {
              const r = active ? (payload?.[0]?.payload as SuccessRow | undefined) : undefined;
              if (!r) return null;
              return (
                <TooltipBox
                  title={r.name}
                  rows={[
                    { color: "var(--ink-2)", label: "success", value: formatPercent(r.rate) },
                    { color: "var(--accent)", label: "95% interval", value: r.ci ? `${formatPercent(r.ci[0])} to ${formatPercent(r.ci[1])}` : "–" },
                    { label: "passed / graded", value: `${r.passed} / ${r.graded}` },
                  ]}
                />
              );
            }}
          />
          <Bar dataKey="value" name="success" fill="var(--ink-2)" barSize={8} isAnimationActive={false}>
            <ErrorBar dataKey="err" width={6} strokeWidth={1.2} stroke="var(--accent)" direction="x" />
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

function MetricRow({ label, cells, strong }: { label: string; cells: ReactNode[]; strong?: boolean }) {
  return (
    <tr>
      <Td className="whitespace-nowrap text-muted">{label}</Td>
      {cells.map((v, i) => (
        <Num key={i} className={strong ? "font-medium text-ink" : "text-ink-2"}>
          {v}
        </Num>
      ))}
    </tr>
  );
}
