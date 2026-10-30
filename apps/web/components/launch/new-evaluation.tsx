"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMemo, useState, type FormEvent, type ReactNode } from "react";
import { Button, Empty, Gate, Input, isSimulated, Loading, Notice, PageHeader, ProviderMark, Select } from "@/components/ui";
import { ApiError } from "@/lib/api/client";
import { MAX_REPETITIONS, type DatasetSummary, type EvaluationEstimate, type ProvidersRead } from "@/lib/api/types";
import { formatCost, formatInt } from "@/lib/format";
import {
  useCapabilities,
  useCreateEvaluation,
  useEvaluationEstimate,
  useFrozenDatasets,
  useProviders,
  useVersionIndex,
  type VersionInfo,
} from "@/lib/hooks";
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

export interface Availability {
  available: Map<string, VersionInfo>;
  unavailable: { version: VersionInfo; reason: string }[];
}

/**
 * Split versions by whether this server can run their provider. Without GET /providers
 * (an older API) every version is offered and the API decides on submit.
 */
export function splitByProvider(index: Map<string, VersionInfo>, providers: ProvidersRead | undefined): Availability {
  if (!providers) return { available: index, unavailable: [] };
  const byName = new Map(providers.providers.map((p) => [p.name, p]));
  const available = new Map<string, VersionInfo>();
  const unavailable: Availability["unavailable"] = [];
  for (const [id, v] of index) {
    const p = byName.get(v.provider);
    if (p?.available) available.set(id, v);
    else unavailable.push({ version: v, reason: p ? (p.reason ?? "Not available on this server.") : `This server does not know the provider "${v.provider}".` });
  }
  return { available, unavailable };
}

const defaultName = (v: VersionInfo | undefined, d: DatasetSummary | undefined) =>
  v && d ? `${v.agentName} v${v.version} on ${d.name} v${d.version}` : "";

