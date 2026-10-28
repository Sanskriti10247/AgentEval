"use client";

import Link from "next/link";
import { useState, type ReactNode } from "react";
import {
  Button,
  CategoryBadge,
  Empty,
  EvaluationStatusBadge,
  Gate,
  Glyph,
  IdLink,
  KeyValue,
  Live,
  LoadMore,
  Meter,
  Mono,
  Notice,
  Num,
  PageHeader,
  RunStatusBadge,
  Section,
  StatusMark,
  Table,
  Tag,
  Td,
  Th,
  Timestamp,
} from "@/components/ui";
import { flatten } from "@/components/pages-util";
import { describeWriteError } from "@/components/launch/write-error";
import { TERMINAL_RUN_STATUSES, type AttemptEvaluationRead, type RunAttemptRead, type TrajectoryEventRead } from "@/lib/api/types";
import { compactJson, formatCost, formatDuration, formatInt, formatScore, formatTimestamp, humanize, shortId } from "@/lib/format";
import {
  useAttemptEvaluation,
  useAttemptEvents,
  useAttempts,
  useCancelRun,
  useCapabilities,
  useReplayRun,
  useRun,
  useTask,
  useVersionIndex,
} from "@/lib/hooks";
import type { Tone } from "@/lib/status";

function isErrorEvent(e: TrajectoryEventRead) {
  return !!e.error_class || e.status === "error" || e.status === "timeout" || e.event_type.includes("error");
}

const ATTEMPT_TONE: Record<string, Tone> = { FINISHED: "good", RUNNING: "active", ABANDONED: "bad", CANCELLED: "muted" };

function WriteError({ error }: { error: unknown }) {
  const { title, body } = describeWriteError(error);
  return <Notice title={title}>{body}</Notice>;
}

