"use client";

import Link from "next/link";
import { useState } from "react";
import { ButtonLink, Empty, Field, Gate, IdLink, LoadMore, Mono, Num, PageHeader, RunStatusBadge, Select, Table, Td, Th, Timestamp } from "@/components/ui";
import { flatten } from "@/components/pages-util";
import { RUN_STATUSES, type RunStatus } from "@/lib/api/types";
import { formatDuration, formatTimestamp, humanize, shortId } from "@/lib/format";
import { useCapabilities, useRuns, useTaskOptions } from "@/lib/hooks";

export interface RunsInitialFilters {
  status?: RunStatus;
  dead_letter?: boolean;
  evaluation_id?: string;
  task_id?: string;
}

export function RunsView({ initial = {} }: { initial?: RunsInitialFilters }) {
  const [status, setStatus] = useState<string>(initial.status ?? "");
  const [deadLetter, setDeadLetter] = useState(initial.dead_letter ?? false);
  const caps = useCapabilities();
  const query = useRuns({
    status: (status || undefined) as RunStatus | undefined,
    dead_letter: deadLetter ? true : undefined,
    evaluation_id: initial.evaluation_id,
    task_id: initial.task_id,
  });
  const runs = flatten(query.data, "runs");
  const tasks = useTaskOptions();
  const taskNames = new Map((tasks.data?.tasks ?? []).map((t) => [t.id, t.name]));
  const filtered = !!(initial.evaluation_id || initial.task_id);

  return (
    <>
      <PageHeader
        title="Runs"
        subtitle={
          filtered ? (
            <>
              Showing runs for one {initial.evaluation_id && initial.task_id ? "task within an evaluation" : initial.evaluation_id ? "evaluation" : "task"}.{" "}
              <Link href="/runs">Show all runs</Link>
            </>
          ) : (
            "Newest first, from evaluations and standalone launches. Dead-letter runs failed for good: retries used up or not worth retrying."
          )
        }
        actions={
          <>
            <Field label="Status">
              <Select value={status} onChange={(e) => setStatus(e.target.value)}>
                <option value="">All statuses</option>
                {RUN_STATUSES.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </Select>
            </Field>
            <label className="flex h-8 items-center gap-2 text-[13px] text-ink-2">
              <input type="checkbox" checked={deadLetter} onChange={(e) => setDeadLetter(e.target.checked)} />
              Dead-letter only
            </label>
            {caps.canLaunch ? (
              <ButtonLink href="/runs/new" variant="primary">
                Start a run
              </ButtonLink>
            ) : null}
          </>
        }
      />
      <Gate query={query}>
        {runs.length === 0 ? (
          <Empty>
            No runs match these filters.{" "}
            {status || deadLetter ? "Try All statuses, or untick Dead-letter only." : caps.canLaunch ? "Start one, or launch an evaluation to queue a batch." : ""}
          </Empty>
        ) : (
          <>
            <Table caption="Runs">
              <thead>
                <tr>
                  <Th>Run</Th>
                  <Th>Status</Th>
                  <Th className="hidden md:table-cell">Outcome</Th>
                  <Th className="text-right">Attempts</Th>
                  <Th>Task</Th>
                  <Th className="hidden lg:table-cell">Evaluation</Th>
                  <Th className="hidden text-right sm:table-cell">Duration</Th>
                  <Th className="hidden xl:table-cell">Created</Th>
                </tr>
              </thead>
              <tbody>
                {runs.map((r) => (
                  <tr key={r.id} className="hover:bg-sunken/50">
                    <Td>
                      <IdLink id={r.id} href={`/runs/${r.id}`} />
                    </Td>
                    <Td>
                      <RunStatusBadge status={r.status} />
                      {r.dead_letter_reason || r.failure_reason ? (
                        <div className="mt-0.5 text-[11.5px] leading-snug text-muted">
                          {r.dead_letter_reason ? <span className="text-bad">{humanize(r.dead_letter_reason)}</span> : null}
                          {r.dead_letter_reason && r.failure_reason ? <span className="text-faint"> · </span> : null}
                          {r.failure_reason ? <span>{humanize(r.failure_reason)}</span> : null}
                        </div>
                      ) : null}
                    </Td>
                    <Td className="hidden font-mono text-[11.5px] text-muted md:table-cell">{r.outcome ?? "–"}</Td>
                    <Td className="text-right">
                      <Mono className="text-[12.5px]">
                        {r.attempt_count}/{r.max_attempts}
                      </Mono>
                    </Td>
                    <Td>
                      {taskNames.get(r.task_id) ? (
                        <span title={r.task_id}>{taskNames.get(r.task_id)}</span>
                      ) : (
                        <IdLink id={r.task_id} />
                      )}
                    </Td>
                    <Td className="hidden lg:table-cell">
                      {r.evaluation_id ? (
                        <IdLink id={r.evaluation_id} href={`/evaluations/${r.evaluation_id}`} label={shortId(r.evaluation_id)} />
                      ) : (
                        <span className="text-[12px] text-faint">on its own</span>
                      )}
                    </Td>
                    <Num className="hidden sm:table-cell">
                      {r.started_at && r.finished_at
                        ? formatDuration(new Date(r.finished_at).getTime() - new Date(r.started_at).getTime())
                        : "–"}
                    </Num>
                    <Td className="hidden xl:table-cell">
                      <Timestamp value={formatTimestamp(r.created_at)} />
                    </Td>
                  </tr>
                ))}
              </tbody>
            </Table>
            <LoadMore
              hasNext={!!query.hasNextPage}
              loading={query.isFetchingNextPage}
              onClick={() => void query.fetchNextPage()}
              shown={runs.length}
            />
          </>
        )}
      </Gate>
    </>
  );
}
