"use client";

import Link from "next/link";
import { useState } from "react";
import { Badge, Empty, Field, Gate, IdLink, LoadMore, Mono, PageHeader, RunStatusBadge, Select, Table, Td, Th } from "@/components/ui";
import { flatten } from "@/components/pages-util";
import { RUN_STATUSES, type RunStatus } from "@/lib/api/types";
import { formatDuration, formatTimestamp, humanize } from "@/lib/format";
import { useRuns } from "@/lib/hooks";

export interface RunsInitialFilters {
  status?: RunStatus;
  dead_letter?: boolean;
  evaluation_id?: string;
  task_id?: string;
}

export function RunsView({ initial = {} }: { initial?: RunsInitialFilters }) {
  const [status, setStatus] = useState<string>(initial.status ?? "");
  const [deadLetter, setDeadLetter] = useState(initial.dead_letter ?? false);
  const query = useRuns({
    status: (status || undefined) as RunStatus | undefined,
    dead_letter: deadLetter ? true : undefined,
    evaluation_id: initial.evaluation_id,
    task_id: initial.task_id,
  });
  const runs = flatten(query.data, "runs");

  return (
    <>
      <PageHeader
        title="Runs"
        subtitle={
          initial.evaluation_id || initial.task_id ? (
            <>
              Filtered by {initial.evaluation_id ? "evaluation" : "task"}. <Link href="/runs">Clear</Link>
            </>
          ) : (
            "Newest first. The dead-letter view lists FAILED runs that exhausted their retries or hit a non-retryable error."
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
            <label className="flex items-center gap-2 self-end pb-1 text-sm">
              <input type="checkbox" checked={deadLetter} onChange={(e) => setDeadLetter(e.target.checked)} />
              Dead-letter only
            </label>
          </>
        }
      />
      <Gate query={query}>
        {runs.length === 0 ? (
          <Empty>No runs match these filters.</Empty>
        ) : (
          <>
            <Table caption="Runs">
              <thead>
                <tr>
                  <Th>Run</Th>
                  <Th>Status</Th>
                  <Th>Outcome</Th>
                  <Th className="text-right">Attempts</Th>
                  <Th>Dead letter</Th>
                  <Th>Failure reason</Th>
                  <Th>Task</Th>
                  <Th>Evaluation</Th>
                  <Th className="text-right">Duration</Th>
                  <Th>Created</Th>
                </tr>
              </thead>
              <tbody>
                {runs.map((r) => (
                  <tr key={r.id}>
                    <Td>
                      <IdLink id={r.id} href={`/runs/${r.id}`} />
                    </Td>
                    <Td>
                      <RunStatusBadge status={r.status} />
                    </Td>
                    <Td className="font-mono text-xs">{r.outcome ?? "–"}</Td>
                    <Td className="text-right">
                      <Mono>
                        {r.attempt_count}/{r.max_attempts}
                      </Mono>
                    </Td>
                    <Td>{r.dead_letter_reason ? <Badge tone="bad">{humanize(r.dead_letter_reason)}</Badge> : "–"}</Td>
                    <Td className="text-xs">{humanize(r.failure_reason)}</Td>
                    <Td>
                      <IdLink id={r.task_id} />
                    </Td>
                    <Td>{r.evaluation_id ? <IdLink id={r.evaluation_id} href={`/evaluations/${r.evaluation_id}`} /> : "–"}</Td>
                    <Td className="text-right">
                      <Mono>
                        {r.started_at && r.finished_at
                          ? formatDuration(new Date(r.finished_at).getTime() - new Date(r.started_at).getTime())
                          : "–"}
                      </Mono>
                    </Td>
                    <Td className="whitespace-nowrap font-mono">{formatTimestamp(r.created_at)}</Td>
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
