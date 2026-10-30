import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { AuthGate } from "@/components/auth-gate";
import { NewEvaluationView } from "@/components/launch/new-evaluation";
import { NewRunView } from "@/components/launch/new-run";
import { Nav } from "@/components/nav";
import { EvaluationsView } from "@/components/views/evaluations";
import { RunsView } from "@/components/views/runs";
import * as F from "./fixtures";
import { mockApi, renderWithClient, respond } from "./helpers";

const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }), usePathname: () => "/" }));

// GET /health of an API started in public demo mode.
const PUBLIC_DEMO = {
  "/health": { status: "ok", read_only: false, public_demo: true, demo_limits: { max_repetitions: 3, max_active_runs: 20, concurrent_runs: 1 } },
};

const olderVersion = { ...F.version, id: "b0000000-0000-4000-8000-000000000000", version: 1, model: "older-model" };
const commandTask = { ...F.task, id: "c0000000-0000-4000-8000-000000000009", name: "echo-hello", task_type: "command", difficulty: "easy" };

const estimate = (_u: URL, init?: RequestInit) => F.estimateFor(JSON.parse(String(init?.body)));

const catalogue = {
  "/agents": { agents: [F.agent], next_after: null },
  [`/agents/${F.IDS.agent}/versions`]: [olderVersion, F.version],
  "/datasets": { datasets: [{ ...F.dataset, task_count: 5 }], next_after: null },
  "/tasks": { tasks: [F.task, commandTask], next_after: null },
  "/providers": F.providers,
  "/evaluations/estimate": estimate,
};

function postTo(calls: { url: URL; init?: RequestInit }[], path: string) {
  return calls.find((c) => c.url.pathname === `/api/v1${path}` && c.init?.method === "POST");
}