export function RunDetailView({ id }: { id: string }) {
  const run = useRun(id);
  const r = run.data;
  const live = !!r && !TERMINAL_RUN_STATUSES.includes(r.status);
  const attempts = useAttempts(id, live);
  const cancel = useCancelRun(id);
  const replay = useReplayRun(id);
  const caps = useCapabilities();
  const task = useTask(r?.task_id);
  const { index } = useVersionIndex();
  const [extra, setExtra] = useState(1);
  const [picked, setPicked] = useState<number | null>(null);

  const list = attempts.data ?? [];
  const selectedNo = picked ?? (list.length ? Math.max(...list.map((a) => a.attempt_no)) : null);
  const selected = list.find((a) => a.attempt_no === selectedNo);
  const canCancel = !!r && !r.cancel_requested_at && ["QUEUED", "CLAIMED", "RUNNING", "RETRYING"].includes(r.status);
  const canReplay = r?.status === "FAILED";
  const version = r?.agent_version_id ? index.get(r.agent_version_id) : undefined;
  const runDuration =
    r?.started_at && r.finished_at ? new Date(r.finished_at).getTime() - new Date(r.started_at).getTime() : null;

  return (
    <>
      <PageHeader
        eyebrow={
          <>
            <Link href="/runs" className="no-underline hover:text-ink">
              Runs
            </Link>{" "}
            / {shortId(id)}
          </>
        }
        title={task.data?.name ?? "Run"}
        subtitle={
          r ? (
            <>
              {version ? (
                <>
                  <Link href={`/agents/${version.agentId}`}>{version.agentName}</Link> v{version.version}
                </>
              ) : r.agent_version_id ? (
                <Mono>{shortId(r.agent_version_id)}</Mono>
              ) : (
                "A command task"
              )}
              {r.evaluation_id ? (
                <>
                  , part of evaluation <IdLink id={r.evaluation_id} href={`/evaluations/${r.evaluation_id}`} />
                  {r.repetition !== null ? <> (repetition {r.repetition})</> : null}
                </>
              ) : (
                ", run on its own"
              )}
              .
            </>
          ) : (
            <span className="font-mono text-[12px]">{id}</span>
          )
        }
        actions={
          r ? (
            <>
              {live ? <Live /> : null}
              <RunStatusBadge status={r.status} />
              {caps.readOnly ? null : (
                <>
                  <Button variant="danger" disabled={!canCancel || cancel.isPending} onClick={() => cancel.mutate()}>
                    {cancel.isPending ? "Cancelling…" : "Cancel"}
                  </Button>
                  <div className="inline-flex">
                    <label className="inline-flex h-[30px] items-center gap-1 rounded-l-[3px] border border-r-0 border-rule-strong bg-surface pl-2 font-mono text-[11.5px] text-faint">
                      +
                      <input
                        type="number"
                        min={1}
                        max={9}
                        value={extra}
                        aria-label="Additional attempts"
                        title="How many more attempts the replay gets"
                        onChange={(e) => setExtra(Math.min(9, Math.max(1, Number(e.target.value) || 1)))}
                        className="w-9 bg-transparent font-mono text-[12.5px] text-ink outline-none"
                      />
                    </label>
                    <Button className="rounded-l-none" disabled={!canReplay || replay.isPending} onClick={() => replay.mutate(extra)}>
                      {replay.isPending ? "Replaying…" : "Replay"}
                    </Button>
                  </div>
                </>
              )}
            </>
          ) : null
        }
      />
      <Gate query={run}>
        {r ? (
          <div className="space-y-10">
            {cancel.isError ? <WriteError error={cancel.error} /> : null}
            {replay.isError ? <WriteError error={replay.error} /> : null}

            <Section title="Run">
              <KeyValue
                columns={2}
                items={[
                  ["Status", <RunStatusBadge key="s" status={r.status} />],
                  ["Outcome", <Mono key="o" className="text-[12.5px]">{r.outcome ?? "–"}</Mono>],
                  [
                    "Attempts",
                    <Mono key="b" className="text-[12.5px]">
                      {r.attempt_count} used of {r.max_attempts}
                      {r.replay_count ? `, replayed ${r.replay_count}×` : ""}
                    </Mono>,
                  ],
                  ["Wall time", <Mono key="d" className="text-[12.5px]">{formatDuration(runDuration)}</Mono>],
                  ["Dead letter", r.dead_letter_reason ? <StatusMark key="dl" tone="bad">{humanize(r.dead_letter_reason)}</StatusMark> : "–"],
                  ["Failure reason", humanize(r.failure_reason)],
                  ...(r.error_message ? ([["Error", <span key="e" className="text-bad">{r.error_message}</span>]] as [string, ReactNode][]) : []),
                  ...(r.next_attempt_at ? ([["Next attempt", <Timestamp key="n" value={formatTimestamp(r.next_attempt_at)} />]] as [string, ReactNode][]) : []),
                  ...(r.cancel_requested_at
                    ? ([
                        [
                          "Cancel requested",
                          <span key="c">
                            <Timestamp value={formatTimestamp(r.cancel_requested_at)} /> <span className="text-faint">({r.cancel_reason})</span>
                          </span>,
                        ],
                      ] as [string, ReactNode][])
                    : []),
                  ["Run id", <Mono key="id" className="break-all text-[12px] text-ink-2">{r.id}</Mono>],
                  ["Task", <IdLink key="t" id={r.task_id} label={task.data ? `${task.data.name} · ${shortId(r.task_id)}` : r.task_id} />],
                  ["Worker", <Mono key="w" className="text-[12.5px]">{r.worker_id ?? "–"}</Mono>],
                  ["Created", <Timestamp key="cr" value={formatTimestamp(r.created_at)} />],
                  ["Started", <Timestamp key="st" value={formatTimestamp(r.started_at)} />],
                  ["Finished", <Timestamp key="f" value={formatTimestamp(r.finished_at)} />],
                ]}
              />
            </Section>

            <Section title="Attempts" note={list.length > 1 ? "pick one to see its trajectory" : undefined}>
              <Gate query={attempts} loadingLabel="Loading attempts">
                {list.length === 0 ? (
                  <Empty>No attempts yet. {live ? "The run is waiting for a worker to pick it up; this page refreshes on its own." : ""}</Empty>
                ) : (
                  <ol aria-label="Attempt timeline" className="border-t border-rule">
                    {list.map((a) => (
                      <AttemptRow key={a.id} a={a} selected={a.attempt_no === selectedNo} onSelect={() => setPicked(a.attempt_no)} />
                    ))}
                  </ol>
                )}
              </Gate>
            </Section>

            {selectedNo !== null ? (
              <div className="grid gap-10 xl:grid-cols-[minmax(0,1fr)_300px]">
                <EventsPanel runId={id} attempt={selected} attemptNo={selectedNo} live={live} />
                <div className="space-y-10">
                  <EvaluationPanel runId={id} attemptNo={selectedNo} />
                  <AttemptDetail attempt={selected} />
                </div>
              </div>
            ) : null}
          </div>
        ) : null}
      </Gate>
    </>
  );
}

