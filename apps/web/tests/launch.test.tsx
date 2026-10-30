import { fireEvent, screen, waitFor } from "@testing-library/react";
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

const catalogue = {
  "/agents": { agents: [F.agent], next_after: null },
  [`/agents/${F.IDS.agent}/versions`]: [olderVersion, F.version],
  "/datasets": { datasets: [{ ...F.dataset, task_count: 5 }], next_after: null },
  "/tasks": { tasks: [F.task, commandTask], next_after: null },
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
