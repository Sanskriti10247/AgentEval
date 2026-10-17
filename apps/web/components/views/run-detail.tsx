"use client";

import { useState } from "react";
import {
  Badge,
  Button,
  CategoryBadge,
  Empty,
  ErrorState,
  EvaluationStatusBadge,
  Gate,
  IdLink,
  KeyValue,
  LoadMore,
  Mono,
  PageHeader,
  Panel,
  RunStatusBadge,
  Table,
  Td,
  Th,
} from "@/components/ui";
import { flatten } from "@/components/pages-util";
import { TERMINAL_RUN_STATUSES, type AttemptEvaluationRead, type RunAttemptRead, type TrajectoryEventRead } from "@/lib/api/types";
import { compactJson, formatCost, formatDuration, formatInt, formatScore, formatTimestamp, humanize } from "@/lib/format";
import { useAttemptEvaluation, useAttemptEvents, useAttempts, useCancelRun, useReplayRun, useRun } from "@/lib/hooks";

function isErrorEvent(e: TrajectoryEventRead) {
  return !!e.error_class || e.status === "error" || e.status === "timeout" || e.event_type.includes("error");
}

export function RunDetailView({ id }: { id: string }) {
  const run = useRun(id);
  const r = run.data;
  const live = !!r && !TERMINAL_RUN_STATUSES.includes(r.status);
  const attempts = useAttempts(id, live);
  const cancel = useCancelRun(id);
  const replay = useReplayRun(id);
  const [extra, setExtra] = useState(1);
  const [picked, setPicked] = useState<number | null>(null);

  const list = attempts.data ?? [];
  const selectedNo = picked ?? (list.length ? Math.max(...list.map((a) => a.attempt_no)) : null);
  const canCancel = !!r && !r.cancel_requested_at && ["QUEUED", "CLAIMED", "RUNNING", "RETRYING"].includes(r.status);
  const canReplay = r?.status === "FAILED";

  return (
    <>
      <PageHeader
        title="Run"
        subtitle={<span className="font-mono">{id}</span>}
        actions={
          r ? (
            <>
              <RunStatusBadge status={r.status} />
              <Button variant="danger" disabled={!canCancel || cancel.isPending} onClick={() => cancel.mutate()}>
                {cancel.isPending ? "Cancelling…" : "Cancel"}
              </Button>
              <label className="flex items-center gap-1 text-xs text-muted">
                +
                <input
                  type="number"
                  min={1}
                  max={9}
                  value={extra}
                  aria-label="Additional attempts"
                  onChange={(e) => setExtra(Math.min(9, Math.max(1, Number(e.target.value) || 1)))}
                  className="w-12 rounded border border-line bg-panel px-1 py-1 font-mono text-sm text-fg"
                />
              </label>
              <Button disabled={!canReplay || replay.isPending} onClick={() => replay.mutate(extra)}>
                {replay.isPending ? "Replaying…" : "Replay"}
              </Button>
            </>
          ) : null
        }
      />
      <Gate query={run}>
        {r ? (
          <div className="space-y-4">
            {cancel.isError ? <ErrorState error={cancel.error} /> : null}
            {replay.isError ? <ErrorState error={replay.error} /> : null}
            <Panel title="Run">
              <KeyValue
                items={[
                  ["Status", <RunStatusBadge key="s" status={r.status} />],
                  ["Outcome", <Mono key="o">{r.outcome ?? "–"}</Mono>],
                  [
                    "Attempt budget",
                    <Mono key="b">
                      {r.attempt_count} used of {r.max_attempts}
                      {r.replay_count ? ` (replayed ${r.replay_count}x)` : ""}
                    </Mono>,
                  ],
                  ["Dead letter", r.dead_letter_reason ? <Badge key="d" tone="bad">{humanize(r.dead_letter_reason)}</Badge> : "–"],
                  ["Failure reason", humanize(r.failure_reason)],
                  ["Error", r.error_message ? <span key="e" className="text-bad">{r.error_message}</span> : "–"],
                  ["Next attempt at", <Mono key="n">{formatTimestamp(r.next_attempt_at)}</Mono>],
                  [
                    "Cancel requested",
                    r.cancel_requested_at ? (
                      <Mono key="c">
                        {formatTimestamp(r.cancel_requested_at)} ({r.cancel_reason})
                      </Mono>
                    ) : (
                      "–"
                    ),
                  ],
                  ["Task", <IdLink key="t" id={r.task_id} label={r.task_id} />],
                  [
                    "Evaluation",
                    r.evaluation_id ? (
                      <span key="ev">
                        <IdLink id={r.evaluation_id} href={`/evaluations/${r.evaluation_id}`} label={r.evaluation_id} />
                        {r.repetition !== null ? <Mono className="text-muted"> rep {r.repetition}</Mono> : null}
                      </span>
                    ) : (
                      "ad hoc"
                    ),
                  ],
                  ["Agent version", r.agent_version_id ? <IdLink key="av" id={r.agent_version_id} label={r.agent_version_id} /> : "command task"],
                  ["Worker", <Mono key="w">{r.worker_id ?? "–"}</Mono>],
                  ["Created", <Mono key="cr">{formatTimestamp(r.created_at)}</Mono>],
                  ["Started", <Mono key="st">{formatTimestamp(r.started_at)}</Mono>],
                  ["Finished", <Mono key="f">{formatTimestamp(r.finished_at)}</Mono>],
                ]}
              />
            </Panel>

            <Panel title="Attempts">
              <Gate query={attempts} loadingLabel="Loading attempts">
                {list.length === 0 ? (
                  <Empty>No attempts yet.</Empty>
                ) : (
                  <ol aria-label="Attempt timeline" className="space-y-1">
                    {list.map((a) => (
                      <AttemptRow key={a.id} a={a} selected={a.attempt_no === selectedNo} onSelect={() => setPicked(a.attempt_no)} />
                    ))}
                  </ol>
                )}
              </Gate>
            </Panel>

            {selectedNo !== null ? (
              <>
                <AttemptDetail attempt={list.find((a) => a.attempt_no === selectedNo)} />
                <EvaluationPanel runId={id} attemptNo={selectedNo} />
                <EventsPanel runId={id} attemptNo={selectedNo} live={live} />
              </>
            ) : null}
          </div>
        ) : null}
      </Gate>
    </>
  );
}

