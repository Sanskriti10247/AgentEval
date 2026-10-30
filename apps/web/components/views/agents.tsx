"use client";

import Link from "next/link";
import { useState, type ReactNode } from "react";
import {
  ButtonLink,
  Empty,
  Field,
  Gate,
  LoadMore,
  Mono,
  PageHeader,
  ProviderMark,
  Section,
  Select,
  Table,
  Tag,
  Td,
  Th,
  Timestamp,
} from "@/components/ui";
import { flatten } from "@/components/pages-util";
import { EvaluationRows } from "@/components/views/evaluations";
import type { AgentVersionRead } from "@/lib/api/types";
import { formatTimestamp, shortId } from "@/lib/format";
import {
  useAgent,
  useAgents,
  useAgentVersions,
  useCapabilities,
  useDatasets,
  useEvaluationsForVersions,
  useEvaluationSummaries,
  useVersionIndex,
} from "@/lib/hooks";

export function AgentsView() {
  const query = useAgents();
  const agents = flatten(query.data, "agents");
  return (
    <>
      <PageHeader
        title="Agents"
        subtitle="Each agent is a series of immutable versions; results always point at the exact version that ran."
      />
      <Gate query={query}>
        {agents.length === 0 ? (
          <Empty title="No agents yet">
            Agents are registered through the API (POST /api/v1/agents, then a version under it). Once one exists it
            shows up here with its latest model and tools.
          </Empty>
        ) : (
          <>
            <Table caption="Agents">
              <thead>
                <tr>
                  <Th>Agent</Th>
                  <Th>Latest</Th>
                  <Th>Model</Th>
                  <Th className="hidden md:table-cell">Tools</Th>
                  <Th className="hidden sm:table-cell">Registered</Th>
                </tr>
              </thead>
              <tbody>
                {agents.map((a) => (
                  <tr key={a.id} className="hover:bg-sunken/50">
                    <Td>
                      <Link href={`/agents/${a.id}`} className="font-medium">
                        {a.name}
                      </Link>
                      <div className="font-mono text-[11px] text-faint">{shortId(a.id)}</div>
                    </Td>
                    <Td>
                      {a.latest_version ? <Mono>v{a.latest_version.version}</Mono> : <span className="text-faint">no versions</span>}
                    </Td>
                    <Td>
                      {a.latest_version ? (
                        <>
                          <Mono className="text-[12.5px]">{a.latest_version.model}</Mono>
                          <div className="flex items-center gap-2 text-[11.5px] text-faint">
                            {a.latest_version.model_provider} <ProviderMark provider={a.latest_version.model_provider} />
                          </div>
                        </>
                      ) : (
                        <span className="text-faint">–</span>
                      )}
                    </Td>
                    <Td className="hidden md:table-cell">
                      <div className="flex flex-wrap gap-1">{a.latest_version?.tools.map((t) => <Tag key={t}>{t}</Tag>)}</div>
                    </Td>
                    <Td className="hidden sm:table-cell">
                      <Timestamp value={formatTimestamp(a.created_at)} />
                    </Td>
                  </tr>
                ))}
              </tbody>
            </Table>
            <LoadMore
              hasNext={!!query.hasNextPage}
              loading={query.isFetchingNextPage}
              onClick={() => void query.fetchNextPage()}
              shown={agents.length}
            />
          </>
        )}
      </Gate>
    </>
  );
}

