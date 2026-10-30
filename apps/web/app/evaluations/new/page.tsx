import { NewEvaluationView } from "@/components/launch/new-evaluation";

export const metadata = { title: "New evaluation" };

type Search = Record<string, string | string[] | undefined>;
const first = (v: string | string[] | undefined) => (Array.isArray(v) ? v[0] : v);

export default async function Page({ searchParams }: { searchParams: Promise<Search> }) {
  const sp = await searchParams;
  return <NewEvaluationView initialVersionId={first(sp.agent_version_id)} />;
}
