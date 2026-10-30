"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMemo, useState, type FormEvent } from "react";
import { Button, Empty, Gate, Input, Loading, Notice, PageHeader, Select } from "@/components/ui";
import { MAX_REPETITIONS, type DatasetSummary } from "@/lib/api/types";
import { formatInt } from "@/lib/format";
import { useCapabilities, useCreateEvaluation, useFrozenDatasets, useVersionIndex, type VersionInfo } from "@/lib/hooks";
import { FormRow, NextSteps, newIdempotencyKey } from "./form";
import { describeWriteError } from "./write-error";

/** Versions grouped by agent (alphabetical), newest version first within each. */
export function groupVersions(index: Map<string, VersionInfo>): { agent: string; versions: VersionInfo[] }[] {
  const byAgent = new Map<string, VersionInfo[]>();
  for (const v of index.values()) byAgent.set(v.agentName, [...(byAgent.get(v.agentName) ?? []), v]);
  return [...byAgent.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([agent, versions]) => ({ agent, versions: versions.sort((a, b) => b.version - a.version) }));
}

const defaultName = (v: VersionInfo | undefined, d: DatasetSummary | undefined) =>
  v && d ? `${v.agentName} v${v.version} on ${d.name} v${d.version}` : "";

export function NewEvaluationView({ initialVersionId }: { initialVersionId?: string }) {
  const caps = useCapabilities();
  const { index, isLoading } = useVersionIndex();
  const datasets = useFrozenDatasets();

  return (
    <>
      <PageHeader
        eyebrow={
          <>
            <Link href="/evaluations" className="no-underline hover:text-ink">
              Evaluations
            </Link>{" "}
            / new
          </>
        }
        title="New evaluation"
        subtitle="Run one agent version over every task of a frozen dataset and grade each attempt. The numbers you get are comparable with any other evaluation on the same dataset."
      />
      {caps.readOnly ? (
        <Notice tone="info" title="This deployment is read-only">
          Browsing works, but the API refuses new evaluations here. Run AgentEval locally to launch your own.
        </Notice>
      ) : isLoading ? (
        <Loading label="Loading agents" />
      ) : (
        <Gate query={datasets} loadingLabel="Loading datasets">
          {index.size === 0 ? (
            <Empty title="Nothing to evaluate yet">
              There are no agent versions on this API. {caps.publicDemo ? "The demo is set up with a fixed set of agents; check back once they are loaded." : "Register an agent and a version through the API first."}
            </Empty>
          ) : (datasets.data?.datasets ?? []).length === 0 ? (
            <Empty title="No frozen datasets">
              Evaluations only run on frozen datasets, so the task list cannot change halfway through a benchmark.{" "}
              {caps.publicDemo ? "" : "Freeze one with POST /api/v1/datasets/{id}/freeze."}
            </Empty>
          ) : (
            <NewEvaluationForm
              index={index}
              datasets={datasets.data?.datasets ?? []}
              initialVersionId={initialVersionId}
              maxRepetitions={caps.demoLimits?.max_repetitions ?? MAX_REPETITIONS}
              concurrentRuns={caps.demoLimits?.concurrent_runs ?? null}
              publicDemo={caps.publicDemo}
            />
          )}
        </Gate>
      )}
    </>
  );
}

interface Errors {
  name?: string;
  version?: string;
  dataset?: string;
  repetitions?: string;
}

