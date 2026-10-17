"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import {
  Badge,
  Button,
  CategoryBadge,
  Empty,
  ErrorState,
  Gate,
  IdLink,
  KeyValue,
  LoadMore,
  Mono,
  PageHeader,
  Panel,
  RunStatusBadge,
  Stat,
  Table,
  Td,
  Th,
} from "@/components/ui";
import { flatten } from "@/components/pages-util";
import { RUN_STATUSES, TERMINAL_RUN_STATUSES, type EvaluationRead, type EvaluationSummary } from "@/lib/api/types";
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
import { RUN_STATUS_COLOR } from "@/lib/status";

const CATEGORY_COLORS = ["#6aa7ff", "#3fb97a", "#e5a63a", "#ef5f5f", "#a78bfa", "#5eb0ef", "#d98fd0", "#8b93a0"];

function AgentCell({ versionId, index }: { versionId: string; index: Map<string, VersionInfo> }) {
  const info = index.get(versionId);
  if (!info) return <IdLink id={versionId} />;
  return (
    <span title={`${info.provider}/${info.model} (version ${versionId})`}>
      <Link href={`/agents/${info.agentId}`}>{info.agentName}</Link> <Mono className="text-muted">v{info.version}</Mono>
    </span>
  );
}

export function progressSummary(e: EvaluationRead) {
  const by = e.progress.runs_by_status;
  const done = TERMINAL_RUN_STATUSES.reduce((n, s) => n + (by[s] ?? 0), 0);
  const state = e.cancel_requested_at ? "CANCELLED" : e.progress.complete ? "COMPLETE" : "RUNNING";
  return { done, total: e.progress.runs_total, state };
}

