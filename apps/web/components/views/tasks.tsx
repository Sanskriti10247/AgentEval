"use client";

import Link from "next/link";
import { useState } from "react";
import { Empty, Field, Gate, IdLink, LoadMore, Num, PageHeader, Select, StatusMark, Table, Tag, Td, Th, Timestamp } from "@/components/ui";
import { flatten } from "@/components/pages-util";
import type { Difficulty, TaskType } from "@/lib/api/types";
import { formatTimestamp, shortId } from "@/lib/format";
import { useDatasets, useTasks } from "@/lib/hooks";

/** Difficulty as one to three ticks, so a column of them reads at a glance. */
function DifficultyTicks({ value }: { value: string | null }) {
  const n = value === "easy" ? 1 : value === "medium" ? 2 : value === "hard" ? 3 : 0;
  if (!n) return <span className="text-faint">–</span>;
  return (
    <span className="inline-flex items-center gap-2 text-[12.5px] text-ink-2">
      <span className="inline-flex gap-[2px]" aria-hidden="true">
        {[1, 2, 3].map((i) => (
          <span key={i} className={`h-[9px] w-[3px] ${i <= n ? "bg-ink-2" : "bg-rule"}`} />
        ))}
      </span>
      {value}
    </span>
  );
}

export function TasksView() {
  const [datasetId, setDatasetId] = useState("");
  const [taskType, setTaskType] = useState("");
  const [difficulty, setDifficulty] = useState("");
  const datasets = useDatasets();
  const query = useTasks({
    dataset_id: datasetId || undefined,
    task_type: (taskType || undefined) as TaskType | undefined,
    difficulty: (difficulty || undefined) as Difficulty | undefined,
  });
  const tasks = flatten(query.data, "tasks");
  const names = new Map((datasets.data?.datasets ?? []).map((d) => [d.id, `${d.name} v${d.version}`]));

  return (
    <>
      <PageHeader
        title="Tasks"
        subtitle="What agents are measured on. Instructions, files and hidden checks are never served here."
        actions={
          <>
            <Field label="Dataset">
              <Select value={datasetId} onChange={(e) => setDatasetId(e.target.value)}>
                <option value="">All datasets</option>
                {(datasets.data?.datasets ?? []).map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.name} v{d.version}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="Type">
              <Select value={taskType} onChange={(e) => setTaskType(e.target.value)}>
                <option value="">All types</option>
                <option value="agent">agent</option>
                <option value="command">command</option>
              </Select>
            </Field>
            <Field label="Difficulty">
              <Select value={difficulty} onChange={(e) => setDifficulty(e.target.value)}>
                <option value="">Any difficulty</option>
                <option value="easy">easy</option>
                <option value="medium">medium</option>
                <option value="hard">hard</option>
              </Select>
            </Field>
          </>
        }
      />
      <Gate query={query}>
        {tasks.length === 0 ? (
          <Empty>No tasks match these filters. Datasets fill up through the API (POST /api/v1/tasks with a dataset_id).</Empty>
        ) : (
          <>
            <Table caption="Tasks">
              <thead>
                <tr>
                  <Th>Task</Th>
                  <Th>Type</Th>
                  <Th>Difficulty</Th>
                  <Th className="hidden md:table-cell">Dataset</Th>
                  <Th className="hidden sm:table-cell">Grading</Th>
                  <Th className="hidden text-right lg:table-cell">Timeout</Th>
                  <Th className="hidden text-right lg:table-cell">Memory</Th>
                  <Th className="hidden xl:table-cell">Created</Th>
                </tr>
              </thead>
              <tbody>
                {tasks.map((t) => (
                  <tr key={t.id} className="hover:bg-sunken/50">
                    <Td>
                      <Link href={`/runs?task_id=${t.id}`} className="font-medium no-underline hover:underline" title="Runs of this task">
                        {t.name}
                      </Link>
                      {t.description ? <div className="mt-0.5 max-w-[46ch] text-[12px] leading-snug text-muted">{t.description}</div> : null}
                      <div className="font-mono text-[11px] text-faint" title={t.id}>
                        {shortId(t.id)}
                      </div>
                    </Td>
                    <Td>
                      <Tag>{t.task_type}</Tag>
                    </Td>
                    <Td>
                      <DifficultyTicks value={t.difficulty} />
                    </Td>
                    <Td className="hidden whitespace-nowrap md:table-cell">{t.dataset_id ? (names.get(t.dataset_id) ?? <IdLink id={t.dataset_id} />) : <span className="text-faint">–</span>}</Td>
                    <Td className="hidden sm:table-cell">
                      {t.has_evaluation ? <StatusMark tone="good">has evaluation</StatusMark> : <StatusMark tone="muted">none</StatusMark>}
                    </Td>
                    <Num className="hidden lg:table-cell">{t.timeout_s} s</Num>
                    <Num className="hidden lg:table-cell">{t.memory_mb} MB</Num>
                    <Td className="hidden xl:table-cell">
                      <Timestamp value={formatTimestamp(t.created_at)} />
                    </Td>
                  </tr>
                ))}
              </tbody>
            </Table>
            <LoadMore
              hasNext={!!query.hasNextPage}
              loading={query.isFetchingNextPage}
              onClick={() => void query.fetchNextPage()}
              shown={tasks.length}
            />
          </>
        )}
      </Gate>
    </>
  );
}