function AttemptRow({ a, selected, onSelect }: { a: RunAttemptRead; selected: boolean; onSelect: () => void }) {
  return (
    <li>
      <button
        type="button"
        onClick={onSelect}
        aria-pressed={selected}
        className={`flex w-full flex-wrap items-center gap-x-4 gap-y-1 rounded border px-3 py-2 text-left text-[13px] hover:bg-panel2 ${
          selected ? "border-accent bg-panel2" : "border-line"
        }`}
      >
        <Mono className="font-semibold">#{a.attempt_no}</Mono>
        <Badge tone={a.status === "FINISHED" ? "good" : a.status === "RUNNING" ? "active" : a.status === "ABANDONED" ? "bad" : "muted"}>
          {a.status}
        </Badge>
        <span className="font-mono text-xs">{a.outcome ?? "–"}</span>
        <Mono>{formatDuration(a.duration_ms)}</Mono>
        <span className="text-muted">
          steps <Mono>{a.steps ?? "–"}</Mono>
        </span>
        <span className="text-muted">
          tokens <Mono>{formatInt(a.tokens_in)}/{formatInt(a.tokens_out)}</Mono>
        </span>
        <Mono>{formatCost(a.cost_usd)}</Mono>
        <span className="font-mono text-xs text-muted">{formatTimestamp(a.started_at)}</span>
        {a.error_message ? <span className="min-w-0 basis-full truncate text-bad">{a.error_message}</span> : null}
      </button>
    </li>
  );
}