function EvalStateBadge({ state }: { state: string }) {
  return <Badge tone={state === "COMPLETE" ? "good" : state === "CANCELLED" ? "muted" : "active"}>{state}</Badge>;
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
          {onToggle ? <Th>Compare</Th> : null}
          <Th>Evaluation</Th>
          <Th>Agent</Th>
          <Th>Dataset</Th>
          <Th>State</Th>
          <Th className="text-right">Runs done</Th>
          <Th className="text-right">Success</Th>
          <Th className="text-right">Latency p50</Th>
          <Th className="text-right">Cost / task</Th>
          <Th>Created</Th>
        </tr>
      </thead>
      <tbody>
        {evaluations.map((e, i) => {
          const s = summaries[i];
          const p = progressSummary(e);
          return (
            <tr key={e.id}>
              {onToggle ? (
                <Td>
                  <input
                    type="checkbox"
                    aria-label={`Select ${e.name} for comparison`}
                    checked={selected?.includes(e.id) ?? false}
                    onChange={() => onToggle(e.id)}
                  />
                </Td>
              ) : null}
              <Td>
                <Link href={`/evaluations/${e.id}`}>{e.name}</Link>
                <div className="font-mono text-xs text-muted">{shortId(e.id)}</div>
              </Td>
              <Td>
                <AgentCell versionId={e.agent_version_id} index={index} />
              </Td>
              <Td>{datasetNames?.get(e.dataset_id) ?? <IdLink id={e.dataset_id} />}</Td>
              <Td>
                <EvalStateBadge state={p.state} />
              </Td>
              <Td className="text-right font-mono tabular-nums">
                {p.done}/{p.total}
              </Td>
              <Td className="text-right font-mono tabular-nums">{formatPercent(s?.success_rate)}</Td>
              <Td className="text-right font-mono tabular-nums">{formatDuration(s?.latency_ms?.p50)}</Td>
              <Td className="text-right font-mono tabular-nums">{formatCost(s?.cost_usd.mean_per_attempt)}</Td>
              <Td className="whitespace-nowrap font-mono">{formatTimestamp(e.created_at)}</Td>
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
        title="Evaluations"
        subtitle="Benchmark batches: one agent version over every task of a frozen dataset. Select 2 to 5 to compare."
        actions={
          <Button disabled={!canCompare} onClick={() => router.push(`/evaluations/compare?ids=${selected.join(",")}`)}>
            Compare selected ({selected.length})
          </Button>
        }
      />
      <Gate query={query}>
        {evaluations.length === 0 ? (
          <Empty>No evaluations yet. Start one with POST /api/v1/evaluations.</Empty>
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

export function EvaluationDetailView({ id }: { id: string }) {
  const evaluation = useEvaluation(id);
  const e = evaluation.data;
  const live = !!e && !e.progress.complete && !e.cancel_requested_at;
  const summary = useEvaluationSummary(id, live);
  const results = useEvaluationResults(id, live);
  const cancel = useCancelEvaluation(id);
  const { index } = useVersionIndex();
  const datasets = useDatasets();
  const dataset = datasets.data?.datasets.find((d) => d.id === e?.dataset_id);
  const s = summary.data;
  const rows = flatten(results.data, "results");
  const taskNames = new Map((s?.per_task ?? []).map((t) => [t.task_id, t.task_name]));
  const canCancel = !!e && !e.cancel_requested_at && !e.progress.complete;

  return (
    <>
      <PageHeader
        title={e?.name ?? "Evaluation"}
        subtitle={e ? <span className="font-mono">{e.id}</span> : undefined}
        actions={
          e ? (
            <>
              <EvalStateBadge state={progressSummary(e).state} />
              <Button variant="danger" disabled={!canCancel || cancel.isPending} onClick={() => cancel.mutate()}>
                {cancel.isPending ? "Cancelling…" : "Cancel evaluation"}
              </Button>
            </>
          ) : null
        }
      />
      <Gate query={evaluation}>
        {e ? (
          <div className="space-y-4">
            {cancel.isError ? <ErrorState error={cancel.error} /> : null}
            <Panel title="Summary">
              <KeyValue
                items={[
                  ["Agent", <AgentCell key="a" versionId={e.agent_version_id} index={index} />],
                  ["Dataset", dataset ? `${dataset.name} v${dataset.version}` : <IdLink key="d" id={e.dataset_id} />],
                  ["Repetitions", <Mono key="r">{e.repetitions}</Mono>],
                  ["Created", <Mono key="c">{formatTimestamp(e.created_at)}</Mono>],
                  ["Cancel requested", <Mono key="cr">{formatTimestamp(e.cancel_requested_at)}</Mono>],
                  [
                    "Attempt budgets",
                    <Mono key="b">
                      runs {e.run_max_attempts}, grading {e.eval_max_attempts}
                    </Mono>,
                  ],
                  [
                    "Scoring",
                    <Mono key="w" >
                      {e.scoring_config.version}: success {e.scoring_config.weights.task_success}, tests{" "}
                      {e.scoring_config.weights.test_score}, reliability {e.scoring_config.weights.reliability},
                      efficiency {e.scoring_config.weights.efficiency}
                    </Mono>,
                  ],
                ]}
              />
            </Panel>

            <Gate query={summary} loadingLabel="Loading summary">
              {s ? (
                <dl className="grid grid-cols-2 gap-3 md:grid-cols-4">
                  <Stat
                    label="Success rate"
                    value={formatPercent(s.success_rate)}
                    hint={s.ci95 ? `95% CI ${formatPercent(s.ci95[0])} to ${formatPercent(s.ci95[1])}` : "nothing graded yet"}
                  />
                  <Stat label="Passed / graded" value={`${s.passed} / ${s.graded}`} hint={`${s.attempts} attempts`} />
                  <Stat
                    label="Latency p50 / p95"
                    value={`${formatDuration(s.latency_ms?.p50)} / ${formatDuration(s.latency_ms?.p95)}`}
                  />
                  <Stat
                    label="Cost / task"
                    value={formatCost(s.cost_usd.mean_per_attempt)}
                    hint={`${formatCost(s.cost_usd.total)} total`}
                  />
                  <Stat label="Mean score" value={formatScore(s.mean_score?.overall)} />
                  <Stat
                    label="Platform failures"
                    value={formatInt(s.platform_failures)}
                    hint="excluded from success rate"
                  />
                  <Stat label="Evaluation errors" value={formatInt(s.evaluation_errors)} />
                  <Stat label="Ungraded / in progress" value={`${s.ungraded} / ${s.in_progress}`} />
                </dl>
              ) : null}
            </Gate>

            <Panel title="Progress">
              <ProgressBar e={e} />
              <div className="mt-3 grid gap-4 md:grid-cols-2">
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
                        <Td className="text-right font-mono tabular-nums">{e.progress.runs_by_status[st] ?? 0}</Td>
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
                        <Td className="font-mono">{k}</Td>
                        <Td className="text-right font-mono tabular-nums">{v}</Td>
                      </tr>
                    ))}
                    <tr>
                      <Td>awaiting evaluation</Td>
                      <Td className="text-right font-mono tabular-nums">{e.progress.awaiting_evaluation}</Td>
                    </tr>
                  </tbody>
                </Table>
              </div>
            </Panel>

            {s && Object.keys(s.failure_categories).length > 0 ? (
              <Panel title="Failure categories">
                <Table caption="Failure categories">
                  <thead>
                    <tr>
                      <Th>Category</Th>
                      <Th className="text-right">Attempts</Th>
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
                          <Td className="text-right font-mono tabular-nums">{v}</Td>
                        </tr>
                      ))}
                  </tbody>
                </Table>
              </Panel>
            ) : null}

            {s && s.per_task.length > 0 ? (
              <Panel title="Per task">
                <Table caption="Per-task results">
                  <thead>
                    <tr>
                      <Th>Task</Th>
                      <Th>Difficulty</Th>
                      <Th className="text-right">Attempts</Th>
                      <Th className="text-right">Passed / graded</Th>
                      <Th className="text-right">Success</Th>
                      <Th className="text-right">Mean score</Th>
                    </tr>
                  </thead>
                  <tbody>
                    {s.per_task.map((t) => (
                      <tr key={t.task_id}>
                        <Td>{t.task_name}</Td>
                        <Td>{t.difficulty ?? "–"}</Td>
                        <Td className="text-right font-mono tabular-nums">{t.attempts}</Td>
                        <Td className="text-right font-mono tabular-nums">
                          {t.passed} / {t.graded}
                        </Td>
                        <Td className="text-right font-mono tabular-nums">{formatPercent(t.success_rate)}</Td>
                        <Td className="text-right font-mono tabular-nums">{formatScore(t.mean_overall)}</Td>
                      </tr>
                    ))}
                  </tbody>
                </Table>
              </Panel>
            ) : null}

            <Panel title="Results">
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
                          <Th>Outcome</Th>
                          <Th>Category</Th>
                          <Th className="text-right">Score</Th>
                          <Th className="text-right">Duration</Th>
                          <Th className="text-right">Cost</Th>
                        </tr>
                      </thead>
                      <tbody>
                        {rows.map((r) => (
                          <tr key={r.run_id}>
                            <Td>{taskNames.get(r.task_id) ?? <IdLink id={r.task_id} />}</Td>
                            <Td className="text-right font-mono">{r.repetition}</Td>
                            <Td>
                              <IdLink id={r.run_id} href={`/runs/${r.run_id}`} />
                            </Td>
                            <Td>
                              <RunStatusBadge status={r.run_status} />
                            </Td>
                            <Td className="font-mono text-xs">{r.run_outcome ?? "–"}</Td>
                            <Td>{r.evaluation ? <CategoryBadge category={r.evaluation.failure_category} /> : "–"}</Td>
                            <Td className="text-right font-mono tabular-nums">{formatScore(r.evaluation?.score?.overall)}</Td>
                            <Td className="text-right font-mono tabular-nums">{formatDuration(r.duration_ms)}</Td>
                            <Td className="text-right font-mono tabular-nums">{formatCost(r.cost_usd)}</Td>
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
            </Panel>
          </div>
        ) : null}
      </Gate>
    </>
  );
}

function ProgressBar({ e }: { e: EvaluationRead }) {
  const total = e.progress.runs_total;
  const { done, state } = progressSummary(e);
  return (
    <div>
      <div className="mb-1 flex justify-between text-xs text-muted">
        <span className="font-mono">
          {done} of {total} runs finished
        </span>
        <span>{state.toLowerCase()}</span>
      </div>
      <div
        role="img"
        aria-label={`${done} of ${total} runs finished`}
        className="flex h-2 overflow-hidden rounded bg-panel2"
      >
        {RUN_STATUSES.map((st) => {
          const n = e.progress.runs_by_status[st] ?? 0;
          return n && total ? (
            <div key={st} title={`${st}: ${n}`} style={{ width: `${(n / total) * 100}%`, background: RUN_STATUS_COLOR[st] }} />
          ) : null;
        })}
      </div>
    </div>
  );
}

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
        <PageHeader title="Compare evaluations" />
        <Empty>
          Pick 2 to 5 evaluations on the <Link href="/evaluations">Evaluations</Link> page, or open
          /evaluations/compare?ids=a,b,c.
        </Empty>
      </>
    );
  }

  const label = (evalId: string, versionId: string) => {
    const info = index.get(versionId);
    const name = evalById.get(evalId)?.name;
    return info ? `${info.agentName} v${info.version}` : (name ?? shortId(evalId));
  };
  const categories = c
    ? [...new Set(c.evaluations.flatMap((x) => Object.keys(x.summary.failure_categories)))].sort()
    : [];
  const dataset = datasets.data?.datasets.find((d) => d.id === c?.dataset_id);

  return (
    <>
      <PageHeader
        title="Compare evaluations"
        subtitle={
          c ? (
            <>
              Dataset {dataset ? `${dataset.name} v${dataset.version}` : shortId(c.dataset_id)}, scoring hash{" "}
              <span className="font-mono">{shortId(c.scoring_config_hash)}</span>. Deltas are against the first column.
            </>
          ) : undefined
        }
      />
      <Gate query={compare}>
        {c ? (
          <div className="space-y-4">
            <Panel title="Headline">
              <Table caption="Evaluation comparison">
                <thead>
                  <tr>
                    <Th>Metric</Th>
                    {c.evaluations.map((x) => (
                      <Th key={x.evaluation_id} className="text-right">
                        <Link href={`/evaluations/${x.evaluation_id}`}>{label(x.evaluation_id, x.agent_version_id)}</Link>
                      </Th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  <MetricRow label="Success rate" cells={c.evaluations.map((x) => formatPercent(x.summary.success_rate))} />
                  <MetricRow
                    label="95% CI"
                    cells={c.evaluations.map((x) =>
                      x.summary.ci95 ? `${formatPercent(x.summary.ci95[0])} to ${formatPercent(x.summary.ci95[1])}` : "–",
                    )}
                  />
                  <MetricRow label="Passed / graded" cells={c.evaluations.map((x) => `${x.summary.passed} / ${x.summary.graded}`)} />
                  <MetricRow label="Latency p50" cells={c.evaluations.map((x) => formatDuration(x.summary.latency_ms?.p50))} />
                  <MetricRow label="Latency p95" cells={c.evaluations.map((x) => formatDuration(x.summary.latency_ms?.p95))} />
                  <MetricRow label="Cost / task" cells={c.evaluations.map((x) => formatCost(x.summary.cost_usd.mean_per_attempt))} />
                  <MetricRow label="Total cost" cells={c.evaluations.map((x) => formatCost(x.summary.cost_usd.total))} />
                  <MetricRow label="Mean score" cells={c.evaluations.map((x) => formatScore(x.summary.mean_score?.overall))} />
                  <MetricRow label="Platform failures" cells={c.evaluations.map((x) => formatInt(x.summary.platform_failures))} />
                </tbody>
              </Table>
            </Panel>

            <Panel title="Failure categories">
              {categories.length === 0 ? (
                <Empty>No graded attempts yet.</Empty>
              ) : (
                <div className="grid gap-4 lg:grid-cols-[2fr_1fr]">
                  <div className="h-64" aria-hidden="true">
                    <ResponsiveContainer width="100%" height="100%">
                      <BarChart
                        data={c.evaluations.map((x) => ({
                          name: label(x.evaluation_id, x.agent_version_id),
                          ...x.summary.failure_categories,
                        }))}
                      >
                        <CartesianGrid stroke="var(--line)" vertical={false} />
                        <XAxis dataKey="name" stroke="var(--muted)" tick={{ fontSize: 11 }} />
                        <YAxis allowDecimals={false} stroke="var(--muted)" tick={{ fontSize: 11 }} />
                        <Tooltip contentStyle={{ background: "var(--panel)", border: "1px solid var(--line)", fontSize: 12 }} />
                        <Legend wrapperStyle={{ fontSize: 11 }} />
                        {categories.map((cat, i) => (
                          <Bar key={cat} dataKey={cat} stackId="c" fill={CATEGORY_COLORS[i % CATEGORY_COLORS.length]} isAnimationActive={false} />
                        ))}
                      </BarChart>
                    </ResponsiveContainer>
                  </div>
                  <Table caption="Failure categories by evaluation">
                    <thead>
                      <tr>
                        <Th>Category</Th>
                        {c.evaluations.map((x) => (
                          <Th key={x.evaluation_id} className="text-right">
                            {label(x.evaluation_id, x.agent_version_id)}
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
                          {c.evaluations.map((x) => (
                            <Td key={x.evaluation_id} className="text-right font-mono tabular-nums">
                              {x.summary.failure_categories[cat] ?? 0}
                            </Td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </Table>
                </div>
              )}
            </Panel>

            <Panel title="Per-task success rate">
              <Table caption="Per-task success rate">
                <thead>
                  <tr>
                    <Th>Task</Th>
                    {c.evaluations.map((x) => (
                      <Th key={x.evaluation_id} className="text-right">
                        {label(x.evaluation_id, x.agent_version_id)}
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
                            <Td key={x.evaluation_id} className="text-right font-mono tabular-nums">
                              {formatPercent(rate)}
                              {i > 0 && delta !== null ? (
                                <span className={delta < 0 ? "text-bad" : delta > 0 ? "text-good" : "text-muted"}>
                                  {" "}
                                  ({delta > 0 ? "+" : ""}
                                  {Math.round(delta * 100)} pt)
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
            </Panel>
          </div>
        ) : null}
      </Gate>
    </>
  );
}

function MetricRow({ label, cells }: { label: string; cells: string[] }) {
  return (
    <tr>
      <Td className="text-muted">{label}</Td>
      {cells.map((v, i) => (
        <Td key={i} className="text-right font-mono tabular-nums">
          {v}
        </Td>
      ))}
    </tr>
  );
}
