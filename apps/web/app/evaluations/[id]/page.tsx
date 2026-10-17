import { EvaluationDetailView } from "@/components/views/evaluations";

export const metadata = { title: "Evaluation" };

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <EvaluationDetailView id={id} />;
}