describe("public demo mode", () => {
  it("skips the key screen, sends no key and says it is a public demo", async () => {
    const { calls } = mockApi({ ...PUBLIC_DEMO, "/stats/overview": F.overview });
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
    expect(screen.getByText(/Public demo\./)).toBeInTheDocument();
    expect(screen.getByText(/up to 3 repetitions each/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sign out" })).not.toBeInTheDocument();
    expect(calls.every((c) => !(c.init?.headers as Record<string, string> | undefined)?.Authorization)).toBe(true);
  });

  it("gives way to a read_only flag: no launch buttons", async () => {
    mockApi({
      "/health": { status: "ok", read_only: true, public_demo: true, demo_limits: null },
      ...catalogue,
      "/evaluations": { evaluations: [], next_after: null },
    });
    renderWithClient(<EvaluationsView />);
    await screen.findByRole("heading", { name: "Evaluations" });
    await waitFor(() => expect(screen.queryByRole("link", { name: "New evaluation" })).not.toBeInTheDocument());
  });
});

describe("New evaluation", () => {
  it("defaults to the latest version and the first frozen dataset, and posts the exact payload", async () => {
    push.mockClear();
    const created = { ...F.evaluation, id: "e0000000-0000-4000-8000-0000000000aa" };
    const { calls } = mockApi({ ...PUBLIC_DEMO, ...catalogue, "/evaluations": respond(202, created) });
    renderWithClient(<NewEvaluationView />);

    const form = await screen.findByRole("form", { name: "New evaluation" });
    expect(screen.getByLabelText("Agent version")).toHaveValue(F.IDS.version); // v2, not v1
    expect(screen.getByLabelText("Name")).toHaveValue("coder-agent v2 on coding-bench v1");
    expect(form).toHaveTextContent("This queues 5 runs");

    fireEvent.change(screen.getByLabelText("Repetitions"), { target: { value: "2" } });
    expect(form).toHaveTextContent("This queues 10 runs");
    expect(form).toHaveTextContent("This instance runs one at a time");
    await userEvent.click(screen.getByRole("button", { name: "Launch evaluation" }));

    await waitFor(() => expect(push).toHaveBeenCalledWith(`/evaluations/${created.id}`));
    const post = postTo(calls, "/evaluations")!;
    expect(JSON.parse(String(post.init?.body))).toEqual({
      name: "coder-agent v2 on coding-bench v1",
      agent_version_id: F.IDS.version,
      dataset_id: F.IDS.dataset,
      repetitions: 2,
    });
    expect((post.init?.headers as Record<string, string>)["Idempotency-Key"]).toMatch(/\S{16,}/);
    // Only frozen datasets are offered.
    expect(calls.some((c) => c.url.pathname.endsWith("/datasets") && c.url.searchParams.get("status") === "FROZEN")).toBe(true);
  });

  it("preselects the version from the URL and keeps a typed name", async () => {
    const { calls } = mockApi({ ...PUBLIC_DEMO, ...catalogue, "/evaluations": respond(202, F.evaluation) });
    renderWithClient(<NewEvaluationView initialVersionId={olderVersion.id} />);
    const name = await screen.findByLabelText("Name");
    expect(screen.getByLabelText("Agent version")).toHaveValue(olderVersion.id);
    await userEvent.clear(name);
    await userEvent.type(name, "baseline");
    await userEvent.click(screen.getByRole("button", { name: "Launch evaluation" }));
    await waitFor(() => expect(postTo(calls, "/evaluations")).toBeDefined());
    expect(JSON.parse(String(postTo(calls, "/evaluations")!.init?.body))).toMatchObject({
      name: "baseline",
      agent_version_id: olderVersion.id,
      repetitions: 1,
    });
  });

  it("validates inline and does not post: repetitions over the demo limit, empty name", async () => {
    const { calls } = mockApi({ ...PUBLIC_DEMO, ...catalogue });
    renderWithClient(<NewEvaluationView />);
    const reps = await screen.findByLabelText("Repetitions");
    fireEvent.change(reps, { target: { value: "4" } });
    await userEvent.clear(screen.getByLabelText("Name"));
    await userEvent.click(screen.getByRole("button", { name: "Launch evaluation" }));
    expect(await screen.findByText("Enter a whole number from 1 to 3.")).toBeInTheDocument();
    expect(screen.getByText("Give the evaluation a name.")).toBeInTheDocument();
    expect(reps).toHaveAttribute("aria-invalid", "true");
    expect(postTo(calls, "/evaluations")).toBeUndefined();
  });

  it.each([
    [403, "Not permitted", "Creating evaluations is not allowed with this key"],
    [409, "Not possible right now", "The demo already has 4 active runs"],
    [429, "Too many requests", "Rate limit exceeded"],
  ])("shows a %i from the API as a plain inline message", async (status, title, detail) => {
    push.mockClear();
    mockApi({ ...PUBLIC_DEMO, ...catalogue, "/evaluations": respond(status, { title: "x", status, detail }) });
    renderWithClient(<NewEvaluationView />);
    await userEvent.click(await screen.findByRole("button", { name: "Launch evaluation" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(title);
    expect(alert).toHaveTextContent(detail);
    expect(push).not.toHaveBeenCalled();
  });
});

describe("New evaluation: providers, estimate and budget", () => {
  const withFake = {
    ...catalogue,
    [`/agents/${F.IDS.agent}/versions`]: [olderVersion, F.version, F.fakeVersion],
  };

  it("previews a simulated evaluation: no spend, no budget field", async () => {
    const { calls } = mockApi({ ...PUBLIC_DEMO, ...withFake });
    renderWithClient(<NewEvaluationView />);
    await screen.findByRole("form", { name: "New evaluation" });
    expect(screen.getByLabelText("Agent version")).toHaveValue(F.fakeVersion.id); // v3 is the latest
    const preview = await screen.findByRole("group", { name: "Estimate" });
    await waitFor(() => expect(preview).toHaveTextContent("$5.00 simulated, no real spend"));
    expect(preview).toHaveTextContent("5 tasks × 1 repetitions = 5");
    expect(preview).toHaveTextContent("at most 150");
    expect(preview).not.toHaveTextContent("one at a time");
    expect(screen.queryByLabelText("Budget (USD)")).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Repetitions"), { target: { value: "2" } });
    await waitFor(() => expect(preview).toHaveTextContent("at most 300"));
    const asked = calls.filter((c) => c.url.pathname.endsWith("/evaluations/estimate")).at(-1)!;
    expect(asked.init?.method).toBe("POST");
    expect(JSON.parse(String(asked.init?.body))).toEqual({ agent_version_id: F.fakeVersion.id, dataset_id: F.IDS.dataset, repetitions: 2 });
  });

  it("previews a real-provider evaluation and sends the budget typed in", async () => {
    push.mockClear();
    const { calls } = mockApi({ ...PUBLIC_DEMO, ...withFake, "/evaluations": respond(202, F.evaluation) });
    renderWithClient(<NewEvaluationView initialVersionId={F.IDS.version} />);
    const budget = await screen.findByLabelText("Budget (USD)");
    expect(budget).toHaveAttribute("placeholder", "2.00");
    const preview = await screen.findByRole("group", { name: "Estimate" });
    await waitFor(() => expect(preview).toHaveTextContent("$5.00 upper bound"));
    expect(preview).toHaveTextContent("per-run cap $1.00");
    expect(preview).toHaveTextContent("stops at $2.00 (server default)");
    expect(preview).toHaveTextContent("one at a time per worker process");

    await userEvent.type(budget, "abc");
    await userEvent.click(screen.getByRole("button", { name: "Launch evaluation" }));
    expect(await screen.findByText(/Enter an amount in dollars/)).toBeInTheDocument();
    expect(postTo(calls, "/evaluations")).toBeUndefined();

    await userEvent.clear(budget);
    await userEvent.type(budget, "0.75");
    expect(preview).toHaveTextContent("stops at $0.7500");
    await userEvent.click(screen.getByRole("button", { name: "Launch evaluation" }));
    await waitFor(() => expect(postTo(calls, "/evaluations")).toBeDefined());
    expect(JSON.parse(String(postTo(calls, "/evaluations")!.init?.body))).toMatchObject({
      agent_version_id: F.IDS.version,
      max_cost_usd: "0.75",
    });
  });

  it("says so when pricing is unknown", async () => {
    mockApi({
      ...PUBLIC_DEMO,
      ...catalogue,
      "/evaluations/estimate": (u: URL, init?: RequestInit) => ({ ...estimate(u, init), pricing_known: false, max_cost_usd: null, max_cost_usd_per_run: null }),
    });
    renderWithClient(<NewEvaluationView />);
    const preview = await screen.findByRole("group", { name: "Estimate" });
    await waitFor(() => expect(preview).toHaveTextContent("pricing unknown for claude-test-model"));
  });

  it("offers only versions whose provider this server runs, and explains the rest", async () => {
    mockApi({ ...PUBLIC_DEMO, ...withFake, "/providers": F.providersFakeOnly });
    renderWithClient(<NewEvaluationView initialVersionId={F.IDS.version} />);
    const select = await screen.findByLabelText("Agent version");
    expect(select).toHaveValue(F.fakeVersion.id);
    expect(within(select).getAllByRole("option")).toHaveLength(1);
    const note = screen.getByRole("note", { name: "Versions not offered" });
    expect(note).toHaveTextContent("coder-agent v2");
    expect(note).toHaveTextContent("Not enabled on this server (AGENTEVAL_ENABLE_ANTHROPIC_PROVIDER)");
  });

  it("explains when no version can run here", async () => {
    mockApi({ ...PUBLIC_DEMO, ...catalogue, "/providers": F.providersFakeOnly });
    renderWithClient(<NewEvaluationView />);
    expect(await screen.findByText("No agent version can run here")).toBeInTheDocument();
    expect(screen.queryByRole("form", { name: "New evaluation" })).not.toBeInTheDocument();
  });

  it("still works against an API without /providers or /evaluations/estimate", async () => {
    const older: Record<string, unknown> = { ...catalogue };
    delete older["/providers"];
    delete older["/evaluations/estimate"];
    mockApi({ ...PUBLIC_DEMO, ...older });
    renderWithClient(<NewEvaluationView />);
    const form = await screen.findByRole("form", { name: "New evaluation" });
    expect(form).toHaveTextContent("This queues 5 runs");
    await waitFor(() => expect(screen.queryByText(/Estimating/)).not.toBeInTheDocument());
    expect(screen.queryByRole("group", { name: "Estimate" })).not.toBeInTheDocument();
  });
});

describe("Start a run", () => {
  it("posts task and agent version, then opens the run", async () => {
    push.mockClear();
    const created = { ...F.runningRun, id: "f0000000-0000-4000-8000-0000000000bb", status: "QUEUED" };
    const { calls } = mockApi({ ...PUBLIC_DEMO, ...catalogue, "/runs": respond(202, created) });
    renderWithClient(<NewRunView />);
    await screen.findByRole("form", { name: "Start a run" });
    expect(screen.getByLabelText("Task")).toHaveValue(F.IDS.task); // agent tasks first
    await userEvent.click(screen.getByRole("button", { name: "Start run" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith(`/runs/${created.id}`));
    expect(JSON.parse(String(postTo(calls, "/runs")!.init?.body))).toEqual({
      task_id: F.IDS.task,
      agent_version_id: F.IDS.version,
    });
  });

  it("sends no agent version for a command task", async () => {
    const { calls } = mockApi({ ...PUBLIC_DEMO, ...catalogue, "/runs": respond(202, F.runningRun) });
    renderWithClient(<NewRunView />);
    await userEvent.selectOptions(await screen.findByLabelText("Task"), commandTask.id);
    expect(screen.getByLabelText("Agent version")).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Start run" }));
    await waitFor(() => expect(postTo(calls, "/runs")).toBeDefined());
    expect(JSON.parse(String(postTo(calls, "/runs")!.init?.body))).toEqual({ task_id: commandTask.id });
  });
});

describe("a read-only API hides the launch flows", () => {
  const READ_ONLY = { "/health": { status: "ok", read_only: true } };

  it("New evaluation shows a note instead of the form", async () => {
    mockApi({ ...READ_ONLY, ...catalogue });
    renderWithClient(<NewEvaluationView />);
    expect(await screen.findByText("This deployment is read-only")).toBeInTheDocument();
    expect(screen.queryByRole("form", { name: "New evaluation" })).not.toBeInTheDocument();
  });

  it("Start a run shows a note instead of the form", async () => {
    mockApi({ ...READ_ONLY, ...catalogue });
    renderWithClient(<NewRunView />);
    expect(await screen.findByText("This deployment is read-only")).toBeInTheDocument();
    expect(screen.queryByRole("form", { name: "Start a run" })).not.toBeInTheDocument();
  });

  it("lists offer no launch buttons", async () => {
    mockApi({ ...READ_ONLY, ...catalogue, "/evaluations": { evaluations: [F.evaluation], next_after: null }, "/runs": { runs: [F.run], next_after: null } });
    renderWithClient(
      <>
        <Nav />
        <EvaluationsView />
        <RunsView />
      </>,
    );
    expect(await screen.findByText(/Read-only demo/)).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "calc-demo" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "New evaluation" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Start a run" })).not.toBeInTheDocument();
  });
});
