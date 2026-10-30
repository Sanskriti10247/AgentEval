import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { AgentDetailView } from "@/components/views/agents";
import { DashboardView } from "@/components/views/dashboard";
import { CompareView, EvaluationDetailView } from "@/components/views/evaluations";
import { RunDetailView } from "@/components/views/run-detail";
import * as F from "./fixtures";
import { mockApi, renderWithClient, respond } from "./helpers";

const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }), usePathname: () => "/" }));

const lookups = {
  "/agents": { agents: [F.agent], next_after: null },
  [`/agents/${F.IDS.agent}/versions`]: [F.version],
  "/datasets": { datasets: [F.dataset], next_after: null },
};

/** The Readouts cell whose label is `label`. */
const stat = (label: string) => screen.getByText(label).closest("div")!;

describe("Dashboard with rates, cost split and activity", () => {
  it("renders the KPIs with their denominators", async () => {
    mockApi({ "/stats/overview": F.overviewFull, "/stats/activity": F.activity });
    renderWithClient(<DashboardView />);
    await screen.findByText("Completion rate");
    expect(stat("Completion rate")).toHaveTextContent("87.5%");
    expect(stat("Completion rate")).toHaveTextContent("7 of 8 finished runs completed");
    expect(stat("Failure rate")).toHaveTextContent("1 of 8 finished runs failed");
    expect(stat("Timeouts / retries")).toHaveTextContent("12.5% / 25%");
    expect(stat("Latency p99")).toHaveTextContent("9.1 s");
    expect(stat("Latency p50")).toHaveTextContent("n = 8 finished runs");
    expect(stat("Real cost")).toHaveTextContent("$0.0200");
    expect(stat("Simulated cost")).toHaveTextContent("$0.0500");
    expect(stat("Simulated cost")).toHaveTextContent("nothing was spent");
    expect(stat("Oldest queued run")).toHaveTextContent("1m");
    expect(stat("Workers alive")).toHaveTextContent("1 stale");
  });

  it("draws activity and recent evaluations from /stats/activity, with tables behind them", async () => {
    const { calls } = mockApi({ "/stats/overview": F.overviewFull, "/stats/activity": F.activity });
    renderWithClient(<DashboardView />);
    expect(await screen.findByText("9 runs, one bar per hour, UTC")).toBeInTheDocument();
    const buckets = screen.getByRole("table", { name: "Finished runs per bucket" });
    expect(within(buckets).getAllByRole("row")).toHaveLength(3);
    expect(within(buckets).getByText("4 / 5")).toBeInTheDocument();

    expect(screen.getByText(/2 evaluations, 12 graded attempts/)).toBeInTheDocument();
    const recent = screen.getByRole("table", { name: "Recent evaluations" });
    expect(within(recent).getByRole("link", { name: "calc-demo" })).toHaveAttribute("href", `/evaluations/${F.IDS.eval1}`);
    expect(within(recent).getByText("Simulated")).toBeInTheDocument();
    expect(within(recent).getByText("Real model")).toBeInTheDocument();
    expect(screen.getByText("simulated (fake model)")).toBeInTheDocument();

    await userEvent.selectOptions(screen.getByLabelText("Window"), "1");
    await waitFor(() =>
      expect(calls.some((c) => c.url.pathname.endsWith("/stats/activity") && c.url.searchParams.get("window_h") === "1")).toBe(true),
    );
  });

  it("shows empty states when nothing happened in the window", async () => {
    mockApi({ "/stats/overview": F.overviewFull, "/stats/activity": F.emptyActivity });
    renderWithClient(<DashboardView />);
    expect(await screen.findByText("No runs finished in the last 24 hours.")).toBeInTheDocument();
    expect(screen.getByText("No evaluations were created in the last 24 hours.")).toBeInTheDocument();
  });

  it("keeps the KPIs when /stats/activity fails, and says why the charts are missing", async () => {
    mockApi({ "/stats/overview": F.overviewFull, "/stats/activity": respond(500, { title: "Boom", status: 500, detail: "db down" }) });
    renderWithClient(<DashboardView />);
    expect(await screen.findByText("Completion rate")).toBeInTheDocument();
    const alerts = await screen.findAllByRole("alert");
    expect(alerts[0]).toHaveTextContent("db down");
  });

  it("treats a 404 from /stats/activity as an older API", async () => {
    mockApi({ "/stats/overview": F.overview });
    renderWithClient(<DashboardView />);
    expect((await screen.findAllByText("Not available on this API")).length).toBeGreaterThan(0);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("Agent versions", () => {
  it("labels simulated and real versions and diffs two of them", async () => {
    mockApi({
      ...lookups,
      [`/agents/${F.IDS.agent}`]: { id: F.IDS.agent, name: "coder-agent", created_at: F.agent.created_at },
      [`/agents/${F.IDS.agent}/versions`]: [F.version, F.fakeVersion],
      "/evaluations": { evaluations: [], next_after: null },
    });
    renderWithClient(<AgentDetailView id={F.IDS.agent} />);
    const versions = await screen.findByRole("list", { name: "Agent versions" });
    expect(within(versions).getByText("Simulated")).toBeInTheDocument();
    expect(within(versions).getByText("Real model")).toBeInTheDocument();
    expect(within(versions).getByText("max_cost_usd")).toBeInTheDocument();

    const diff = screen.getByRole("table", { name: "Version differences" });
    const rows = within(diff).getAllByRole("row").map((r) => r.textContent);
    expect(rows).toContain("provideranthropicfake");
    expect(rows).toContain("limits.max_steps2030");
    expect(rows).toContain("limits.max_cost_usdnot set1.00");
    expect(rows.some((r) => r?.startsWith("tools"))).toBe(false); // unchanged fields are left out
  });
});

describe("Evaluation detail context", () => {
  it("shows what ran, rates with counts, latency with n, and labels cost as simulated", async () => {
    mockApi({
      ...lookups,
      [`/evaluations/${F.IDS.eval1}`]: F.evaluationWithContext,
      [`/evaluations/${F.IDS.eval1}/summary`]: F.summaryFull,
      [`/evaluations/${F.IDS.eval1}/results`]: { results: [F.result], next_after: null },
    });
    renderWithClient(<EvaluationDetailView id={F.IDS.eval1} />);
    expect(await screen.findByText("grader-1.4")).toBeInTheDocument();
    expect(screen.getByText("fake/fake-model")).toBeInTheDocument();
    expect(screen.getByText("coding-bench v1, 1 task")).toBeInTheDocument();
    expect(screen.getByText("none (simulated, no spend)")).toBeInTheDocument();
    expect(screen.getByText("Spent (simulated)")).toBeInTheDocument();
    expect(await screen.findByText("Cost / task (simulated)")).toBeInTheDocument();
    expect(stat("Completion rate")).toHaveTextContent("1 of 2 finished runs; 3 scheduled");
    expect(stat("Latency p99")).toHaveTextContent("7.9 s");
    expect(stat("Latency p50")).toHaveTextContent("n = 4 attempts");
  });
});

describe("Compare with warnings and differences", () => {
  it("shows the API's warnings, what differs, and a not-distinguishable note", async () => {
    mockApi({
      ...lookups,
      "/evaluations": { evaluations: [F.evaluation, F.evaluation2], next_after: null },
      "/evaluations/compare": F.compareFull,
    });
    renderWithClient(<CompareView ids={[F.IDS.eval1, F.IDS.eval2]} />);
    const warn = await screen.findByRole("alert");
    expect(warn).toHaveTextContent("mix simulated and real models");
    expect(warn).toHaveTextContent("only 4 graded attempts");
    expect(screen.getByText("coder-agent v3: not distinguishable from coder-agent v2 at 95%")).toBeInTheDocument();

    const diff = screen.getByRole("table", { name: "Configuration differences" });
    expect(within(diff).getByText("claude-test-model")).toBeInTheDocument();
    expect(within(diff).getByText("provider")).toBeInTheDocument();

    const table = screen.getByRole("table", { name: "Evaluation comparison" });
    const row = within(table).getByText("Differs from first at 95%").closest("tr")!;
    expect(row).toHaveTextContent("baseline");
    expect(row).toHaveTextContent("no");
  });
});

describe("Run detail while live", () => {
  it("flags a failed refresh but keeps the last data on screen", async () => {
    let calls = 0;
    const id = F.runningRun.id;
    mockApi({
      [`/runs/${id}`]: () => (++calls === 1 ? F.runningRun : respond(503, { title: "Service Unavailable", status: 503, detail: "upstream down" })),
      [`/runs/${id}/attempts`]: [],
    });
    renderWithClient(<RunDetailView id={id} />);
    expect(await screen.findByText(/updated \d+s ago/)).toBeInTheDocument();
    expect(screen.getByText("Elapsed")).toBeInTheDocument();
    // The run polls every 3 s; the second answer is the failure.
    expect(await screen.findByText("Lost contact with the API", {}, { timeout: 6_000 })).toBeInTheDocument();
    expect(screen.getByText(/upstream down/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeInTheDocument(); // the page did not blank out
  }, 10_000);

  it("explains a queued run with queue depth and live workers", async () => {
    const id = F.runningRun.id;
    mockApi({
      [`/runs/${id}`]: { ...F.runningRun, status: "QUEUED", started_at: null, claimed_at: null, worker_id: null },
      [`/runs/${id}/attempts`]: [],
      "/stats/overview": F.overviewFull,
    });
    renderWithClient(<RunDetailView id={id} />);
    expect(await screen.findByText(/^Queued for \S+, waiting for a worker$/)).toBeInTheDocument();
    expect(await screen.findByText(/1 run queued in total, the oldest for 1m\. 2 workers alive, 1 stale\./)).toBeInTheDocument();
    expect(screen.getByText("not started")).toBeInTheDocument();
  });

  it("lists the retry history of a run that needed more than one attempt", async () => {
    mockApi({
      [`/runs/${F.IDS.run}`]: F.run,
      [`/runs/${F.IDS.run}/attempts`]: [{ ...F.attempt, attempt_no: 1, id: "a1", error_message: "first try failed" }, F.attempt],
      [`/runs/${F.IDS.run}/attempts/2/evaluation`]: F.attemptEvaluation,
      [`/runs/${F.IDS.run}/attempts/2/events`]: { events: [], next_after_seq: null },
    });
    renderWithClient(<RunDetailView id={F.IDS.run} />);
    const history = await screen.findByRole("list", { name: "Retry history" });
    const items = within(history).getAllByRole("listitem");
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveTextContent("#1 finished, MODEL_ERROR after 3.4 s (first try failed) → retried");
    expect(items[1]).not.toHaveTextContent("retried");
  });
});
