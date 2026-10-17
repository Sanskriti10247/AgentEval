"use client";

import { Badge, Empty, Gate, LoadMore, Mono, PageHeader, Table, Td, Th } from "@/components/ui";
import { flatten } from "@/components/pages-util";
import { ageSeconds, formatAge, formatTimestamp } from "@/lib/format";
import { useWorkers } from "@/lib/hooks";

export function WorkersView() {
  const query = useWorkers();
  const workers = flatten(query.data, "workers");
  // Heartbeat age is measured at the last successful fetch, so render stays pure.
  const now = query.dataUpdatedAt;
  const live = workers.filter((w) => w.alive).length;

  return (
    <>
      <PageHeader
        title="Workers"
        subtitle={`${live} live of ${workers.length} listed. A worker is live when it has not stopped and its heartbeat is recent.`}
      />
      <Gate query={query}>
        {workers.length === 0 ? (
          <Empty>No workers have registered yet.</Empty>
        ) : (
          <>
            <Table caption="Workers">
              <thead>
                <tr>
                  <Th>Worker</Th>
                  <Th>State</Th>
                  <Th className="text-right">Heartbeat age</Th>
                  <Th className="text-right">Concurrency</Th>
                  <Th>Host</Th>
                  <Th>Version</Th>
                  <Th>Started</Th>
                  <Th>Stopped</Th>
                </tr>
              </thead>
              <tbody>
                {workers.map((w) => {
                  const state = w.stopped_at ? "stopped" : w.alive ? "live" : "stale";
                  return (
                    <tr key={w.id}>
                      <Td className="font-mono">{w.id}</Td>
                      <Td>
                        <Badge tone={state === "live" ? "good" : state === "stale" ? "warn" : "muted"}>{state}</Badge>
                        {w.stop_reason ? <span className="ml-2 text-xs text-muted">{w.stop_reason}</span> : null}
                      </Td>
                      <Td className="text-right"><Mono>{formatAge(ageSeconds(w.last_heartbeat_at, now))}</Mono></Td>
                      <Td className="text-right"><Mono>{w.concurrency}</Mono></Td>
                      <Td className="font-mono">{w.hostname}</Td>
                      <Td className="font-mono">{w.version}</Td>
                      <Td className="whitespace-nowrap font-mono">{formatTimestamp(w.started_at)}</Td>
                      <Td className="whitespace-nowrap font-mono">{formatTimestamp(w.stopped_at)}</Td>
                    </tr>
                  );
                })}
              </tbody>
            </Table>
            <LoadMore
              hasNext={!!query.hasNextPage}
              loading={query.isFetchingNextPage}
              onClick={() => void query.fetchNextPage()}
              shown={workers.length}
            />
          </>
        )}
      </Gate>
    </>
  );
}