export function NewEvaluationView({ initialVersionId }: { initialVersionId?: string }) {
  const caps = useCapabilities();
  const { index, isLoading } = useVersionIndex();
  const datasets = useFrozenDatasets();
  const providers = useProviders();
  const { available, unavailable } = useMemo(() => splitByProvider(index, providers.data), [index, providers.data]);

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
      ) : isLoading || providers.isLoading ? (
        <Loading label="Loading agents" />
      ) : (
        <Gate query={datasets} loadingLabel="Loading datasets">
          {index.size === 0 ? (
            <Empty title="Nothing to evaluate yet">
              There are no agent versions on this API. {caps.publicDemo ? "The demo is set up with a fixed set of agents; check back once they are loaded." : "Register an agent and a version through the API first."}
            </Empty>
          ) : available.size === 0 ? (
            <>
              <Empty title="No agent version can run here">
                Every registered version uses a model provider this server does not run. The reasons are listed below.
              </Empty>
              <UnavailableNote items={unavailable} />
            </>
          ) : (datasets.data?.datasets ?? []).length === 0 ? (
            <Empty title="No frozen datasets">
              Evaluations only run on frozen datasets, so the task list cannot change halfway through a benchmark.{" "}
              {caps.publicDemo ? "" : "Freeze one with POST /api/v1/datasets/{id}/freeze."}
            </Empty>
          ) : (
            <NewEvaluationForm
              index={available}
              unavailable={unavailable}
              providers={providers.data}
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
  budget?: string;
}

function UnavailableNote({ items }: { items: Availability["unavailable"] }) {
  if (items.length === 0) return null;
  return (
    <div className="mt-4 text-[12.5px] leading-relaxed text-muted" role="note" aria-label="Versions not offered">
      <div className="mb-1 font-mono text-[10.5px] uppercase tracking-[0.1em] text-faint">Not offered here</div>
      <ul className="space-y-0.5">
        {items.map(({ version: v, reason }) => (
          <li key={v.versionId}>
            {v.agentName} v{v.version} <span className="font-mono text-[12px]">({v.provider}/{v.model})</span>: {reason}
          </li>
        ))}
      </ul>
    </div>
  );
}

/** A budget as typed: a positive amount up to 1000 with at most six decimals. */
const BUDGET_RE = /^\d{1,4}(\.\d{1,6})?$/;

export function NewEvaluationForm({
  index,
  unavailable = [],
  providers,
  datasets,
  initialVersionId,
  maxRepetitions,
  concurrentRuns,
  publicDemo,
}: {
  index: Map<string, VersionInfo>;
  unavailable?: Availability["unavailable"];
  providers?: ProvidersRead;
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
  const [budget, setBudget] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const [key, setKey] = useState(newIdempotencyKey);

  const version = index.get(versionId);
  const dataset = datasets.find((d) => d.id === datasetId);
  const effectiveName = name ?? defaultName(version, dataset);
  const reps = Number(repetitions);
  const runCount = dataset && Number.isInteger(reps) && reps > 0 ? dataset.task_count * reps : null;
  const real = !!version && !isSimulated(version.provider);
  const repsValid = /^\d+$/.test(repetitions.trim()) && reps >= 1 && reps <= maxRepetitions;
  const estimate = useEvaluationEstimate(
    version && dataset && repsValid ? { agent_version_id: version.versionId, dataset_id: dataset.id, repetitions: reps } : null,
  );
  const defaultBudget = estimate.data?.default_budget_usd ?? providers?.default_real_budget_usd ?? null;

  const errors: Errors = {};
  if (!version) errors.version = "Pick an agent version.";
  if (!dataset) errors.dataset = "Pick a dataset.";
  if (!effectiveName.trim()) errors.name = "Give the evaluation a name.";
  else if (effectiveName.trim().length > 200) errors.name = "Keep the name to 200 characters or fewer.";
  if (!repsValid) errors.repetitions = `Enter a whole number from 1 to ${maxRepetitions}.`;
  const budgetText = budget.trim();
  if (real && budgetText && (!BUDGET_RE.test(budgetText) || Number(budgetText) <= 0 || Number(budgetText) > 1000))
    errors.budget = "Enter an amount in dollars above 0 and at most 1000, for example 2.50.";
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
        body: {
          name: effectiveName.trim(),
          agent_version_id: version.versionId,
          dataset_id: dataset.id,
          repetitions: reps,
          // Blank means the server's default budget for real providers; fake models need none.
          ...(real && budgetText ? { max_cost_usd: budgetText } : {}),
        },
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
          hint={
            version ? (
              <>
                Model <span className="font-mono text-[12px] text-ink-2">{version.model}</span> from {version.provider}{" "}
                <ProviderMark provider={version.provider} />
                {real ? " Real model calls cost money." : " Deterministic; no model is called and nothing is spent."}
              </>
            ) : (
              "Every version is immutable, so the result always points at exactly what ran."
            )
          }
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
        <UnavailableNote items={unavailable} />

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

        {real ? (
          <FormRow
            label="Budget (USD)"
            error={show("budget")}
            hint={
              <>
                Spending cap for the whole evaluation. Once it is reached, the remaining runs fail without calling the model.
                {defaultBudget ? ` Leave blank to use this server's default of ${formatCost(defaultBudget)}.` : " Leave blank to use this server's default."}
              </>
            }
          >
            {(p) => (
              <Input
                {...p}
                type="text"
                inputMode="decimal"
                placeholder={defaultBudget ?? ""}
                className="w-32 font-mono"
                value={budget}
                onChange={(e) => edit(setBudget)(e.target.value)}
              />
            )}
          </FormRow>
        ) : null}

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
          {repsValid && version && dataset ? (
            <EstimatePreview
              estimate={estimate.data}
              stale={estimate.isPlaceholderData || estimate.isFetching}
              error={estimate.isError ? estimate.error : null}
              budget={real ? (budgetText && !errors.budget ? budgetText : defaultBudget) : null}
              budgetIsDefault={!budgetText}
              concurrency={providers?.real_provider_concurrency ?? null}
            />
          ) : null}
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

/**
 * Upper bounds from POST /evaluations/estimate. Every figure is a ceiling the platform
 * enforces (step and cost caps per run), not a prediction of what the run will use.
 */
export function EstimatePreview({
  estimate: e,
  stale,
  error,
  budget,
  budgetIsDefault,
  concurrency,
}: {
  estimate: EvaluationEstimate | undefined;
  stale: boolean;
  error: unknown;
  budget: string | null;
  budgetIsDefault: boolean;
  concurrency: number | null;
}) {
  if (error && !e) {
    // An API without the endpoint: the run count above is all there is.
    if (error instanceof ApiError && error.status === 404) return null;
    return <p className="text-[12.5px] text-muted">Upper bounds are unavailable: {error instanceof Error ? error.message : "the estimate failed"}.</p>;
  }
  if (!e) return <p className="font-mono text-[12px] text-faint">Estimating upper bounds…</p>;
  const cost = !e.pricing_known
    ? `pricing unknown for ${e.model}`
    : e.simulated
      ? `${formatCost(e.max_cost_usd)} simulated, no real spend`
      : `${formatCost(e.max_cost_usd)} upper bound`;
  const rows: [string, ReactNode][] = [
    ["Runs", `${e.tasks} tasks × ${e.repetitions} repetitions = ${formatInt(e.runs)}`],
    [
      "Model requests",
      <>
        at most {formatInt(e.max_model_requests)}{" "}
        <span className="text-faint">
          ({formatInt(e.runs)} runs × {e.max_steps_per_run} steps × {e.run_max_attempts} attempt{e.run_max_attempts === 1 ? "" : "s"})
        </span>
      </>,
    ],
    [
      "Cost",
      <>
        {cost}
        {e.pricing_known && e.max_cost_usd_per_run ? (
          <span className="text-faint"> (per-run cap {formatCost(e.max_cost_usd_per_run)} × runs × attempts)</span>
        ) : null}
      </>,
    ],
  ];
  if (!e.simulated && budget) rows.push(["Budget", `stops at ${formatCost(budget)}${budgetIsDefault ? " (server default)" : ""}`]);
  return (
    <div className={`max-w-[34rem] ${stale ? "opacity-60" : ""}`} aria-label="Estimate" role="group">
      <dl className="grid grid-cols-[7.5rem_1fr] gap-x-4 gap-y-1 text-[12.5px]">
        {rows.map(([k, v]) => (
          <div key={k} className="contents">
            <dt className="text-muted">{k}</dt>
            <dd className="font-mono text-[12px] tabular-nums text-ink-2">{v}</dd>
          </div>
        ))}
      </dl>
      {e.sequential ? (
        <p className="mt-2 text-[12.5px] leading-relaxed text-muted">
          Real-provider runs execute {concurrency && concurrency > 1 ? `at most ${concurrency} at a time` : "one at a time"} per worker process,
          so this evaluation takes longer than a simulated one of the same size.
        </p>
      ) : null}
      {!e.available ? <p className="mt-2 text-[12.5px] text-bad">This server cannot run {e.provider} right now; the launch will be refused.</p> : null}
    </div>
  );
}