function AttemptRow({ a, selected, onSelect }: { a: RunAttemptRead; selected: boolean; onSelect: () => void }) {
  return (
    <li className="border-b border-rule">
      <button
        type="button"
        onClick={onSelect}
        aria-pressed={selected}
        className={`relative grid w-full grid-cols-[2.5rem_1fr] items-baseline gap-x-3 gap-y-1 py-2 pl-3 pr-1 text-left text-[13px] sm:grid-cols-[2.5rem_9rem_1fr_auto] ${
          selected ? "bg-sunken/70" : "hover:bg-sunken/40"
        }`}
      >
        <span aria-hidden="true" className={`absolute inset-y-0 left-0 w-[2px] ${selected ? "bg-accent" : ""}`} />
        <Mono className={selected ? "text-ink" : "text-muted"}>#{a.attempt_no}</Mono>
        <StatusMark tone={ATTEMPT_TONE[a.status] ?? "neutral"}>{a.status}</StatusMark>
        <span className="col-start-2 font-mono text-[11.5px] text-muted sm:col-start-auto">
          {a.outcome ?? "–"}
          <span className="text-faint">
            {" "}
            · {formatDuration(a.duration_ms)} · {a.steps ?? "–"} steps · {formatInt(a.tokens_in)}/{formatInt(a.tokens_out)} tok · {formatCost(a.cost_usd)}
          </span>
        </span>
        <span className="col-start-2 sm:col-start-auto">
          <Timestamp value={formatTimestamp(a.started_at)} />
        </span>
        {a.error_message ? <span className="col-start-2 min-w-0 truncate text-[12.5px] text-bad sm:col-span-3">{a.error_message}</span> : null}
      </button>
    </li>
  );
}

function AttemptDetail({ attempt }: { attempt: RunAttemptRead | undefined }) {
  if (!attempt) return null;
  return (
    <Section title={`Output · attempt #${attempt.attempt_no}`}>
      <KeyValue
        items={[
          ["Exit code", <Mono key="x" className="text-[12.5px]">{attempt.exit_code ?? "–"}{attempt.oom_killed ? " (OOM killed)" : ""}</Mono>],
          ["Worker", <Mono key="w" className="text-[12.5px]">{attempt.worker_id}</Mono>],
          ["Image", <Mono key="i" className="break-all text-[12px]">{attempt.sandbox_image_id ? attempt.sandbox_image_id.slice(0, 19) : "–"}</Mono>],
        ]}
      />
      {attempt.final_answer ? (
        <div className="mt-4">
          <div className="mb-1 font-mono text-[10.5px] uppercase tracking-[0.1em] text-faint">Final answer</div>
          <pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded-[3px] bg-sunken p-3 font-mono text-[12px] leading-relaxed text-ink">{attempt.final_answer}</pre>
        </div>
      ) : null}
      {(["stdout", "stderr"] as const).map((k) =>
        attempt[k] ? (
          <details key={k} className="mt-3">
            <summary className="cursor-pointer font-mono text-[11.5px] text-muted hover:text-ink">
              ▸ {k}
              {attempt[`${k}_truncated`] ? " (truncated)" : ""}
            </summary>
            <pre className="mt-1 max-h-64 overflow-auto rounded-[3px] bg-sunken p-3 font-mono text-[11.5px] text-ink-2">{attempt[k]}</pre>
          </details>
        ) : null,
      )}
    </Section>
  );
}

const SCORE_PARTS = [
  ["task_success", "Task success"],
  ["test_score", "Tests"],
  ["reliability", "Reliability"],
  ["efficiency", "Efficiency"],
] as const;

