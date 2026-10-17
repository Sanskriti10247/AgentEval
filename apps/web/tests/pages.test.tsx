import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { AgentDetailView, AgentsView } from "@/components/views/agents";
import { DashboardView } from "@/components/views/dashboard";
import { CompareView, EvaluationDetailView, EvaluationsView } from "@/components/views/evaluations";
import { RunDetailView } from "@/components/views/run-detail";
import { RunsView } from "@/components/views/runs";
import { TasksView } from "@/components/views/tasks";
import { WorkersView } from "@/components/views/workers";
import { getApiKey, setApiKey } from "@/lib/auth";
import * as F from "./fixtures";
import { mockApi, renderWithClient, respond } from "./helpers";

const push = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push }),
  usePathname: () => "/",
}));

function apiDown() {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("fetch failed")));
}

async function expectApiDownMessage() {
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("Could not load data");
  expect(alert).toHaveTextContent("Cannot reach the API");
}

// Shared lookup routes used by several pages.
const lookups = {
  "/agents": { agents: [F.agent], next_after: null },
  [`/agents/${F.IDS.agent}/versions`]: [F.version],
  "/datasets": { datasets: [F.dataset], next_after: null },
};

describe("Dashboard", () => {
  it("renders KPIs from /stats/overview and requests the chosen window", async () => {
    const { calls } = mockApi({ "/stats/overview": F.overview });
    renderWithClient(<DashboardView />);
    expect(await screen.findByText("75%")).toBeInTheDocument();
    expect(screen.getByText("Success rate")).toBeInTheDocument();
    expect(screen.getByText("7.2 s")).toBeInTheDocument(); // p95
    expect(screen.getByText("$0.0700")).toBeInTheDocument();
    expect(screen.getByText(/1 queued, 1 retrying, 3 grading/)).toBeInTheDocument();
    const table = screen.getByRole("table", { name: "Runs by status" });
    expect(within(table).getByText("CANCELLED")).toBeInTheDocument();
    expect(calls[0]!.url.searchParams.get("window_h")).toBe("24");

    await userEvent.selectOptions(screen.getByLabelText("Window"), "168");
    await waitFor(() => expect(calls.some((c) => c.url.searchParams.get("window_h") === "168")).toBe(true));
  });

  it("shows dashes and zeros for an empty database", async () => {
    mockApi({ "/stats/overview": F.emptyOverview });
    renderWithClient(<DashboardView />);
    expect(await screen.findByText("Success rate")).toBeInTheDocument();
    expect(screen.getAllByText("–").length).toBeGreaterThanOrEqual(3);
  });

  it("shows a readable error when the API is down", async () => {
    apiDown();
    renderWithClient(<DashboardView />);
    await expectApiDownMessage();
  });
});

describe("Agents", () => {
  it("lists agents with their latest version", async () => {
    mockApi({ "/agents": { agents: [F.agent], next_after: null } });
    renderWithClient(<AgentsView />);
    expect(await screen.findByRole("link", { name: "coder-agent" })).toHaveAttribute("href", `/agents/${F.IDS.agent}`);
    expect(screen.getByText("v2")).toBeInTheDocument();
    expect(screen.getByText("claude-test-model")).toBeInTheDocument();
    expect(screen.getByText("run_tests")).toBeInTheDocument();
  });

  it("loads more with the next_after cursor", async () => {
    const { calls } = mockApi({
      "/agents": (u: URL) =>
        u.searchParams.get("after")
          ? { agents: [{ ...F.agent, id: "x2", name: "second-agent" }], next_after: null }
          : { agents: [F.agent], next_after: "cursor-1" },
    });
    renderWithClient(<AgentsView />);
    await userEvent.click(await screen.findByRole("button", { name: "Load more" }));
    expect(await screen.findByText("second-agent")).toBeInTheDocument();
    expect(calls.at(-1)!.url.searchParams.get("after")).toBe("cursor-1");
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
  });

  it("shows an empty state", async () => {
    mockApi({ "/agents": { agents: [], next_after: null } });
    renderWithClient(<AgentsView />);
    expect(await screen.findByText(/No agents yet/)).toBeInTheDocument();
  });

  it("shows an error state", async () => {
    apiDown();
    renderWithClient(<AgentsView />);
    await expectApiDownMessage();
  });
});