function AttemptDetail({ attempt }: { attempt: RunAttemptRead | undefined }) {
  if (!attempt) return null;
  return (
    <Panel title={`Attempt #${attempt.attempt_no} output`}>
      <KeyValue
        items={[
          ["Worker", <Mono key="w">{attempt.worker_id}</Mono>],
          ["Exit code", <Mono key="x">{attempt.exit_code ?? "–"}{attempt.oom_killed ? " (OOM killed)" : ""}</Mono>],
          ["Sandbox image", <Mono key="i">{attempt.sandbox_image_id ?? "–"}</Mono>],
          ["Final answer", attempt.final_answer ? <pre key="fa" className="whitespace-pre-wrap font-mono text-xs">{attempt.final_answer}</pre> : "–"],
        ]}
      />
      {(["stdout", "stderr"] as const).map((k) =>
        attempt[k] ? (
          <details key={k} className="mt-2">
            <summary className="cursor-pointer text-xs text-muted">
              {k}
              {attempt[`${k}_truncated`] ? " (truncated)" : ""}
            </summary>
            <pre className="mt-1 max-h-64 overflow-auto rounded bg-panel2 p-2 font-mono text-xs">{attempt[k]}</pre>
          </details>
        ) : null,
      )}
    </Panel>
  );
}

function EvaluationPanel({ runId, attemptNo }: { runId: string; attemptNo: number }) {
  const query = useAttemptEvaluation(runId, attemptNo);
  const ev: AttemptEvaluationRead | null | undefined = query.data;
  return (
    <Panel title={`Evaluation (attempt #${attemptNo})`}>
      <Gate query={query} loadingLabel="Loading evaluation">
        {!ev ? (
          <Empty>No evaluation recorded for this attempt (command task, not graded yet, or the run never started).</Empty>
        ) : (
          <div className="space-y-3">
            <KeyValue
              items={[
                ["Status", <EvaluationStatusBadge key="s" status={ev.status} />],
                [
                  "Task success",
                  ev.task_success === null ? "–" : <Badge key="ts" tone={ev.task_success ? "good" : "bad"}>{ev.task_success ? "passed" : "failed"}</Badge>,
                ],
                ["Failure category", <CategoryBadge key="fc" category={ev.failure_category} />],
                ["Tags", ev.tags.length ? <span key="t" className="flex flex-wrap gap-1">{ev.tags.map((t) => <Badge key={t}>{t}</Badge>)}</span> : "–"],
              ]}
            />
            {ev.score ? (
              <Table caption="Score components">
                <thead>
                  <tr>
                    <Th>Overall</Th>
                    <Th>Task success</Th>
                    <Th>Tests</Th>
                    <Th>Reliability</Th>
                    <Th>Efficiency</Th>
                    <Th>Integrity gate</Th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <Td className="font-mono font-semibold">{formatScore(ev.score.overall)}</Td>
                    <Td className="font-mono">{formatScore(ev.score.task_success)}</Td>
                    <Td className="font-mono">{formatScore(ev.score.test_score)}</Td>
                    <Td className="font-mono">{formatScore(ev.score.reliability)}</Td>
                    <Td className="font-mono">{formatScore(ev.score.efficiency)}</Td>
                    <Td>{ev.score.integrity_gate_applied ? "applied" : "no"}</Td>
                  </tr>
                </tbody>
              </Table>
            ) : null}
            {ev.checks.length ? (
              <Table caption="Checks">
                <thead>
                  <tr>
                    <Th>Check</Th>
                    <Th>Result</Th>
                    <Th className="text-right">Score</Th>
                    <Th className="text-right">Duration</Th>
                  </tr>
                </thead>
                <tbody>
                  {ev.checks.map((c) => (
                    <tr key={c.position}>
                      <Td className="font-mono">#{c.position}</Td>
                      <Td>
                        <Badge tone={c.passed === null ? "muted" : c.passed ? "good" : "bad"}>
                          {c.passed === null ? "not run" : c.passed ? "pass" : "fail"}
                        </Badge>
                      </Td>
                      <Td className="text-right font-mono">{c.score ?? "–"}</Td>
                      <Td className="text-right font-mono">{formatDuration(c.duration_ms)}</Td>
                    </tr>
                  ))}
                </tbody>
              </Table>
            ) : null}
            {ev.metrics ? (
              <p className="font-mono text-xs text-muted">
                model calls {ev.metrics.model_calls} (errors {ev.metrics.model_errors}) · tool calls {ev.metrics.tool_calls} ·
                tool error rate {ev.metrics.tool_error_rate ?? "–"} · loop {ev.metrics.loop_detected ? "detected" : "no"} · ran
                tests {ev.metrics.ran_tests ? "yes" : "no"}
                {ev.metrics.limits_reached.length ? ` · limits ${ev.metrics.limits_reached.join(", ")}` : ""}
              </p>
            ) : null}
          </div>
        )}
      </Gate>
    </Panel>
  );
}