export function AgentDetailView({ id }: { id: string }) {
  const agent = useAgent(id);
  const versions = useAgentVersions(id);
  const caps = useCapabilities();
  const { index } = useVersionIndex();
  const sorted = [...(versions.data ?? [])].sort((a, b) => b.version - a.version);
  const evals = useEvaluationsForVersions(sorted.map((v) => v.id));
  const mine = evals.evaluations;
  const summaries = useEvaluationSummaries(mine.map((e) => e.id));
  const latest = sorted[0];
  const datasets = useDatasets();
  const datasetNames = new Map((datasets.data?.datasets ?? []).map((d) => [d.id, `${d.name} v${d.version}`]));

  return (
    <>
      <PageHeader
        eyebrow={
          <>
            <Link href="/agents" className="no-underline hover:text-ink">
              Agents
            </Link>{" "}
            / {shortId(id)}
          </>
        }
        title={agent.data?.name ?? "Agent"}
        subtitle={agent.data ? <span className="font-mono text-[12px]">{agent.data.id}</span> : undefined}
        actions={
          caps.canLaunch && latest ? (
            <>
              <ButtonLink href={`/runs/new?agent_version_id=${latest.id}`}>Start a run</ButtonLink>
              <ButtonLink href={`/evaluations/new?agent_version_id=${latest.id}`} variant="primary">
                Evaluate v{latest.version}
              </ButtonLink>
            </>
          ) : null
        }
      />
      <Gate query={agent}>
        <div className="space-y-10">
          <Section
            title="Versions"
            note={sorted.length ? `${sorted.length} version${sorted.length === 1 ? "" : "s"}, newest first; a version never changes once registered` : undefined}
          >
            <Gate query={versions}>
              {sorted.length === 0 ? (
                <Empty>This agent has a name but no versions yet, so there is nothing to run.</Empty>
              ) : (
                <ol aria-label="Agent versions" className="divide-y divide-rule">
                  {sorted.map((v, i) => (
                    <VersionSpec key={v.id} v={v} latest={i === 0} />
                  ))}
                </ol>
              )}
            </Gate>
          </Section>
          {sorted.length >= 2 ? (
            <Section title="Compare versions" note="fields that differ between the two picked versions">
              <VersionDiff versions={sorted} />
            </Section>
          ) : null}
          <Section title="Evaluations">
            <Gate query={evals}>
              {mine.length === 0 ? (
                <Empty>No evaluations for this agent yet.{caps.canLaunch && latest ? " Evaluate the latest version to see how it does on a dataset." : ""}</Empty>
              ) : (
                <EvaluationRows evaluations={mine} summaries={summaries.map((s) => s.data)} index={index} datasetNames={datasetNames} />
              )}
            </Gate>
          </Section>
        </div>
      </Gate>
    </>
  );
}

function VersionSpec({ v, latest }: { v: AgentVersionRead; latest: boolean }) {
  return (
    <li className="grid gap-x-6 gap-y-2 py-4 first:pt-1 sm:grid-cols-[88px_1fr]">
      <div className="flex items-baseline gap-2 sm:block">
        <div className="font-mono text-[20px] leading-none text-ink" title={v.id}>
          v{v.version}
        </div>
        {latest ? <div className="text-[11.5px] font-medium text-accent sm:mt-1.5">latest</div> : null}
      </div>
      <dl className="grid min-w-0 gap-x-8 gap-y-3 text-[13px] md:grid-cols-2">
        <Spec label="Model">
          <Mono>{v.model}</Mono> <span className="text-faint">· {v.model_provider}</span> <ProviderMark provider={v.model_provider} />
        </Spec>
        <Spec label="Registered">
          <Timestamp value={formatTimestamp(v.created_at)} />
        </Spec>
        <Spec label="Tools (allowed)">
          {v.tools.length ? <span className="flex flex-wrap gap-1">{v.tools.map((t) => <Tag key={t}>{t}</Tag>)}</span> : <span className="text-faint">none</span>}
        </Spec>
        <Spec label="Shell allowlist">
          {v.shell_allowlist.length ? <Mono className="text-[12.5px]">{v.shell_allowlist.join(", ")}</Mono> : <span className="text-faint">none</span>}
        </Spec>
        <Spec label="Limits">
          <SettingsList values={v.limits} empty="platform defaults" />
        </Spec>
        <Spec label="Model settings">
          <SettingsList values={v.model_settings} empty="provider defaults" />
        </Spec>
        <Spec label="Config hash">
          <Mono className="text-[12px] text-ink-2">{v.config_hash.slice(0, 12)}</Mono>
        </Spec>
        {v.system_prompt ? (
          <div className="md:col-span-2">
            <details>
              <summary className="cursor-pointer text-[12px] text-muted hover:text-ink">System prompt ({v.system_prompt.length} characters)</summary>
              <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap rounded-[5px] bg-sunken p-3 font-mono text-[11.5px] leading-relaxed text-ink-2">
                {v.system_prompt}
              </pre>
            </details>
          </div>
        ) : null}
      </dl>
    </li>
  );
}

const showValue = (v: unknown) => (typeof v === "string" ? v : JSON.stringify(v));

