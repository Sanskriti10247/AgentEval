"use client";

import { useState } from "react";
import { Bar, BarChart, CartesianGrid, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Field, Gate, PageHeader, Panel, RunStatusBadge, Select, Stat, Table, Td, Th } from "@/components/ui";
import { RUN_STATUSES } from "@/lib/api/types";
import { formatCost, formatDuration, formatInt, formatPercent } from "@/lib/format";
import { useOverview } from "@/lib/hooks";
import { RUN_STATUS_COLOR } from "@/lib/status";

const WINDOWS = [
  { hours: 24, label: "Last 24 hours" },
  { hours: 168, label: "Last 7 days" },
  { hours: 720, label: "Last 30 days" },
];

export function DashboardView() {
  const [windowH, setWindowH] = useState(24);
  const query = useOverview(windowH);
  const o = query.data;
  const windowLabel = WINDOWS.find((w) => w.hours === windowH)?.label.toLowerCase() ?? `last ${windowH} h`;

  return (
    <>
      <PageHeader
        title="Dashboard"
        subtitle="Success, latency and cost cover the selected window; counts and queue depth are current."
        actions={
          <Field label="Window">
            <Select value={windowH} onChange={(e) => setWindowH(Number(e.target.value))}>
              {WINDOWS.map((w) => (
                <option key={w.hours} value={w.hours}>
                  {w.label}
                </option>
              ))}
            </Select>
          </Field>
        }
      />
      <Gate query={query}>
        {o ? (
          <div className="space-y-4">
            <dl className="grid grid-cols-2 gap-3 md:grid-cols-4">
              <Stat label="Evaluations" value={formatInt(o.evaluations.total)} hint={`${o.evaluations.cancel_requested} cancelled`} />
              <Stat
                label="Success rate"
                value={formatPercent(o.success.success_rate)}
                hint={`${o.success.passed} of ${o.success.graded} graded, ${windowLabel}`}
              />
              <Stat label="Latency p50" value={formatDuration(o.run_latency_ms.p50)} hint={`${o.run_latency_ms.count} runs`} />
              <Stat label="Latency p95" value={formatDuration(o.run_latency_ms.p95)} />
              <Stat label="Total cost" value={formatCost(o.usage.cost_usd)} hint={`${o.usage.attempts} attempts, ${windowLabel}`} />
              <Stat
                label="Tokens in / out"
                value={`${formatInt(o.usage.tokens_in)} / ${formatInt(o.usage.tokens_out)}`}
              />
              <Stat label="Active workers" value={formatInt(o.active_workers)} />
              <Stat
                label="Queue depth"
                value={formatInt(o.queue.total)}
                hint={`${o.queue.runs_queued} queued, ${o.queue.runs_retrying} retrying, ${o.queue.evaluations_pending} grading`}
              />
            </dl>
            <Panel title={`Runs by status (${formatInt(o.runs.total)} total, all time)`}>
              <div className="grid gap-4 lg:grid-cols-[2fr_1fr]">
                <div className="h-56" aria-hidden="true">
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={RUN_STATUSES.map((s) => ({ status: s, count: o.runs.by_status[s] ?? 0 }))}>
                      <CartesianGrid stroke="var(--line)" vertical={false} />
                      <XAxis dataKey="status" stroke="var(--muted)" tick={{ fontSize: 11 }} />
                      <YAxis allowDecimals={false} stroke="var(--muted)" tick={{ fontSize: 11 }} />
                      <Tooltip
                        cursor={{ fill: "var(--panel-2)" }}
                        contentStyle={{ background: "var(--panel)", border: "1px solid var(--line)", fontSize: 12 }}
                      />
                      <Bar dataKey="count" isAnimationActive={false}>
                        {RUN_STATUSES.map((s) => (
                          <Cell key={s} fill={RUN_STATUS_COLOR[s]} />
                        ))}
                      </Bar>
                    </BarChart>
                  </ResponsiveContainer>
                </div>
                <Table caption="Runs by status">
                  <thead>
                    <tr>
                      <Th>Status</Th>
                      <Th className="text-right">Runs</Th>
                    </tr>
                  </thead>
                  <tbody>
                    {RUN_STATUSES.map((s) => (
                      <tr key={s}>
                        <Td>
                          <RunStatusBadge status={s} />
                        </Td>
                        <Td className="text-right font-mono tabular-nums">{formatInt(o.runs.by_status[s] ?? 0)}</Td>
                      </tr>
                    ))}
                  </tbody>
                </Table>
              </div>
            </Panel>
          </div>
        ) : null}
      </Gate>
    </>
  );
}
