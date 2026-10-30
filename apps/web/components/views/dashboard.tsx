"use client";

import { useRouter } from "next/navigation";
import { useState, type ReactNode } from "react";
import { Bar, BarChart, CartesianGrid, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { AXIS_LABEL, AXIS_STROKE, AXIS_TICK, ChartLegend, GRID_STROKE, TableView, TooltipBox } from "@/components/charts";
import {
  ButtonLink,
  Empty,
  ErrorState,
  Field,
  Gate,
  IdLink,
  Loading,
  Meter,
  Notice,
  Num,
  PageHeader,
  ProviderMark,
  Readouts,
  RunStatusBadge,
  Section,
  Select,
  Stat,
  Table,
  Td,
  Th,
  Timestamp,
} from "@/components/ui";
import { ApiError } from "@/lib/api/client";
import { RUN_STATUSES, type ActivityBucket, type ActivityEvaluation, type StatsActivity, type StatsOverview } from "@/lib/api/types";
import { formatAge, formatCompact, formatCost, formatDuration, formatInt, formatPercent, formatTimestamp } from "@/lib/format";
import { useActivity, useCapabilities, useOverview } from "@/lib/hooks";
import { RUN_STATUS_COLOR } from "@/lib/status";

const WINDOWS = [
  { hours: 1, label: "Last hour" },
  { hours: 24, label: "Last 24 hours" },
  { hours: 168, label: "Last 7 days" },
  { hours: 720, label: "Last 30 days" },
];

/** "6 of 8 finished runs", or a plain note when there is nothing to divide by. */
function ofHint(n: number, d: number, noun: string) {
  return d ? `${formatInt(n)} of ${formatInt(d)} ${noun}` : `no ${noun} yet`;
}

/** Real and simulated spend from usage.by_provider; null when the API does not split it. */
export function splitCost(usage: StatsOverview["usage"]) {
  if (!usage.by_provider) return null;
  const out = { real: 0, simulated: 0, realAttempts: 0, simulatedAttempts: 0 };
  for (const p of Object.values(usage.by_provider)) {
    const cost = Number(p.cost_usd) || 0;
    if (p.simulated) {
      out.simulated += cost;
      out.simulatedAttempts += p.attempts;
    } else {
      out.real += cost;
      out.realAttempts += p.attempts;
    }
  }
  return out;
}

export function DashboardView() {
  const [windowH, setWindowH] = useState(24);
  const query = useOverview(windowH);
  const caps = useCapabilities();
  const o = query.data;
  const windowLabel = WINDOWS.find((w) => w.hours === windowH)?.label.toLowerCase() ?? `last ${windowH} h`;

  return (
    <>
      <PageHeader
        eyebrow="Overview"
        title="Dashboard"
        subtitle="How the fleet has been doing. Rates, latency and cost cover runs that finished in the window you pick; queue and worker counts are as of now, refreshed every ten seconds."
        actions={
          <>
            <Field label="Window">
              <Select value={windowH} onChange={(e) => setWindowH(Number(e.target.value))}>
                {WINDOWS.map((w) => (
                  <option key={w.hours} value={w.hours}>
                    {w.label}
                  </option>
                ))}
              </Select>
            </Field>
            {caps.canLaunch ? (
              <ButtonLink href="/evaluations/new" variant="primary">
                New evaluation
              </ButtonLink>
            ) : null}
          </>
        }
      />
      <Gate query={query}>
        {o ? (
          <div className={`space-y-10 ${query.isPlaceholderData ? "opacity-60" : ""}`}>
            <WindowKpis o={o} windowLabel={windowLabel} />
            <NowKpis o={o} />
            <ActivitySection windowH={windowH} windowLabel={windowLabel} />

            <Section title="Runs by status" note={`${formatInt(o.runs.total)} runs, all time`}>
              <StatusStrip byStatus={o.runs.by_status} total={o.runs.total} />
              <div className="mt-4 max-w-2xl">
                <Table caption="Runs by status">
                  <thead>
                    <tr>
                      <Th>Status</Th>
                      <Th className="text-right">Runs</Th>
                      <Th className="w-1/2">
                        <span className="sr-only">Share</span>
                      </Th>
                    </tr>
                  </thead>
                  <tbody>
                    {RUN_STATUSES.map((s) => {
                      const n = o.runs.by_status[s] ?? 0;
                      const share = o.runs.total ? n / o.runs.total : 0;
                      return (
                        <tr key={s}>
                          <Td>
                            <RunStatusBadge status={s} />
                          </Td>
                          <Td className="text-right font-mono tabular-nums">{formatInt(n)}</Td>
                          <Td>
                            <div className="flex items-center gap-3">
                              <div className="relative h-[3px] flex-1 bg-sunken" aria-hidden="true">
                                <div className="absolute inset-y-0 left-0" style={{ width: `${share * 100}%`, background: RUN_STATUS_COLOR[s] }} />
                              </div>
                              <span className="w-12 text-right font-mono text-[11.5px] text-faint tabular-nums">{formatPercent(share)}</span>
                            </div>
                          </Td>
                        </tr>
                      );
                    })}
                  </tbody>
                </Table>
              </div>
            </Section>
          </div>
        ) : null}
      </Gate>
    </>
  );
}

function WindowKpis({ o, windowLabel }: { o: StatsOverview; windowLabel: string }) {
  const r = o.rates;
  const lat = o.run_latency_ms;
  const cost = splitCost(o.usage);
  return (
    <Section title={`In the ${windowLabel}`}>
      <div className="space-y-px">
        <Readouts>
          <Stat
            label="Success rate"
            value={formatPercent(o.success.success_rate)}
            meter={<Meter value={o.success.success_rate} tone="good" />}
            hint={`${o.success.passed} of ${o.success.graded} graded attempts passed`}
          />
          {r ? (
            <>
              <Stat label="Completion rate" value={formatPercent(r.completion_rate)} hint={ofHint(r.completed, r.finished, "finished runs completed")} />
              <Stat label="Failure rate" value={formatPercent(r.failure_rate)} hint={ofHint(r.failed, r.finished, "finished runs failed")} />
              <Stat
                label="Timeouts / retries"
                value={`${formatPercent(r.timeout_rate)} / ${formatPercent(r.retry_rate)}`}
                hint={r.finished ? `${r.timed_out} timed out, ${r.retried} needed a retry, of ${r.finished}` : "no finished runs yet"}
              />
            </>
          ) : (
            <>
              <Stat label="Latency p50" value={formatDuration(lat.p50)} hint={`over ${lat.count} finished runs`} />
              <Stat label="Latency p95" value={formatDuration(lat.p95)} hint="the slow tail" />
              <Stat label="Total cost" value={formatCost(o.usage.cost_usd)} hint={`${o.usage.attempts} attempts`} />
            </>
          )}
        </Readouts>
        {r ? (
          <Readouts>
            <Stat label="Latency p50" value={formatDuration(lat.p50)} hint={`n = ${formatInt(lat.count)} finished runs`} />
            <Stat label="Latency p95" value={formatDuration(lat.p95)} hint="nearest rank" />
            <Stat label="Latency p99" value={formatDuration(lat.p99)} hint="nearest rank" />
            <Stat label="Latency mean" value={formatDuration(lat.mean)} hint="pulled up by the slow tail" />
          </Readouts>
        ) : null}
        <Readouts cols={cost ? 4 : r ? 3 : 2}>
          {cost ? (
            <>
              <Stat label="Real cost" value={formatCost(cost.real)} hint={`${formatInt(cost.realAttempts)} attempts on real providers`} />
              <Stat
                label="Simulated cost"
                value={formatCost(cost.simulated)}
                hint={`${formatInt(cost.simulatedAttempts)} fake-model attempts; priced for comparison, nothing was spent`}
              />
            </>
          ) : r ? (
            <Stat label="Total cost" value={formatCost(o.usage.cost_usd)} hint={`${o.usage.attempts} attempts; this API does not split real from simulated`} />
          ) : null}
          <Stat
            label="Tokens in / out"
            value={`${formatCompact(o.usage.tokens_in)} / ${formatCompact(o.usage.tokens_out)}`}
            hint={`${formatInt(o.usage.tokens_in)} in, ${formatInt(o.usage.tokens_out)} out`}
          />
          <Stat label="Attempts" value={formatInt(o.usage.attempts)} hint="including retries" />
        </Readouts>
      </div>
    </Section>
  );
}

function NowKpis({ o }: { o: StatsOverview }) {
  const oldest = o.queue.oldest_queued_s;
  const workers = o.workers;
  return (
    <Section title="Right now">
      <Readouts>
        <Stat
          label="Queue depth"
          value={formatInt(o.queue.total)}
          hint={`${o.queue.runs_queued} queued, ${o.queue.runs_retrying} retrying, ${o.queue.evaluations_pending} grading`}
        />
        <Stat
          label="Oldest queued run"
          value={oldest === undefined ? "–" : oldest === null ? "none" : formatAge(oldest)}
          hint={oldest === undefined ? "not reported by this API" : oldest === null ? "nothing is waiting" : "time since it was queued"}
        />
        <Stat
          label="Workers alive"
          value={formatInt(workers?.alive ?? o.active_workers)}
          hint={
            workers
              ? workers.stale
                ? `${workers.stale} stale: not stopped, but no recent heartbeat`
                : "none stale"
              : o.active_workers
                ? "with a recent heartbeat"
                : "nothing is picking up work"
          }
        />
        <Stat label="Evaluations" value={formatInt(o.evaluations.total)} hint={`${o.evaluations.cancel_requested} cancelled`} />
      </Readouts>
    </Section>
  );
}

// ---- activity ---------------------------------------------------------------------

/** UTC tick for a bucket start, as coarse as the bucket. */
export function bucketLabel(iso: string, bucketS: number): string {
  const t = formatTimestamp(iso); // "2026-10-17 12:00:00"
  if (t === "–") return t;
  if (bucketS < 21_600) return t.slice(11, 16);
  if (bucketS < 86_400) return `${t.slice(5, 10)} ${t.slice(11, 13)}h`;
  return t.slice(5, 10);
}

const bucketWord = (s: number) => (s === 3600 ? "hour" : s === 21_600 ? "6 hours" : s === 86_400 ? "day" : `${s} s`);

function ActivitySection({ windowH, windowLabel }: { windowH: number; windowLabel: string }) {
  const query = useActivity(windowH);
  const a = query.data;
  const missing = query.isError && query.error instanceof ApiError && query.error.status === 404;

  const body = (render: (data: StatsActivity) => ReactNode) => {
    if (query.isLoading) return <Loading label="Loading activity" />;
    if (missing)
      return (
        <Notice tone="info" title="Not available on this API">
          This server does not have GET /stats/activity yet, so the charts are left out. The figures above still apply.
        </Notice>
      );
    if (query.isError && !a) return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
    return a ? render(a) : null;
  };

  const finished = a ? a.buckets.reduce((n, b) => n + b.finished + b.cancelled, 0) : 0;
  const graded = a ? a.evaluations.reduce((n, e) => n + e.graded, 0) : 0;

  return (
    <div className={`grid gap-x-10 gap-y-10 xl:grid-cols-2 ${query.isPlaceholderData ? "opacity-60" : ""}`}>
      <Section
        title="Finished runs over time"
        note={a && finished ? `${formatInt(finished)} runs, one bar per ${bucketWord(a.bucket_s)}, UTC` : undefined}
      >
        {body((d) =>
          finished === 0 ? (
            <Empty>No runs finished in the {windowLabel}.</Empty>
          ) : (
            <ActivityChart buckets={d.buckets} bucketS={d.bucket_s} />
          ),
        )}
      </Section>
      <Section
        title="Recent evaluations"
        note={a && a.evaluations.length ? `${a.evaluations.length} evaluations, ${formatInt(graded)} graded attempts; click a bar to open one` : undefined}
      >
        {body((d) =>
          d.evaluations.length === 0 ? (
            <Empty>No evaluations were created in the {windowLabel}.</Empty>
          ) : (
            <RecentEvaluationsChart evaluations={d.evaluations} />
          ),
        )}
      </Section>
    </div>
  );
}

const ACTIVITY_SERIES = [
  { key: "completed", label: "completed", color: "var(--chart-1)" },
  { key: "failed", label: "failed", color: "var(--chart-2)" },
  { key: "cancelled", label: "cancelled", color: "var(--tone-muted)" },
] as const;

export function ActivityChart({ buckets, bucketS }: { buckets: ActivityBucket[]; bucketS: number }) {
  const data = buckets.map((b) => ({ ...b, label: bucketLabel(b.start, bucketS) }));
  return (
    <figure aria-label="Finished runs per time bucket, by final status">
      <ChartLegend items={ACTIVITY_SERIES.map((s) => ({ color: s.color, label: s.label }))} />
      <div style={{ height: 220 }} aria-hidden="true">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={data} margin={{ top: 4, right: 8, bottom: 18, left: 4 }} barCategoryGap="18%">
            <CartesianGrid stroke={GRID_STROKE} vertical={false} />
            <XAxis
              dataKey="label"
              stroke={AXIS_STROKE}
              tick={AXIS_TICK}
              tickLine={false}
              minTickGap={18}
              label={{ value: "Finished at (UTC)", position: "insideBottom", offset: -12, style: AXIS_LABEL }}
            />
            <YAxis
              allowDecimals={false}
              width={44}
              stroke={AXIS_STROKE}
              tick={AXIS_TICK}
              tickLine={false}
              label={{ value: "Runs", angle: -90, position: "insideLeft", offset: 12, style: AXIS_LABEL }}
            />
            <Tooltip
              cursor={{ fill: "var(--sunken)" }}
              content={({ active, payload }) => {
                const b = active ? (payload?.[0]?.payload as (ActivityBucket & { label: string }) | undefined) : undefined;
                if (!b) return null;
                return (
                  <TooltipBox
                    title={`${formatTimestamp(b.start)} UTC, ${bucketWord(bucketS)}`}
                    rows={[
                      ...ACTIVITY_SERIES.map((s) => ({ color: s.color, label: s.label, value: formatInt(b[s.key]) })),
                      { label: "passed of graded", value: `${b.passed} / ${b.graded}` },
                      { label: "p95 latency", value: formatDuration(b.p95_ms) },
                    ]}
                  />
                );
              }}
            />
            {ACTIVITY_SERIES.map((s) => (
              <Bar
                key={s.key}
                dataKey={s.key}
                name={s.label}
                stackId="runs"
                fill={s.color}
                stroke="var(--paper)"
                strokeWidth={1}
                maxBarSize={24}
                isAnimationActive={false}
              />
            ))}
          </BarChart>
        </ResponsiveContainer>
      </div>
      <TableView>
        <Table caption="Finished runs per bucket">
          <thead>
            <tr>
              <Th>Bucket start (UTC)</Th>
              <Th className="text-right">Completed</Th>
              <Th className="text-right">Failed</Th>
              <Th className="text-right">Cancelled</Th>
              <Th className="text-right">Passed / graded</Th>
              <Th className="text-right">p95</Th>
            </tr>
          </thead>
          <tbody>
            {buckets.map((b) => (
              <tr key={b.start}>
                <Td>
                  <Timestamp value={formatTimestamp(b.start)} />
                </Td>
                <Num>{b.completed}</Num>
                <Num>{b.failed}</Num>
                <Num>{b.cancelled}</Num>
                <Num>
                  {b.passed} / {b.graded}
                </Num>
                <Num>{formatDuration(b.p95_ms)}</Num>
              </tr>
            ))}
          </tbody>
        </Table>
      </TableView>
    </figure>
  );
}

const SIM_COLOR = "var(--chart-1)";
const REAL_COLOR = "var(--chart-2)";

export function RecentEvaluationsChart({ evaluations }: { evaluations: ActivityEvaluation[] }) {
  const router = useRouter();
  // The API lists newest first; the chart reads left to right in time.
  const data = [...evaluations].reverse().map((e) => ({ ...e, rate: e.success_rate }));
  const open = (item: unknown) => {
    const id = (item as { payload?: { id?: string } } | undefined)?.payload?.id;
    if (id) router.push(`/evaluations/${id}`);
  };
  return (
    <figure aria-label="Success rate of each recent evaluation">
      <ChartLegend
        items={[
          { color: SIM_COLOR, label: "simulated (fake model)" },
          { color: REAL_COLOR, label: "real model" },
        ]}
      />
      <div style={{ height: 220 }} aria-hidden="true">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={data} margin={{ top: 4, right: 8, bottom: 18, left: 4 }} barCategoryGap="18%">
            <CartesianGrid stroke={GRID_STROKE} vertical={false} />
            <XAxis
              dataKey="id"
              tick={false}
              stroke={AXIS_STROKE}
              tickLine={false}
              label={{ value: "Evaluations, oldest to newest", position: "insideBottom", offset: -4, style: AXIS_LABEL }}
            />
            <YAxis
              domain={[0, 1]}
              ticks={[0, 0.25, 0.5, 0.75, 1]}
              tickFormatter={(v: number) => `${Math.round(v * 100)}%`}
              width={44}
              stroke={AXIS_STROKE}
              tick={AXIS_TICK}
              tickLine={false}
              label={{ value: "Success", angle: -90, position: "insideLeft", offset: 12, style: AXIS_LABEL }}
            />
            <Tooltip
              cursor={{ fill: "var(--sunken)" }}
              content={({ active, payload }) => {
                const e = active ? (payload?.[0]?.payload as ActivityEvaluation | undefined) : undefined;
                if (!e) return null;
                return (
                  <TooltipBox
                    title={`${e.name} · ${e.agent_name} v${e.agent_version}`}
                    rows={[
                      { color: e.simulated ? SIM_COLOR : REAL_COLOR, label: "success", value: formatPercent(e.success_rate) },
                      { label: "passed / graded", value: `${e.passed} / ${e.graded}` },
                      { label: "runs", value: formatInt(e.runs_total) },
                      { label: "p95 latency", value: formatDuration(e.p95_ms) },
                    ]}
                    footer={`${e.provider}/${e.model}${e.simulated ? ", simulated" : ""} · ${formatTimestamp(e.created_at)}`}
                  />
                );
              }}
            />
            <Bar dataKey="rate" name="success" maxBarSize={24} radius={[4, 4, 0, 0]} isAnimationActive={false} onClick={open} cursor="pointer">
              {data.map((e) => (
                <Cell key={e.id} fill={e.simulated ? SIM_COLOR : REAL_COLOR} />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
      <TableView>
        <Table caption="Recent evaluations">
          <thead>
            <tr>
              <Th>Evaluation</Th>
              <Th>Agent</Th>
              <Th>Model</Th>
              <Th className="text-right">Success</Th>
              <Th className="text-right">Passed / graded</Th>
              <Th className="text-right">p95</Th>
            </tr>
          </thead>
          <tbody>
            {evaluations.map((e) => (
              <tr key={e.id}>
                <Td>
                  <IdLink href={`/evaluations/${e.id}`} id={e.id} label={e.name} />
                </Td>
                <Td className="whitespace-nowrap">
                  {e.agent_name} v{e.agent_version}
                </Td>
                <Td className="whitespace-nowrap">
                  <span className="font-mono text-[12px]">{e.model}</span> <ProviderMark provider={e.provider} simulated={e.simulated} />
                </Td>
                <Num>{formatPercent(e.success_rate)}</Num>
                <Num>
                  {e.passed} / {e.graded}
                </Num>
                <Num>{formatDuration(e.p95_ms)}</Num>
              </tr>
            ))}
          </tbody>
        </Table>
      </TableView>
    </figure>
  );
}

/** One thin bar split by run status, in status order. */
export function StatusStrip({ byStatus, total, height = 6 }: { byStatus: Record<string, number>; total: number; height?: number }) {
  return (
    <div className="flex w-full gap-px bg-sunken" style={{ height }} aria-hidden="true">
      {total
        ? RUN_STATUSES.map((s) => {
            const n = byStatus[s] ?? 0;
            return n ? <div key={s} title={`${s}: ${n}`} style={{ flexGrow: n, background: RUN_STATUS_COLOR[s] }} /> : null;
          })
        : null}
    </div>
  );
}
