"use client";

import Link from "next/link";
import { Badge, Empty, Gate, LoadMore, Mono, PageHeader, Panel, Table, Td, Th } from "@/components/ui";
import { flatten } from "@/components/pages-util";
import { compactJson, formatTimestamp } from "@/lib/format";
import { useAgent, useAgents, useAgentVersions, useEvaluationsForVersions, useEvaluationSummaries, useVersionIndex } from "@/lib/hooks";
import { EvaluationRows } from "@/components/views/evaluations";

export function AgentsView() {
  const query = useAgents();
  const agents = flatten(query.data, "agents");
  return (
    <>
      <PageHeader title="Agents" subtitle="Each agent is a named series of immutable versions." />
      <Gate query={query}>
        {agents.length === 0 ? (
          <Empty>No agents yet. Create one with POST /api/v1/agents.</Empty>
        ) : (
          <>
            <Table caption="Agents">
              <thead>
                <tr>
                  <Th>Name</Th>
                  <Th>Latest version</Th>
                  <Th>Provider</Th>
                  <Th>Model</Th>
                  <Th>Tools</Th>
                  <Th>Created</Th>
                </tr>
              </thead>
              <tbody>
                {agents.map((a) => (
                  <tr key={a.id}>
                    <Td>
                      <Link href={`/agents/${a.id}`}>{a.name}</Link>
                    </Td>
                    <Td>{a.latest_version ? <Mono>v{a.latest_version.version}</Mono> : <span className="text-muted">no versions</span>}</Td>
                    <Td>{a.latest_version?.model_provider ?? "–"}</Td>
                    <Td>{a.latest_version ? <Mono>{a.latest_version.model}</Mono> : "–"}</Td>
                    <Td>
                      <div className="flex flex-wrap gap-1">
                        {a.latest_version?.tools.map((t) => <Badge key={t}>{t}</Badge>)}
                      </div>
                    </Td>
                    <Td className="whitespace-nowrap font-mono">{formatTimestamp(a.created_at)}</Td>
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
  const { index } = useVersionIndex();
  const versionIds = (versions.data ?? []).map((v) => v.id);
  const evals = useEvaluationsForVersions(versionIds);
  const mine = evals.evaluations;
  const summaries = useEvaluationSummaries(mine.map((e) => e.id));

  return (
    <>
      <PageHeader
        title={agent.data?.name ?? "Agent"}
        subtitle={agent.data ? <span className="font-mono">{agent.data.id}</span> : undefined}
      />
      <Gate query={agent}>
        <div className="space-y-4">
          <Panel title="Versions">
            <Gate query={versions}>
              {(versions.data ?? []).length === 0 ? (
                <Empty>This agent has no versions yet.</Empty>
              ) : (
                <Table caption="Agent versions">
                  <thead>
                    <tr>
                      <Th>Version</Th>
                      <Th>Provider</Th>
                      <Th>Model</Th>
                      <Th>Tools</Th>
                      <Th>Shell allowlist</Th>
                      <Th>Limits</Th>
                      <Th>Created</Th>
                    </tr>
                  </thead>
                  <tbody>
                    {[...(versions.data ?? [])]
                      .sort((a, b) => b.version - a.version)
                      .map((v) => (
                        <tr key={v.id}>
                          <Td>
                            <span title={v.id} className="font-mono">v{v.version}</span>
                          </Td>
                          <Td>{v.model_provider}</Td>
                          <Td><Mono>{v.model}</Mono></Td>
                          <Td>
                            <div className="flex flex-wrap gap-1">{v.tools.map((t) => <Badge key={t}>{t}</Badge>)}</div>
                          </Td>
                          <Td className="font-mono">{v.shell_allowlist.join(", ") || "–"}</Td>
                          <Td>
                            <pre className="max-w-xs whitespace-pre-wrap break-words font-mono text-xs text-muted">
                              {Object.keys(v.limits).length ? compactJson(v.limits, 400) : "defaults"}
                            </pre>
                          </Td>
                          <Td className="whitespace-nowrap font-mono">{formatTimestamp(v.created_at)}</Td>
                        </tr>
                      ))}
                  </tbody>
                </Table>
              )}
            </Gate>
          </Panel>
          <Panel title="Evaluations">
            <Gate query={evals}>
              {mine.length === 0 ? (
                <Empty>No evaluations for this agent yet.</Empty>
              ) : (
                <EvaluationRows evaluations={mine} summaries={summaries.map((s) => s.data)} index={index} />
              )}
            </Gate>
          </Panel>
        </div>
      </Gate>
    </>
  );
}
