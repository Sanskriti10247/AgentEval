import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { AuthGate } from "@/components/auth-gate";
import { Nav } from "@/components/nav";
import { EvaluationDetailView } from "@/components/views/evaluations";
import { RunDetailView } from "@/components/views/run-detail";
import * as F from "./fixtures";
import { mockApi, renderWithClient } from "./helpers";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }), usePathname: () => "/" }));

// GET /health of an API started with AGENTEVAL_READ_ONLY=true.
const READ_ONLY = { "/health": { status: "ok", read_only: true } };

describe("a read-only API", () => {
  it("shows pages without asking for a key, and sends none", async () => {
    const { calls } = mockApi({ ...READ_ONLY, "/stats/overview": F.overview });
    renderWithClient(
      <>
        <Nav />
        <AuthGate>
          <div>secret page</div>
        </AuthGate>
      </>,
    );
    expect(await screen.findByText("secret page")).toBeInTheDocument();
    expect(screen.queryByRole("form", { name: "API key" })).not.toBeInTheDocument();
    expect(screen.getByText(/Read-only demo/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sign out" })).not.toBeInTheDocument();
    expect(calls.every((c) => !(c.init?.headers as Record<string, string> | undefined)?.Authorization)).toBe(true);
  });

  it("still asks for a key when /health does not say read-only", async () => {
    mockApi({ "/health": { status: "ok", read_only: false } });
    renderWithClient(
      <AuthGate>
        <div>secret page</div>
      </AuthGate>,
    );
    expect(await screen.findByRole("form", { name: "API key" })).toBeInTheDocument();
    expect(screen.queryByText("secret page")).not.toBeInTheDocument();
  });

  it("hides Cancel and Replay on a run", async () => {
    mockApi({
      ...READ_ONLY,
      [`/runs/${F.IDS.run}`]: F.run,
      [`/runs/${F.IDS.run}/attempts`]: [F.attempt],
    });
    renderWithClient(
      <>
        <Nav />
        <RunDetailView id={F.IDS.run} />
      </>,
    );
    // The Nav note appears once /health has answered; the run's status once it has loaded.
    expect(await screen.findByText(/Read-only demo/)).toBeInTheDocument();
    expect((await screen.findAllByText(F.run.status)).length).toBeGreaterThan(0);
    expect(screen.queryByRole("button", { name: "Replay" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel" })).not.toBeInTheDocument();
  });

  it("hides Cancel evaluation", async () => {
    mockApi({
      ...READ_ONLY,
      "/agents": { agents: [F.agent], next_after: null },
      [`/agents/${F.IDS.agent}/versions`]: [F.version],
      "/datasets": { datasets: [F.dataset], next_after: null },
      [`/evaluations/${F.IDS.eval1}`]: F.evaluation,
      [`/evaluations/${F.IDS.eval1}/summary`]: F.summary,
      [`/evaluations/${F.IDS.eval1}/results`]: { results: [F.result], next_after: null },
    });
    renderWithClient(
      <>
        <Nav />
        <EvaluationDetailView id={F.IDS.eval1} />
      </>,
    );
    expect(await screen.findByText(/Read-only demo/)).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: F.evaluation.name })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel evaluation" })).not.toBeInTheDocument();
  });
});
