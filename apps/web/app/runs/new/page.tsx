import { NewRunView } from "@/components/launch/new-run";

export const metadata = { title: "Start a run" };

type Search = Record<string, string | string[] | undefined>;
const first = (v: string | string[] | undefined) => (Array.isArray(v) ? v[0] : v);

export default async function Page({ searchParams }: { searchParams: Promise<Search> }) {
  const sp = await searchParams;
  return <NewRunView initialVersionId={first(sp.agent_version_id)} initialTaskId={first(sp.task_id)} />;
}