describe("Agent detail", () => {
  it("shows versions and the agent's evaluations with success rate", async () => {
    mockApi({
      ...lookups,
      [`/agents/${F.IDS.agent}`]: { id: F.IDS.agent, name: "coder-agent", created_at: F.agent.created_at },
      "/evaluations": { evaluations: [F.evaluation], next_after: null },
      [`/evaluations/${F.IDS.eval1}/summary`]: F.summary,
    });
    renderWithClient(<AgentDetailView id={F.IDS.agent} />);
    expect(await screen.findByRole("heading", { name: "coder-agent" })).toBeInTheDocument();
    const versions = await screen.findByRole("table", { name: "Agent versions" });
    expect(within(versions).getByText("anthropic")).toBeInTheDocument();
    expect(within(versions).getByText("python")).toBeInTheDocument();
    const evals = await screen.findByRole("table", { name: "Evaluations" });
    expect(await within(evals).findByText("75%")).toBeInTheDocument();
    expect(within(evals).getByRole("link", { name: "calc-demo" })).toBeInTheDocument();
  });

  it("shows an empty evaluations state", async () => {
    mockApi({
      ...lookups,
      [`/agents/${F.IDS.agent}`]: { id: F.IDS.agent, name: "coder-agent", created_at: F.agent.created_at },
      "/evaluations": { evaluations: [], next_after: null },
    });
    renderWithClient(<AgentDetailView id={F.IDS.agent} />);
    expect(await screen.findByText("No evaluations for this agent yet.")).toBeInTheDocument();
  });

  it("shows an error state for an unknown agent", async () => {
    mockApi({ "/agents": { agents: [], next_after: null } });
    renderWithClient(<AgentDetailView id="nope" />);
    expect(await screen.findByRole("alert")).toHaveTextContent("HTTP 404");
  });
});

describe("Tasks", () => {
  it("lists tasks and sends filters", async () => {
    const { calls } = mockApi({ "/tasks": { tasks: [F.task], next_after: null }, "/datasets": lookups["/datasets"] });
    renderWithClient(<TasksView />);
    expect(await screen.findByText("fix-auth-test")).toBeInTheDocument();
    expect(screen.getByText("has evaluation")).toBeInTheDocument();
    expect(await screen.findAllByText("coding-bench v1")).not.toHaveLength(0);
    await userEvent.selectOptions(screen.getByLabelText("Difficulty"), "hard");
    await userEvent.selectOptions(screen.getByLabelText("Type"), "agent");
    await waitFor(() => {
      const last = calls.filter((c) => c.url.pathname.endsWith("/tasks")).at(-1)!;
      expect(last.url.searchParams.get("difficulty")).toBe("hard");
      expect(last.url.searchParams.get("task_type")).toBe("agent");
    });
  });

  it("shows empty and error states", async () => {
    mockApi({ "/tasks": { tasks: [], next_after: null }, "/datasets": { datasets: [], next_after: null } });
    const first = renderWithClient(<TasksView />);
    expect(await screen.findByText("No tasks match these filters.")).toBeInTheDocument();
    first.unmount();
    apiDown();
    renderWithClient(<TasksView />);
    await expectApiDownMessage();
  });
});