function EvaluationPanel({ runId, attemptNo }: { runId: string; attemptNo: number }) {
  const query = useAttemptEvaluation(runId, attemptNo);
  const ev: AttemptEvaluationRead | null | undefined = query.data;
  return (
    <Section title={`Grade · attempt #${attemptNo}`}>
      <Gate query={query} loadingLabel="Loading evaluation">
        {!ev ? (
          <Empty>No evaluation recorded for this attempt. Command tasks are not graded, and an attempt that never started has nothing to grade.</Empty>
        ) : (
          <div className="space-y-5">
            <div className="flex flex-wrap items-baseline gap-x-4 gap-y-2">
              {ev.task_success === null ? null : (
                <span className={`font-display text-[22px] font-semibold leading-none ${ev.task_success ? "text-good" : "text-bad"}`}>
                  {ev.task_success ? "Passed" : "Failed"}
                </span>
              )}
              <CategoryBadge category={ev.failure_category} />
              <EvaluationStatusBadge status={ev.status} />
            </div>
            {ev.tags.length ? (
              <div className="flex flex-wrap gap-1">
                {ev.tags.map((t) => (
                  <Tag key={t}>{t}</Tag>
                ))}
              </div>
            ) : null}
            {ev.score ? (
              <Table caption="Score components">
                <thead>
                  <tr>
                    <Th>Component</Th>
                    <Th className="text-right">Score</Th>
                    <Th className="w-[40%]">
                      <span className="sr-only">Bar</span>
                    </Th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <Td className="font-medium">Overall</Td>
                    <Num className="font-medium text-ink">{formatScore(ev.score.overall)}</Num>
                    <Td>
                      <Meter value={Number(ev.score.overall)} tone="accent" />
                    </Td>
                  </tr>
                  {SCORE_PARTS.map(([key, label]) => {
                    const v = ev.score?.[key] ?? null;
                    return (
                      <tr key={key}>
                        <Td className="text-muted">{label}</Td>
                        <Num className="text-ink-2">{formatScore(v)}</Num>
                        <Td>
                          <Meter value={v === null ? null : Number(v)} />
                        </Td>
                      </tr>
                    );
                  })}
                </tbody>
              </Table>
            ) : null}
            {ev.score?.integrity_gate_applied ? (
              <Notice tone="warn" title="Integrity gate applied">
                The grader found signs of tampering, so the overall score was forced down.
              </Notice>
            ) : null}
            {ev.checks.length ? (
              <Table caption="Checks">
                <thead>
                  <tr>
                    <Th>Check</Th>
                    <Th>Result</Th>
                    <Th className="text-right">Score</Th>
                    <Th className="text-right">Time</Th>
                  </tr>
                </thead>
                <tbody>
                  {ev.checks.map((c) => (
                    <tr key={c.position}>
                      <Td className="font-mono text-[12px] text-muted">#{c.position}</Td>
                      <Td>
                        <StatusMark tone={c.passed === null ? "muted" : c.passed ? "good" : "bad"}>
                          {c.passed === null ? "not run" : c.passed ? "pass" : "fail"}
                        </StatusMark>
                      </Td>
                      <Num>{c.score ?? "–"}</Num>
                      <Num>{formatDuration(c.duration_ms)}</Num>
                    </tr>
                  ))}
                </tbody>
              </Table>
            ) : null}
            {ev.metrics ? (
              <dl className="grid gap-x-6 font-mono text-[11.5px] sm:grid-cols-2 xl:grid-cols-1">
                <Metric k="model calls" v={`${ev.metrics.model_calls}${ev.metrics.model_errors ? ` (${ev.metrics.model_errors} err)` : ""}`} />
                <Metric k="tool calls" v={ev.metrics.tool_calls} />
                <Metric k="tool error rate" v={ev.metrics.tool_error_rate === null ? "–" : `${Math.round(ev.metrics.tool_error_rate * 100)}%`} />
                <Metric k="loop" v={ev.metrics.loop_detected ? "detected" : "no"} />
                <Metric k="ran tests" v={ev.metrics.ran_tests ? (ev.metrics.last_tests_passed ? "yes, passing" : "yes") : "no"} />
                {ev.metrics.limits_reached.length ? <Metric k="limits hit" v={ev.metrics.limits_reached.join(", ")} /> : null}
              </dl>
            ) : null}
          </div>
        )}
      </Gate>
    </Section>
  );
}

function Metric({ k, v }: { k: string; v: ReactNode }) {
  return (
    <div className="flex justify-between gap-2 border-b border-rule py-1">
      <dt className="text-faint">{k}</dt>
      <dd className="text-ink-2 tabular-nums">{v}</dd>
    </div>
  );
}