export function NewEvaluationForm({
  index,
  datasets,
  initialVersionId,
  maxRepetitions,
  concurrentRuns,
  publicDemo,
}: {
  index: Map<string, VersionInfo>;
  datasets: DatasetSummary[];
  initialVersionId?: string;
  maxRepetitions: number;
  concurrentRuns: number | null;
  publicDemo: boolean;
}) {
  const router = useRouter();
  const create = useCreateEvaluation();
  const groups = useMemo(() => groupVersions(index), [index]);
  const firstVersion = groups[0]?.versions[0]?.versionId ?? "";

  const [versionId, setVersionId] = useState(initialVersionId && index.has(initialVersionId) ? initialVersionId : firstVersion);
  const [datasetId, setDatasetId] = useState(datasets[0]?.id ?? "");
  const [repetitions, setRepetitions] = useState("1");
  const [name, setName] = useState<string | null>(null); // null: follow the generated default
  const [submitted, setSubmitted] = useState(false);
  const [key, setKey] = useState(newIdempotencyKey);

  const version = index.get(versionId);
  const dataset = datasets.find((d) => d.id === datasetId);
  const effectiveName = name ?? defaultName(version, dataset);
  const reps = Number(repetitions);
  const runCount = dataset && Number.isInteger(reps) && reps > 0 ? dataset.task_count * reps : null;

  const errors: Errors = {};
  if (!version) errors.version = "Pick an agent version.";
  if (!dataset) errors.dataset = "Pick a dataset.";
  if (!effectiveName.trim()) errors.name = "Give the evaluation a name.";
  else if (effectiveName.trim().length > 200) errors.name = "Keep the name to 200 characters or fewer.";
  if (!/^\d+$/.test(repetitions.trim()) || reps < 1 || reps > maxRepetitions)
    errors.repetitions = `Enter a whole number from 1 to ${maxRepetitions}.`;
  const valid = Object.keys(errors).length === 0;
  const show = (k: keyof Errors) => (submitted ? (errors[k] ?? null) : null);

  // Any change makes it a different request, so it gets a different idempotency key.
  const edit = <T,>(set: (v: T) => void) => (v: T) => {
    set(v);
    setKey(newIdempotencyKey());
    if (create.isError) create.reset();
  };

  function submit(e: FormEvent) {
    e.preventDefault();
    setSubmitted(true);
    if (!valid || !version || !dataset) return;
    create.mutate(
      {
        body: { name: effectiveName.trim(), agent_version_id: version.versionId, dataset_id: dataset.id, repetitions: reps },
        key,
      },
      { onSuccess: (created) => router.push(`/evaluations/${created.id}`) },
    );
  }

  const failure = create.isError ? describeWriteError(create.error) : null;

  return (
    <div className="grid gap-10 lg:grid-cols-[minmax(0,1fr)_15rem]">
      <form aria-label="New evaluation" noValidate onSubmit={submit} className="-mt-4">
        <FormRow
          label="Agent version"
          error={show("version")}
          hint={version ? <>Model <span className="font-mono text-[12px] text-ink-2">{version.model}</span> from {version.provider}.</> : "Every version is immutable, so the result always points at exactly what ran."}
        >
          {(p) => (
            <Select {...p} className="w-full" value={versionId} onChange={(e) => edit(setVersionId)(e.target.value)}>
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

        <FormRow
          label="Dataset"
          error={show("dataset")}
          hint={dataset?.description ?? "Only frozen datasets are listed: their task list can no longer change."}
        >
          {(p) => (
            <Select {...p} className="w-full" value={datasetId} onChange={(e) => edit(setDatasetId)(e.target.value)}>
              {datasets.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.name} v{d.version} · {d.task_count} task{d.task_count === 1 ? "" : "s"}
                </option>
              ))}
            </Select>
          )}
        </FormRow>

        <FormRow
          label="Repetitions"
          error={show("repetitions")}
          hint={
            <>
              Each repetition runs every task once more. More repetitions narrow the confidence interval on the success
              rate{publicDemo ? `; the demo allows up to ${maxRepetitions}` : ""}.
            </>
          }
        >
          {(p) => (
            <Input
              {...p}
              type="number"
              inputMode="numeric"
              min={1}
              max={maxRepetitions}
              step={1}
              className="w-24 font-mono"
              value={repetitions}
              onChange={(e) => edit(setRepetitions)(e.target.value)}
            />
          )}
        </FormRow>

        <FormRow label="Name" error={show("name")} hint="Shown in lists and comparisons. The suggestion follows your picks until you type over it.">
          {(p) => (
            <Input
              {...p}
              type="text"
              maxLength={200}
              className="w-full"
              value={effectiveName}
              onChange={(e) => edit((v: string) => setName(v))(e.target.value)}
            />
          )}
        </FormRow>

        <div className="space-y-4 py-5 sm:pl-[calc(11rem+2rem)]">
          <p className="text-[13px] text-ink-2" aria-live="polite">
            {runCount !== null ? (
              <>
                This queues <span className="font-mono tabular-nums text-ink">{formatInt(runCount)}</span> run{runCount === 1 ? "" : "s"}:{" "}
                <span className="font-mono tabular-nums">{dataset?.task_count}</span> task{dataset?.task_count === 1 ? "" : "s"} ×{" "}
                <span className="font-mono tabular-nums">{reps}</span> repetition{reps === 1 ? "" : "s"}.
                {concurrentRuns !== null && runCount > concurrentRuns
                  ? ` This instance runs ${concurrentRuns === 1 ? "one" : concurrentRuns} at a time, so the rest wait their turn.`
                  : ""}
              </>
            ) : (
              "Pick a dataset and a number of repetitions to see how many runs this queues."
            )}
          </p>
          {failure ? <Notice title={failure.title}>{failure.body}</Notice> : null}
          {submitted && !valid ? <p className="text-[12.5px] text-bad">Fix the fields marked above, then launch again.</p> : null}
          <div className="flex items-center gap-3">
            <Button type="submit" variant="primary" disabled={create.isPending}>
              {create.isPending ? "Launching…" : "Launch evaluation"}
            </Button>
            <Link href="/evaluations" className="text-[13px] text-muted">
              Cancel
            </Link>
          </div>
        </div>
      </form>
      <NextSteps
        steps={[
          "The API queues one run per task and repetition, all pinned to this agent version.",
          "Workers pick runs up and execute each in its own sandbox, retrying platform failures.",
          "Every finished attempt is graded against the task's hidden checks.",
          "You land on the evaluation page, which follows progress live until the last run is graded.",
        ]}
      />
    </div>
  );
}