function EventsPanel({ runId, attemptNo, live }: { runId: string; attemptNo: number; live: boolean }) {
  const query = useAttemptEvents(runId, attemptNo, live);
  const events = flatten(query.data, "events");
  return (
    <Panel title={`Trajectory (attempt #${attemptNo})`}>
      <Gate query={query} loadingLabel="Loading trajectory">
        {events.length === 0 ? (
          <Empty>No trajectory events for this attempt.</Empty>
        ) : (
          <>
            <Table caption="Trajectory events">
              <thead>
                <tr>
                  <Th className="text-right">Seq</Th>
                  <Th className="text-right">Step</Th>
                  <Th>Event</Th>
                  <Th>Tool</Th>
                  <Th>Status</Th>
                  <Th className="text-right">Duration</Th>
                  <Th className="text-right">Tokens in/out</Th>
                  <Th className="text-right">Cost</Th>
                  <Th>Detail</Th>
                </tr>
              </thead>
              <tbody>
                {events.map((e) => (
                  <tr key={e.seq} className={isErrorEvent(e) ? "bg-bad/10" : undefined}>
                    <Td className="text-right font-mono">{e.seq}</Td>
                    <Td className="text-right font-mono">{e.step}</Td>
                    <Td className="font-mono">{e.event_type}</Td>
                    <Td className="font-mono">{e.tool ?? "–"}</Td>
                    <Td>
                      {e.status ? <Badge tone={e.status === "ok" ? "good" : isErrorEvent(e) ? "bad" : "neutral"}>{e.status}</Badge> : "–"}
                      {e.error_class ? <div className="font-mono text-xs text-bad">{e.error_class}</div> : null}
                    </Td>
                    <Td className="text-right font-mono">{formatDuration(e.duration_ms)}</Td>
                    <Td className="text-right font-mono">
                      {e.tokens_in !== null || e.tokens_out !== null ? `${formatInt(e.tokens_in)}/${formatInt(e.tokens_out)}` : "–"}
                    </Td>
                    <Td className="text-right font-mono">{formatCost(e.cost_usd)}</Td>
                    <Td>
                      {e.input || e.output ? (
                        <details>
                          <summary className="cursor-pointer text-xs text-muted">input / output</summary>
                          {e.input ? <pre className="mt-1 max-h-48 max-w-xl overflow-auto rounded bg-panel2 p-2 font-mono text-xs">{compactJson(e.input)}</pre> : null}
                          {e.output ? <pre className="mt-1 max-h-48 max-w-xl overflow-auto rounded bg-panel2 p-2 font-mono text-xs">{compactJson(e.output)}</pre> : null}
                        </details>
                      ) : (
                        <span className="text-muted">–</span>
                      )}
                    </Td>
                  </tr>
                ))}
              </tbody>
            </Table>
            <LoadMore
              hasNext={!!query.hasNextPage}
              loading={query.isFetchingNextPage}
              onClick={() => void query.fetchNextPage()}
              shown={events.length}
            />
          </>
        )}
      </Gate>
    </Panel>
  );
}

