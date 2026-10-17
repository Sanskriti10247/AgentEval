import { RunsView, type RunsInitialFilters } from "@/components/views/runs";
import { RUN_STATUSES, type RunStatus } from "@/lib/api/types";

export const metadata = { title: "Runs" };

type Search = Record<string, string | string[] | undefined>;
const first = (v: string | string[] | undefined) => (Array.isArray(v) ? v[0] : v);

export default async function Page({ searchParams }: { searchParams: Promise<Search> }) {
  const sp = await searchParams;
  const status = first(sp.status);
  const initial: RunsInitialFilters = {
    status: RUN_STATUSES.includes(status as RunStatus) ? (status as RunStatus) : undefined,
    dead_letter: first(sp.dead_letter) === "true" || undefined,
    evaluation_id: first(sp.evaluation_id),
    task_id: first(sp.task_id),
  };
  return <RunsView initial={initial} />;
}
