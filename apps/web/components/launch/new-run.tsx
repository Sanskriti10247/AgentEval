"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMemo, useState, type FormEvent } from "react";
import { Button, Empty, Gate, Loading, Notice, PageHeader, Select } from "@/components/ui";
import type { TaskListItem } from "@/lib/api/types";
import { useCapabilities, useCreateRun, useDatasets, useTaskOptions, useVersionIndex, type VersionInfo } from "@/lib/hooks";
import { FormRow, NextSteps, newIdempotencyKey } from "./form";
import { groupVersions } from "./new-evaluation";
import { describeWriteError } from "./write-error";

export function NewRunView({ initialVersionId, initialTaskId }: { initialVersionId?: string; initialTaskId?: string }) {
  const caps = useCapabilities();
  const { index, isLoading } = useVersionIndex();
  const tasks = useTaskOptions();
  const datasets = useDatasets();
  const datasetNames = new Map((datasets.data?.datasets ?? []).map((d) => [d.id, `${d.name} v${d.version}`]));

  return (
    <>
      <PageHeader
        eyebrow={
          <>
            <Link href="/runs" className="no-underline hover:text-ink">
              Runs
            </Link>{" "}
            / new
          </>
        }
        title="Start a run"
        subtitle="One agent version on one task, once. To measure an agent, launch an evaluation instead."
      />
      {caps.readOnly ? (
        <Notice tone="info" title="This deployment is read-only">
          Browsing works, but the API refuses new runs here. Run AgentEval locally to start your own.
        </Notice>
      ) : isLoading ? (
        <Loading label="Loading agents" />
      ) : (
        <Gate query={tasks} loadingLabel="Loading tasks">
          {(tasks.data?.tasks ?? []).length === 0 ? (
            <Empty title="No tasks yet">There is nothing to run. Tasks are added through the API.</Empty>
          ) : (
            <NewRunForm
              index={index}
              tasks={tasks.data?.tasks ?? []}
              datasetNames={datasetNames}
              initialVersionId={initialVersionId}
              initialTaskId={initialTaskId}
            />
          )}
        </Gate>
      )}
    </>
  );
}

export function NewRunForm({
  index,
  tasks,
  datasetNames,
  initialVersionId,
  initialTaskId,
}: {
  index: Map<string, VersionInfo>;
  tasks: TaskListItem[];
  datasetNames: Map<string, string>;
  initialVersionId?: string;
  initialTaskId?: string;
}) {
  const router = useRouter();
  const create = useCreateRun();
  const groups = useMemo(() => groupVersions(index), [index]);
  // Agent tasks first: they are the ones that need an agent, and the interesting ones to watch.
  const ordered = useMemo(
    () => [...tasks].sort((a, b) => (a.task_type === b.task_type ? a.name.localeCompare(b.name) : a.task_type === "agent" ? -1 : 1)),
    [tasks],
  );
  const defaultTask = (initialTaskId && tasks.find((t) => t.id === initialTaskId)?.id) || ordered[0]?.id || "";

  const [taskId, setTaskId] = useState(defaultTask);
  const [versionId, setVersionId] = useState(
    initialVersionId && index.has(initialVersionId) ? initialVersionId : (groups[0]?.versions[0]?.versionId ?? ""),
  );
  const [submitted, setSubmitted] = useState(false);
  const [key, setKey] = useState(newIdempotencyKey);

  const task = tasks.find((t) => t.id === taskId);
  const needsAgent = task?.task_type !== "command";
  const version = index.get(versionId);

  const errors: { task?: string; version?: string } = {};
  if (!task) errors.task = "Pick a task.";
  if (needsAgent && !version) errors.version = index.size ? "Agent tasks need an agent version." : "There are no agent versions to run this task with.";
  const valid = Object.keys(errors).length === 0;

  const change = (set: (v: string) => void) => (v: string) => {
    set(v);
    setKey(newIdempotencyKey());
    if (create.isError) create.reset();
  };

  function submit(e: FormEvent) {
    e.preventDefault();
    setSubmitted(true);
    if (!valid || !task) return;
    create.mutate(
      { body: needsAgent && version ? { task_id: task.id, agent_version_id: version.versionId } : { task_id: task.id }, key },
      { onSuccess: (run) => router.push(`/runs/${run.id}`) },
    );
  }

  const failure = create.isError ? describeWriteError(create.error) : null;

  return (
    <div className="grid gap-10 lg:grid-cols-[minmax(0,1fr)_15rem]">
      <form aria-label="Start a run" noValidate onSubmit={submit} className="-mt-4">
        <FormRow
          label="Task"
          error={submitted ? (errors.task ?? null) : null}
          hint={
            task ? (
              <>
                {task.description ? `${task.description} ` : ""}
                <span className="font-mono text-[12px]">
                  {task.task_type} · {task.difficulty ?? "no difficulty"} · {task.timeout_s} s limit
                  {task.dataset_id && datasetNames.get(task.dataset_id) ? ` · ${datasetNames.get(task.dataset_id)}` : ""}
                </span>
              </>
            ) : undefined
          }
        >
          {(p) => (
            <Select {...p} className="w-full" value={taskId} onChange={(e) => change(setTaskId)(e.target.value)}>
              {ordered.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}
                  {t.difficulty ? ` · ${t.difficulty}` : ""}
                  {t.task_type === "command" ? " · command" : ""}
                </option>
              ))}
            </Select>
          )}
        </FormRow>

        <FormRow
          label="Agent version"
          error={submitted ? (errors.version ?? null) : null}
          hint={
            needsAgent
              ? version
                ? <>Model <span className="font-mono text-[12px] text-ink-2">{version.model}</span> from {version.provider}.</>
                : undefined
              : "This is a command task: it runs a fixed command in the sandbox, with no agent involved."
          }
        >
          {(p) => (
            <Select {...p} className="w-full" value={needsAgent ? versionId : ""} disabled={!needsAgent} onChange={(e) => change(setVersionId)(e.target.value)}>
              {!needsAgent ? <option value="">not used</option> : null}
              {groups.map((g) => (
                <optgroup key={g.agent} label={g.agent}>
                  {g.versions.map((v, i) => (
                    <option key={v.versionId} value={v.versionId}>
                      {g.agent} v{v.version}
                      {i === 0 ? " (latest)" : ""} · {v.model}
                    </option>
                  ))}
                </optgroup>
              ))}
            </Select>
          )}
        </FormRow>

        <div className="space-y-4 py-5 sm:pl-[calc(11rem+2rem)]">
          {failure ? <Notice title={failure.title}>{failure.body}</Notice> : null}
          <div className="flex items-center gap-3">
            <Button type="submit" variant="primary" disabled={create.isPending}>
              {create.isPending ? "Starting…" : "Start run"}
            </Button>
            <Link href="/runs" className="text-[13px] text-muted">
              Cancel
            </Link>
          </div>
        </div>
      </form>
      <NextSteps
        steps={[
          "The run is queued with the platform's default attempt budget.",
          "A worker claims it and starts a fresh sandbox for the task.",
          "You land on the run page and watch the trajectory arrive step by step.",
          "When it finishes, the grade and score appear beside the trajectory.",
        ]}
      />
    </div>
  );
}