/** A flat settings object as aligned key / value lines. */
function SettingsList({ values, empty }: { values: Record<string, unknown>; empty: string }) {
  const entries = Object.entries(values).sort(([a], [b]) => a.localeCompare(b));
  if (entries.length === 0) return <span className="text-faint">{empty}</span>;
  return (
    <span className="grid grid-cols-[auto_1fr] gap-x-3 font-mono text-[12px] leading-snug">
      {entries.map(([k, val]) => (
        <span key={k} className="contents">
          <span className="text-muted">{k}</span>
          <span className="break-all text-ink-2">{showValue(val)}</span>
        </span>
      ))}
    </span>
  );
}

/** Every comparable field of a version, flattened: "limits.max_steps" -> "30". */
export function flattenVersion(v: AgentVersionRead): Map<string, string> {
  const out = new Map<string, string>();
  out.set("provider", v.model_provider);
  out.set("model", v.model);
  out.set("tools", [...v.tools].sort().join(", ") || "none");
  out.set("shell allowlist", [...v.shell_allowlist].sort().join(", ") || "none");
  out.set("system prompt", v.system_prompt ? `${v.system_prompt.length} characters` : "empty");
  for (const [k, val] of Object.entries(v.limits)) out.set(`limits.${k}`, showValue(val));
  for (const [k, val] of Object.entries(v.model_settings)) out.set(`model_settings.${k}`, showValue(val));
  out.set("config hash", v.config_hash.slice(0, 12));
  return out;
}

export function diffVersions(a: AgentVersionRead, b: AgentVersionRead): { field: string; a: string; b: string }[] {
  const fa = flattenVersion(a);
  const fb = flattenVersion(b);
  const fields = [...new Set([...fa.keys(), ...fb.keys()])];
  const rows = fields
    .map((field) => ({ field, a: fa.get(field) ?? "not set", b: fb.get(field) ?? "not set" }))
    .filter((r) => r.a !== r.b);
  // Same length, different text: say the prompt changed rather than showing equal lengths.
  if (a.system_prompt !== b.system_prompt && !rows.some((r) => r.field === "system prompt")) {
    rows.push({ field: "system prompt", a: "text differs", b: "text differs" });
  }
  return rows;
}

function VersionDiff({ versions }: { versions: AgentVersionRead[] }) {
  // Defaults: the previous version against the latest.
  const [baseId, setBaseId] = useState(versions[1]!.id);
  const [otherId, setOtherId] = useState(versions[0]!.id);
  const base = versions.find((v) => v.id === baseId) ?? versions[1]!;
  const other = versions.find((v) => v.id === otherId) ?? versions[0]!;
  const rows = base.id === other.id ? [] : diffVersions(base, other);
  const picker = (label: string, value: string, set: (id: string) => void) => (
    <Field label={label}>
      <Select value={value} onChange={(e) => set(e.target.value)}>
        {versions.map((v) => (
          <option key={v.id} value={v.id}>
            v{v.version} · {v.model}
          </option>
        ))}
      </Select>
    </Field>
  );
  return (
    <div className="max-w-3xl">
      <div className="mb-4 flex flex-wrap items-end gap-3">
        {picker("From", baseId, setBaseId)}
        {picker("To", otherId, setOtherId)}
      </div>
      {base.id === other.id ? (
        <p className="text-[13px] text-muted">Pick two different versions.</p>
      ) : rows.length === 0 ? (
        <p className="text-[13px] text-muted">
          No differences in the fields shown here. The config hashes match, so both versions run the same configuration.
        </p>
      ) : (
        <Table caption="Version differences">
          <thead>
            <tr>
              <Th>Field</Th>
              <Th>v{base.version}</Th>
              <Th>v{other.version}</Th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.field}>
                <Td className="whitespace-nowrap font-mono text-[12px] text-muted">{r.field}</Td>
                <Td className="break-all font-mono text-[12px] text-ink-2">{r.a}</Td>
                <Td className="break-all font-mono text-[12px] text-ink">{r.b}</Td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
    </div>
  );
}

function Spec({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-[12px] text-muted">{label}</dt>
      <dd className="mt-0.5 min-w-0 break-words">{children}</dd>
    </div>
  );
}
