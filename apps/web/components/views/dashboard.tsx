"use client";

import { useState } from "react";
import { ButtonLink, Field, Gate, Meter, PageHeader, Readouts, RunStatusBadge, Section, Select, Stat, Table, Td, Th } from "@/components/ui";
import { RUN_STATUSES } from "@/lib/api/types";
import { formatCompact, formatCost, formatDuration, formatInt, formatPercent } from "@/lib/format";
import { useCapabilities, useOverview } from "@/lib/hooks";
import { RUN_STATUS_COLOR } from "@/lib/status";

const WINDOWS = [
  { hours: 24, label: "Last 24 hours" },
  { hours: 168, label: "Last 7 days" },
  { hours: 720, label: "Last 30 days" },
];

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
        subtitle="How the fleet has been doing. Success, latency and cost cover the window you pick; queue and worker counts are as of now, refreshed every ten seconds."
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
          <div className="space-y-10">
            <Section title={`In the ${windowLabel}`}>
              <Readouts>
                <Stat
                  label="Success rate"
                  value={formatPercent(o.success.success_rate)}
                  meter={<Meter value={o.success.success_rate} tone="good" />}
                  hint={`${o.success.passed} of ${o.success.graded} graded attempts passed`}
                />
                <Stat label="Latency p50" value={formatDuration(o.run_latency_ms.p50)} hint={`over ${o.run_latency_ms.count} finished runs`} />
                <Stat label="Latency p95" value={formatDuration(o.run_latency_ms.p95)} hint="the slow tail" />
                <Stat label="Total cost" value={formatCost(o.usage.cost_usd)} hint={`${o.usage.attempts} attempts`} />
              </Readouts>
            </Section>

            <Section title="Right now">
              <Readouts>
                <Stat
                  label="Queue depth"
                  value={formatInt(o.queue.total)}
                  hint={`${o.queue.runs_queued} queued, ${o.queue.runs_retrying} retrying, ${o.queue.evaluations_pending} grading`}
                />
                <Stat label="Active workers" value={formatInt(o.active_workers)} hint={o.active_workers ? "with a recent heartbeat" : "nothing is picking up work"} />
                <Stat label="Evaluations" value={formatInt(o.evaluations.total)} hint={`${o.evaluations.cancel_requested} cancelled`} />
                <Stat
                  label="Tokens in / out"
                  value={`${formatCompact(o.usage.tokens_in)} / ${formatCompact(o.usage.tokens_out)}`}
                  hint={`${formatInt(o.usage.tokens_in)} in, ${formatInt(o.usage.tokens_out)} out`}
                />
              </Readouts>
            </Section>

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
