"use client";

import { Empty, Gate, LoadMore, Mono, Num, PageHeader, StatusMark, Table, Td, Th, Timestamp } from "@/components/ui";
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
        subtitle={`${live} live of ${workers.length} listed. Live means not stopped, with a recent heartbeat; a stale worker's runs are picked up by the others.`}
      />
      <Gate query={query}>
        {workers.length === 0 ? (
          <Empty>No workers have registered yet. Runs stay QUEUED until one starts and claims them.</Empty>
        ) : (
          <>
            <Table caption="Workers">
              <thead>
                <tr>
                  <Th>Worker</Th>
                  <Th>State</Th>
                  <Th className="text-right">Heartbeat</Th>
                  <Th className="hidden text-right sm:table-cell">Slots</Th>
                  <Th className="hidden md:table-cell">Host</Th>
                  <Th className="hidden md:table-cell">Version</Th>
                  <Th className="hidden lg:table-cell">Started</Th>
                  <Th className="hidden lg:table-cell">Stopped</Th>
                </tr>
              </thead>
              <tbody>
                {workers.map((w) => {
                  const state = w.stopped_at ? "stopped" : w.alive ? "live" : "stale";
                  return (
                    <tr key={w.id} className="hover:bg-sunken/50">
                      <Td className="font-mono text-[12.5px]">{w.id}</Td>
                      <Td>
                        <StatusMark tone={state === "live" ? "good" : state === "stale" ? "warn" : "muted"}>{state}</StatusMark>
                        {w.stop_reason ? <span className="ml-2 text-[12px] text-faint">{w.stop_reason}</span> : null}
                      </Td>
                      <Num className={state === "stale" ? "text-warn" : ""}>{formatAge(ageSeconds(w.last_heartbeat_at, now))}</Num>
                      <Num className="hidden sm:table-cell">{w.concurrency}</Num>
                      <Td className="hidden font-mono text-[12.5px] text-ink-2 md:table-cell">{w.hostname}</Td>
                      <Td className="hidden md:table-cell">
                        <Mono className="text-[12.5px] text-ink-2">{w.version}</Mono>
                      </Td>
                      <Td className="hidden lg:table-cell">
                        <Timestamp value={formatTimestamp(w.started_at)} />
                      </Td>
                      <Td className="hidden lg:table-cell">
                        <Timestamp value={formatTimestamp(w.stopped_at)} />
                      </Td>
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
