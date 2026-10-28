"use client";

import Link from "next/link";
import type { ReactNode } from "react";
import { ButtonLink, Empty, Gate, LoadMore, Mono, PageHeader, Section, Table, Tag, Td, Th, Timestamp } from "@/components/ui";
import { flatten } from "@/components/pages-util";
import { EvaluationRows } from "@/components/views/evaluations";
import type { AgentVersionRead } from "@/lib/api/types";
import { compactJson, formatTimestamp, shortId } from "@/lib/format";
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
        eyebrow="Registry"
        title="Agents"
        subtitle="An agent is a name for a series of immutable versions. Changing the model, prompt, tools or limits makes a new version, so results always point at exactly what ran."
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
                          <div className="text-[11.5px] text-faint">{a.latest_version.model_provider}</div>
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
          <Section title="Versions" note={sorted.length ? `${sorted.length} version${sorted.length === 1 ? "" : "s"}, newest first` : undefined}>
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
  const limits = Object.keys(v.limits).length ? compactJson(v.limits, 400) : null;
  return (
    <li className="grid gap-x-6 gap-y-2 py-4 first:pt-1 sm:grid-cols-[88px_1fr]">
      <div className="flex items-baseline gap-2 sm:block">
        <div className="font-mono text-[20px] leading-none text-ink" title={v.id}>
          v{v.version}
        </div>
        {latest ? <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-accent sm:mt-1.5">latest</div> : null}
      </div>
      <dl className="grid min-w-0 gap-x-8 gap-y-2 text-[13px] md:grid-cols-2">
        <Spec label="Model">
          <Mono>{v.model}</Mono> <span className="text-faint">· {v.model_provider}</span>
        </Spec>
        <Spec label="Registered">
          <Timestamp value={formatTimestamp(v.created_at)} />
        </Spec>
        <Spec label="Tools">
          {v.tools.length ? <span className="flex flex-wrap gap-1">{v.tools.map((t) => <Tag key={t}>{t}</Tag>)}</span> : <span className="text-faint">none</span>}
        </Spec>
        <Spec label="Shell allowlist">
          {v.shell_allowlist.length ? <Mono className="text-[12.5px]">{v.shell_allowlist.join(", ")}</Mono> : <span className="text-faint">none</span>}
        </Spec>
        <Spec label="Limits">
          {limits ? (
            <pre className="whitespace-pre-wrap break-words font-mono text-[11.5px] leading-snug text-ink-2">{limits}</pre>
          ) : (
            <span className="text-faint">platform defaults</span>
          )}
        </Spec>
        <Spec label="Config hash">
          <Mono className="text-[12px] text-ink-2">{v.config_hash.slice(0, 12)}</Mono>
        </Spec>
        {v.system_prompt ? (
          <div className="md:col-span-2">
            <details>
              <summary className="cursor-pointer text-[12px] text-muted hover:text-ink">System prompt ({v.system_prompt.length} characters)</summary>
              <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap rounded-[3px] bg-sunken p-3 font-mono text-[11.5px] leading-relaxed text-ink-2">
                {v.system_prompt}
              </pre>
            </details>
          </div>
        ) : null}
      </dl>
    </li>
  );
}

function Spec({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-faint">{label}</dt>
      <dd className="mt-0.5 min-w-0 break-words">{children}</dd>
    </div>
  );
}
