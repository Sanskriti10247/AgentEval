import { describe, expect, it, vi } from "vitest";
import * as api from "@/lib/api/client";
import { ApiError, buildUrl } from "@/lib/api/client";
import { IDS, run } from "./fixtures";
import { mockApi, respond } from "./helpers";

describe("buildUrl", () => {
  it("uses the default base URL and /api/v1", () => {
    expect(buildUrl("/runs")).toBe("http://localhost:8000/api/v1/runs");
  });
  it("honours NEXT_PUBLIC_API_URL and trims trailing slashes", () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.example.test//");
    expect(buildUrl("/agents")).toBe("https://api.example.test/api/v1/agents");
    vi.unstubAllEnvs();
  });
  it("drops undefined, null and empty params but keeps false and 0", () => {
    const url = buildUrl("/runs", { status: undefined, after: null, task_id: "", dead_letter: false, limit: 0 });
    expect(url).toBe("http://localhost:8000/api/v1/runs?dead_letter=false&limit=0");
  });
});

describe("requests", () => {
  it("sends filters and the cursor as query params", async () => {
    const { calls } = mockApi({ "/runs": { runs: [], next_after: null } });
    await api.listRuns({ status: "FAILED", dead_letter: true, evaluation_id: IDS.eval1, after: "cur", limit: 50 });
    const q = calls[0]!.url.searchParams;
    expect(q.get("status")).toBe("FAILED");
    expect(q.get("dead_letter")).toBe("true");
    expect(q.get("evaluation_id")).toBe(IDS.eval1);
    expect(q.get("after")).toBe("cur");
    expect(q.get("limit")).toBe("50");
  });

  it("pages through a keyset list with next_after", async () => {
    const pages: Record<string, unknown> = {
      "": { runs: [{ ...run, id: "r1" }], next_after: "c1" },
      c1: { runs: [{ ...run, id: "r2" }], next_after: null },
    };
    mockApi({ "/runs": (u: URL) => pages[u.searchParams.get("after") ?? ""] });
    const first = await api.listRuns({});
    expect(first.next_after).toBe("c1");
    const second = await api.listRuns({ after: first.next_after });
    expect(second.runs[0]!.id).toBe("r2");
    expect(second.next_after).toBeNull();
  });

  it("passes window_h to the overview and ids to compare", async () => {
    const { calls } = mockApi({ "/stats/overview": {}, "/evaluations/compare": {} });
    await api.getOverview(168);
    await api.compareEvaluations(["a", "b", "c"]);
    expect(calls[0]!.url.searchParams.get("window_h")).toBe("168");
    expect(calls[1]!.url.searchParams.get("ids")).toBe("a,b,c");
  });

  it("pages events by after_seq", async () => {
    const { calls } = mockApi({ [`/runs/${IDS.run}/attempts/2/events`]: { events: [], next_after_seq: null } });
    await api.listEvents(IDS.run, 2, { afterSeq: 41, limit: 200 });
    expect(calls[0]!.url.searchParams.get("after_seq")).toBe("41");
    expect(calls[0]!.url.searchParams.get("limit")).toBe("200");
  });

  it("POSTs cancel and replay with the right method and body", async () => {
    const { calls } = mockApi({ [`/runs/${IDS.run}/cancel`]: run, [`/runs/${IDS.run}/replay`]: run, [`/evaluations/${IDS.eval1}/cancel`]: {} });
    await api.cancelRun(IDS.run);
    await api.replayRun(IDS.run, 3);
    await api.cancelEvaluation(IDS.eval1);
    expect(calls.map((c) => c.init?.method)).toEqual(["POST", "POST", "POST"]);
    expect(calls[0]!.init?.body).toBeUndefined();
    expect(JSON.parse(String(calls[1]!.init?.body))).toEqual({ additional_attempts: 3 });
  });
});

describe("error handling", () => {
  it("turns problem+json into an ApiError", async () => {
    mockApi({ [`/runs/${IDS.run}`]: respond(404, { title: "Not Found", status: 404, detail: "run not found" }) });
    const err = await api.getRun(IDS.run).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(404);
    expect((err as ApiError).message).toBe("Not Found: run not found");
  });

  it("formats 422 validation lists", async () => {
    mockApi({
      "/evaluations/compare": respond(422, { detail: [{ type: "value_error", loc: ["query", "ids"], msg: "must share a dataset" }] }),
    });
    const err = (await api.compareEvaluations(["a", "b"]).catch((e: unknown) => e)) as ApiError;
    expect(err.status).toBe(422);
    expect(err.message).toContain("query.ids: must share a dataset");
  });

  it("reports an unreachable API as status 0", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("fetch failed")));
    const err = (await api.getOverview().catch((e: unknown) => e)) as ApiError;
    expect(err.status).toBe(0);
    expect(err.message).toMatch(/Cannot reach the API at http:\/\/localhost:8000/);
  });

  it("copes with a non-JSON error body", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("Bad gateway", { status: 502 })));
    const err = (await api.getOverview().catch((e: unknown) => e)) as ApiError;
    expect(err.status).toBe(502);
    expect(err.message).toBe("Request failed with status 502");
  });

  it("maps a missing attempt evaluation (404) to null but rethrows other errors", async () => {
    mockApi({ [`/runs/${IDS.run}/attempts/1/evaluation`]: respond(404, { title: "Not Found", status: 404, detail: "x" }) });
    expect(await api.getAttemptEvaluation(IDS.run, 1)).toBeNull();
    mockApi({ [`/runs/${IDS.run}/attempts/1/evaluation`]: respond(500, { title: "Boom", status: 500, detail: "x" }) });
    await expect(api.getAttemptEvaluation(IDS.run, 1)).rejects.toBeInstanceOf(ApiError);
  });
});