describe("Evaluations list", () => {
  const routes = {
    ...lookups,
    "/evaluations": { evaluations: [F.evaluation, F.evaluation2], next_after: null },
    [`/evaluations/${F.IDS.eval1}/summary`]: F.summary,
    [`/evaluations/${F.IDS.eval2}/summary`]: respond(500, { title: "Boom", status: 500, detail: "x" }),
  };

  it("renders rows, resolves the agent and tolerates a failing summary", async () => {
    mockApi(routes);
    renderWithClient(<EvaluationsView />);
    expect(await screen.findByRole("link", { name: "calc-demo" })).toHaveAttribute("href", `/evaluations/${F.IDS.eval1}`);
    expect(await screen.findAllByRole("link", { name: "coder-agent" })).not.toHaveLength(0);
    expect(await screen.findByText("75%")).toBeInTheDocument();
    expect(screen.getAllByText("RUNNING").length).toBeGreaterThan(0);
  });

  it("enables Compare only for 2+ selections and navigates", async () => {
    mockApi(routes);
    renderWithClient(<EvaluationsView />);
    const button = screen.getByRole("button", { name: /Compare selected \(0\)/ });
    expect(button).toBeDisabled();
    await userEvent.click(await screen.findByLabelText("Select calc-demo for comparison"));
    await userEvent.click(screen.getByLabelText("Select calc-demo-b for comparison"));
    const ready = screen.getByRole("button", { name: /Compare selected \(2\)/ });
    expect(ready).toBeEnabled();
    await userEvent.click(ready);
    expect(push).toHaveBeenCalledWith(`/evaluations/compare?ids=${F.IDS.eval1},${F.IDS.eval2}`);
  });

  it("shows empty and error states", async () => {
    mockApi({ ...lookups, "/evaluations": { evaluations: [], next_after: null } });
    const first = renderWithClient(<EvaluationsView />);
    expect(await screen.findByText(/No evaluations yet/)).toBeInTheDocument();
    first.unmount();
    apiDown();
    renderWithClient(<EvaluationsView />);
    await expectApiDownMessage();
  });
});

describe("Evaluation detail", () => {
  const base = {
    ...lookups,
    [`/evaluations/${F.IDS.eval1}`]: F.evaluation,
    [`/evaluations/${F.IDS.eval1}/summary`]: F.summary,
    [`/evaluations/${F.IDS.eval1}/results`]: { results: [F.result], next_after: null },
  };

  it("renders summary, progress including RETRYING and CANCELLED, and results", async () => {
    mockApi(base);
    renderWithClient(<EvaluationDetailView id={F.IDS.eval1} />);
    expect(await screen.findByRole("heading", { name: "calc-demo" })).toBeInTheDocument();
    expect((await screen.findAllByText("75%")).length).toBeGreaterThan(0);
    expect(screen.getByText("$0.0400")).toBeInTheDocument();
    expect(screen.getAllByText("3 / 4").length).toBeGreaterThan(0);
    const runs = screen.getByRole("table", { name: "Runs by status" });
    expect(within(runs).getByText("RETRYING")).toBeInTheDocument();
    expect(within(runs).getByText("CANCELLED")).toBeInTheDocument();
    const results = await screen.findByRole("table", { name: "Run results" });
    expect(within(results).getByText("tests_failed")).toBeInTheDocument();
    expect(within(results).getByText("0.123")).toBeInTheDocument();
    expect(within(results).getByRole("link", { name: F.IDS.run.slice(0, 8) })).toHaveAttribute("href", `/runs/${F.IDS.run}`);
  });

  it("cancels the evaluation via POST", async () => {
    const { calls } = mockApi({ ...base, [`/evaluations/${F.IDS.eval1}/cancel`]: F.evaluation });
    renderWithClient(<EvaluationDetailView id={F.IDS.eval1} />);
    await userEvent.click(await screen.findByRole("button", { name: "Cancel evaluation" }));
    await waitFor(() => {
      const post = calls.find((c) => c.url.pathname.endsWith("/cancel"));
      expect(post?.init?.method).toBe("POST");
    });
  });

  it("disables cancel once the batch is complete", async () => {
    mockApi({
      ...base,
      [`/evaluations/${F.IDS.eval1}`]: { ...F.evaluation, progress: { ...F.evaluation.progress, complete: true } },
    });
    renderWithClient(<EvaluationDetailView id={F.IDS.eval1} />);
    expect(await screen.findByRole("button", { name: "Cancel evaluation" })).toBeDisabled();
  });

  it("shows the empty results state", async () => {
    mockApi({ ...base, [`/evaluations/${F.IDS.eval1}/results`]: { results: [], next_after: null } });
    renderWithClient(<EvaluationDetailView id={F.IDS.eval1} />);
    expect(await screen.findByText("No runs in this evaluation.")).toBeInTheDocument();
  });

  it("shows an error state", async () => {
    apiDown();
    renderWithClient(<EvaluationDetailView id={F.IDS.eval1} />);
    await expectApiDownMessage();
  });
});

