"use client";

import { useState } from "react";
import { Badge, Empty, Field, Gate, IdLink, LoadMore, Mono, PageHeader, Select, Table, Td, Th } from "@/components/ui";
import { flatten } from "@/components/pages-util";
import type { Difficulty, TaskType } from "@/lib/api/types";
import { formatTimestamp } from "@/lib/format";
import { useDatasets, useTasks } from "@/lib/hooks";

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
        subtitle="Units of evaluation. Instructions, files and hidden checks are never exposed by the API."
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
          <Empty>No tasks match these filters.</Empty>
        ) : (
          <>
            <Table caption="Tasks">
              <thead>
                <tr>
                  <Th>Task</Th>
                  <Th>Type</Th>
                  <Th>Difficulty</Th>
                  <Th>Dataset</Th>
                  <Th>Evaluation</Th>
                  <Th className="text-right">Timeout</Th>
                  <Th className="text-right">Memory</Th>
                  <Th>Created</Th>
                </tr>
              </thead>
              <tbody>
                {tasks.map((t) => (
                  <tr key={t.id}>
                    <Td>
                      <div>{t.name}</div>
                      <div className="font-mono text-xs text-muted" title={t.id}>
                        {t.id.slice(0, 8)}
                      </div>
                    </Td>
                    <Td>
                      <Badge>{t.task_type}</Badge>
                    </Td>
                    <Td>{t.difficulty ?? "–"}</Td>
                    <Td>{t.dataset_id ? (names.get(t.dataset_id) ?? <IdLink id={t.dataset_id} />) : "–"}</Td>
                    <Td>{t.has_evaluation ? <Badge tone="good">has evaluation</Badge> : <Badge tone="muted">none</Badge>}</Td>
                    <Td className="text-right"><Mono>{t.timeout_s} s</Mono></Td>
                    <Td className="text-right"><Mono>{t.memory_mb} MB</Mono></Td>
                    <Td className="whitespace-nowrap font-mono">{formatTimestamp(t.created_at)}</Td>
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