/** "+0.8s", "+12.4s", "+3m04s" from the attempt's start; the gutter of the log. */
function offset(ts: string, start: string | undefined): string {
  if (!start) return formatTimestamp(ts).slice(11);
  const ms = new Date(ts).getTime() - new Date(start).getTime();
  if (Number.isNaN(ms)) return "";
  const s = Math.max(0, ms) / 1000;
  if (s < 60) return `+${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  return `+${m}m${String(Math.round(s % 60)).padStart(2, "0")}s`;
}

function eventTone(e: TrajectoryEventRead): Tone {
  if (isErrorEvent(e)) return "bad";
  if (e.status === "ok") return "good";
  if (e.event_type.includes("model")) return "info";
  return "neutral";
}

function EventsPanel({
  runId,
  attempt,
  attemptNo,
  live,
}: {
  runId: string;
  attempt: RunAttemptRead | undefined;
  attemptNo: number;
  live: boolean;
}) {
  const query = useAttemptEvents(runId, attemptNo, live);
  const events = flatten(query.data, "events");
  const errors = events.filter(isErrorEvent).length;
  return (
    <Section
      title={`Trajectory · attempt #${attemptNo}`}
      note={events.length ? `${events.length} events${errors ? `, ${errors} with errors` : ""}` : undefined}
    >
      <Gate query={query} loadingLabel="Loading trajectory">
        {events.length === 0 ? (
          <Empty>No trajectory events for this attempt.</Empty>
        ) : (
          <>
            <ol aria-label="Trajectory events" className="font-mono text-[12.5px]">
              {events.map((e, i) => (
                <EventRow key={e.seq} e={e} newStep={i === 0 || events[i - 1]!.step !== e.step} start={attempt?.started_at} />
              ))}
            </ol>
            <LoadMore
              hasNext={!!query.hasNextPage}
              loading={query.isFetchingNextPage}
              onClick={() => void query.fetchNextPage()}
              shown={events.length}
            />
          </>
        )}
      </Gate>
    </Section>
  );
}

function EventRow({ e, newStep, start }: { e: TrajectoryEventRead; newStep: boolean; start: string | undefined }) {
  const error = isErrorEvent(e);
  const tokens = e.tokens_in !== null || e.tokens_out !== null ? `${formatInt(e.tokens_in)}→${formatInt(e.tokens_out)} tok` : null;
  return (
    <li className={`grid grid-cols-[3.25rem_14px_minmax(0,1fr)] gap-x-2 sm:grid-cols-[4.5rem_16px_minmax(0,1fr)] ${newStep ? "mt-1 border-t border-rule" : ""}`}>
      {/* gutter: step number on the first event of a step, then the offset from attempt start */}
      <div className="pt-2 text-right leading-tight">
        <div className={`text-[11px] ${newStep ? "text-ink-2" : "invisible"}`}>
          <span className="text-faint">step</span> {e.step}
        </div>
        <div className="text-[10.5px] text-faint tabular-nums" title={formatTimestamp(e.ts)}>
          {offset(e.ts, start)}
        </div>
      </div>
      {/* rail */}
      <div className="relative flex justify-center">
        <span aria-hidden="true" className="absolute inset-y-0 w-px bg-rule" />
        <span className="relative mt-[11px] h-fit bg-paper py-[2px]">
          <Glyph tone={eventTone(e)} />
        </span>
      </div>
      {/* entry */}
      <div className={`min-w-0 py-2 ${error ? "border-l-2 border-bad pl-2.5" : "pl-0"}`}>
        <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-0.5">
          <div className="flex min-w-0 flex-wrap items-baseline gap-x-2">
            <span className="text-ink">{e.event_type}</span>
            {e.tool ? (
              <>
                <span className="text-faint" aria-hidden="true">
                  ›
                </span>
                <span className="text-ink-2">{e.tool}</span>
              </>
            ) : null}
            {e.status && e.status !== "ok" ? <span className={error ? "text-bad" : "text-muted"}>{e.status}</span> : null}
            {e.error_class ? <span className="text-bad">{e.error_class}</span> : null}
          </div>
          <div className="flex items-baseline gap-3 text-[11.5px] text-faint tabular-nums">
            {tokens ? <span>{tokens}</span> : null}
            {e.cost_usd ? <span>{formatCost(e.cost_usd)}</span> : null}
            <span className={(e.duration_ms ?? 0) >= 10_000 ? "text-warn" : "text-ink-2"}>{formatDuration(e.duration_ms)}</span>
          </div>
        </div>
        {e.input || e.output ? (
          <details className="group mt-1">
            <summary className="cursor-pointer text-[11px] text-faint hover:text-ink">
              <span className="inline-block transition-transform group-open:rotate-90">▸</span> input / output
            </summary>
            {e.input ? <Payload label="in" value={e.input} /> : null}
            {e.output ? <Payload label="out" value={e.output} /> : null}
          </details>
        ) : null}
      </div>
    </li>
  );
}

function Payload({ label, value }: { label: string; value: unknown }) {
  return (
    <div className="mt-1.5 grid grid-cols-[2rem_minmax(0,1fr)] gap-2">
      <span className="pt-2 text-right text-[10.5px] uppercase text-faint">{label}</span>
      <pre className="max-h-56 overflow-auto rounded-[3px] bg-sunken p-2.5 text-[11.5px] leading-relaxed text-ink-2">{compactJson(value)}</pre>
    </div>
  );
}
