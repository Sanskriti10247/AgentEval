import { CompareView } from "@/components/views/evaluations";

export const metadata = { title: "Compare evaluations" };

export default async function Page({ searchParams }: { searchParams: Promise<{ ids?: string | string[] }> }) {
  const { ids } = await searchParams;
  const raw = Array.isArray(ids) ? ids.join(",") : (ids ?? "");
  const list = [...new Set(raw.split(",").map((s) => s.trim()).filter(Boolean))].slice(0, 5);
  return <CompareView ids={list} />;
}