describe("Compare", () => {
  it("renders success, latency, cost and failure categories side by side", async () => {
    const { calls } = mockApi({
      ...lookups,
      "/evaluations": { evaluations: [F.evaluation, F.evaluation2], next_after: null },
      "/evaluations/compare": F.compare,
    });
    renderWithClient(<CompareView ids={[F.IDS.eval1, F.IDS.eval2]} />);
    const table = await screen.findByRole("table", { name: "Evaluation comparison" });
    expect(within(table).getByText("75%")).toBeInTheDocument();
    expect(within(table).getByText("50%")).toBeInTheDocument();
    expect(within(table).getAllByText("$0.0400")).toHaveLength(2);
    const cats = screen.getByRole("table", { name: "Failure categories by evaluation" });
    expect(within(cats).getByText("wrong_answer")).toBeInTheDocument();
    expect(within(cats).getByText("tests_failed")).toBeInTheDocument();
    expect(screen.getByText(/-25 pt/)).toBeInTheDocument();
    expect(calls.find((c) => c.url.pathname.endsWith("/compare"))!.url.searchParams.get("ids")).toBe(`${F.IDS.eval1},${F.IDS.eval2}`);
  });

  it("asks for at least two evaluations", () => {
    mockApi({});
    renderWithClient(<CompareView ids={[F.IDS.eval1]} />);
    expect(screen.getByText(/Pick 2 to 5 evaluations/)).toBeInTheDocument();
  });

  it("shows the API's 422 message", async () => {
    mockApi({
      ...lookups,
      "/evaluations": { evaluations: [], next_after: null },
      "/evaluations/compare": respond(422, { detail: [{ type: "value_error", loc: ["query", "ids"], msg: "evaluations must share a dataset" }] }),
    });
    renderWithClient(<CompareView ids={[F.IDS.eval1, F.IDS.eval2]} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("evaluations must share a dataset");
  });
});

describe("Runs list", () => {
  it("lists runs and sends the dead-letter filter", async () => {
    const { calls } = mockApi({ "/runs": { runs: [F.run], next_after: null } });
    renderWithClient(<RunsView />);
    expect(await screen.findByRole("link", { name: F.IDS.run.slice(0, 8) })).toHaveAttribute("href", `/runs/${F.IDS.run}`);
    const table = screen.getByRole("table", { name: "Runs" });
    expect(within(table).getByText("FAILED")).toBeInTheDocument();
    expect(within(table).getByText("2/2")).toBeInTheDocument();
    expect(screen.getByText("retries exhausted")).toBeInTheDocument();
    await userEvent.click(screen.getByLabelText("Dead-letter only"));
    await waitFor(() => expect(calls.at(-1)!.url.searchParams.get("dead_letter")).toBe("true"));
    await userEvent.selectOptions(screen.getByLabelText("Status"), "RETRYING");
    await waitFor(() => expect(calls.at(-1)!.url.searchParams.get("status")).toBe("RETRYING"));
  });

  it("applies initial filters from the URL", async () => {
    const { calls } = mockApi({ "/runs": { runs: [], next_after: null } });
    renderWithClient(<RunsView initial={{ evaluation_id: F.IDS.eval1, dead_letter: true }} />);
    expect(await screen.findByText("No runs match these filters.")).toBeInTheDocument();
    expect(calls[0]!.url.searchParams.get("evaluation_id")).toBe(F.IDS.eval1);
    expect(calls[0]!.url.searchParams.get("dead_letter")).toBe("true");
  });

  it("shows an error state", async () => {
    apiDown();
    renderWithClient(<RunsView />);
    await expectApiDownMessage();
  });
});

describe("Run detail", () => {
  const eventsPath = `/runs/${F.IDS.run}/attempts/2/events`;
  const routes = {
    [`/runs/${F.IDS.run}`]: F.run,
    [`/runs/${F.IDS.run}/attempts`]: [{ ...F.attempt, attempt_no: 1, error_message: "first try failed" }, F.attempt],
    [`/runs/${F.IDS.run}/attempts/2/evaluation`]: F.attemptEvaluation,
    [eventsPath]: { events: F.events, next_after_seq: null },
  };

  it("shows status, attempts, trajectory with errors and durations, and the evaluation", async () => {
    mockApi(routes);
    renderWithClient(<RunDetailView id={F.IDS.run} />);
    expect((await screen.findAllByText("model overloaded")).length).toBeGreaterThan(0);
    const timeline = await screen.findByRole("list", { name: "Attempt timeline" });
    expect(within(timeline).getAllByRole("listitem")).toHaveLength(2);
    expect(within(timeline).getByText("first try failed")).toBeInTheDocument();
    const traj = await screen.findByRole("table", { name: "Trajectory events" });
    expect(within(traj).getByText("run_tests")).toBeInTheDocument();
    expect(within(traj).getByText("ToolTimeout")).toBeInTheDocument();
    expect(within(traj).getByText("842 ms")).toBeInTheDocument();
    expect(within(traj).getByText("30.0 s")).toBeInTheDocument();
    const score = await screen.findByRole("table", { name: "Score components" });
    expect(within(score).getByText("0.123")).toBeInTheDocument();
    expect(screen.getByText("tests_failed")).toBeInTheDocument();
  });

  it("switches attempts and pages events by after_seq", async () => {
    const { calls } = mockApi({
      ...routes,
      [`/runs/${F.IDS.run}/attempts/1/evaluation`]: respond(404, { title: "Not Found", status: 404, detail: "x" }),
      [`/runs/${F.IDS.run}/attempts/1/events`]: { events: [], next_after_seq: null },
      [eventsPath]: (u: URL) =>
        u.searchParams.get("after_seq") === "-1"
          ? { events: [F.events[0]], next_after_seq: 0 }
          : { events: [F.events[1]], next_after_seq: null },
    });
    renderWithClient(<RunDetailView id={F.IDS.run} />);
    await userEvent.click(await screen.findByRole("button", { name: "Load more" }));
    expect(await screen.findByText("ToolTimeout")).toBeInTheDocument();
    expect(calls.filter((c) => c.url.pathname.endsWith("/events")).at(-1)!.url.searchParams.get("after_seq")).toBe("0");

    await userEvent.click(screen.getByRole("button", { name: /#1/ }));
    expect(await screen.findByText("No trajectory events for this attempt.")).toBeInTheDocument();
    expect(await screen.findByText(/No evaluation recorded/)).toBeInTheDocument();
  });

  it("replays a FAILED run with the chosen number of extra attempts", async () => {
    const { calls } = mockApi({ ...routes, [`/runs/${F.IDS.run}/replay`]: { ...F.run, status: "QUEUED" } });
    renderWithClient(<RunDetailView id={F.IDS.run} />);
    const replay = await screen.findByRole("button", { name: "Replay" });
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Additional attempts"), { target: { value: "2" } });
    await userEvent.click(replay);
    await waitFor(() => {
      const post = calls.find((c) => c.url.pathname.endsWith("/replay"));
      expect(JSON.parse(String(post?.init?.body))).toEqual({ additional_attempts: 2 });
    });
  });

  it("cancels a RUNNING run and disables Replay", async () => {
    const id = F.runningRun.id;
    const { calls } = mockApi({
      [`/runs/${id}`]: F.runningRun,
      [`/runs/${id}/attempts`]: [],
      [`/runs/${id}/cancel`]: { ...F.runningRun, cancel_requested_at: "2026-10-17T10:00:10Z", cancel_reason: "user" },
    });
    renderWithClient(<RunDetailView id={id} />);
    expect(await screen.findByRole("button", { name: "Replay" })).toBeDisabled();
    expect(await screen.findByText("No attempts yet.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(calls.some((c) => c.url.pathname.endsWith("/cancel") && c.init?.method === "POST")).toBe(true));
  });

  it("surfaces a 409 from replay", async () => {
    mockApi({ ...routes, [`/runs/${F.IDS.run}/replay`]: respond(409, { title: "Conflict", status: 409, detail: "run is not FAILED" }) });
    renderWithClient(<RunDetailView id={F.IDS.run} />);
    await userEvent.click(await screen.findByRole("button", { name: "Replay" }));
    expect(await screen.findByText(/run is not FAILED/)).toBeInTheDocument();
  });

  it("explains a 403 on replay: the key lacks the write scope", async () => {
    setApiKey("ae_0a1b2c3d_readonlykey");
    mockApi({
      ...routes,
      [`/runs/${F.IDS.run}/replay`]: respond(403, { title: "Forbidden", status: 403, detail: "This API key lacks the write scope" }),
    });
    renderWithClient(<RunDetailView id={F.IDS.run} />);
    await userEvent.click(await screen.findByRole("button", { name: "Replay" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Not permitted");
    expect(alert).toHaveTextContent("This API key lacks the write scope");
    expect(getApiKey()).toBe("ae_0a1b2c3d_readonlykey"); // a 403 does not sign the user out
  });

  it("explains a 403 on cancel", async () => {
    const id = F.runningRun.id;
    mockApi({
      [`/runs/${id}`]: F.runningRun,
      [`/runs/${id}/attempts`]: [],
      [`/runs/${id}/cancel`]: respond(403, { title: "Forbidden", status: 403, detail: "This API key lacks the write scope" }),
    });
    renderWithClient(<RunDetailView id={id} />);
    await userEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    expect(await screen.findByText(/lacks the write scope/)).toBeInTheDocument();
  });

  it("shows an error state", async () => {
    apiDown();
    renderWithClient(<RunDetailView id={F.IDS.run} />);
    await expectApiDownMessage();
  });
});

describe("Workers", () => {
  it("shows live and stopped workers with heartbeat age", async () => {
    mockApi({ "/workers": { workers: [F.worker, F.stoppedWorker], next_after: null } });
    renderWithClient(<WorkersView />);
    expect(await screen.findByText("worker-alpha")).toBeInTheDocument();
    expect(screen.getByText("live")).toBeInTheDocument();
    expect(screen.getByText("stopped")).toBeInTheDocument();
    expect(screen.getByText("shutdown")).toBeInTheDocument();
    expect(screen.getByText(/1 live of 2 listed/)).toBeInTheDocument();
    expect(screen.getAllByText("4")).toHaveLength(2); // concurrency of both workers
  });

  it("shows empty and error states", async () => {
    mockApi({ "/workers": { workers: [], next_after: null } });
    const first = renderWithClient(<WorkersView />);
    expect(await screen.findByText("No workers have registered yet.")).toBeInTheDocument();
    first.unmount();
    apiDown();
    renderWithClient(<WorkersView />);
    await expectApiDownMessage();
  });
});
