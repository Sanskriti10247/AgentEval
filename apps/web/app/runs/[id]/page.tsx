import { RunDetailView } from "@/components/views/run-detail";

export const metadata = { title: "Run" };

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <RunDetailView id={id} />;
}
