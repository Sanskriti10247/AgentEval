import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import * as api from "@/lib/api/client";
import { ApiError } from "@/lib/api/client";
import { AuthGate } from "@/components/auth-gate";
import { Nav } from "@/components/nav";
import { clearApiKey, getApiKey, getAuthState, rejectApiKey, setApiKey } from "@/lib/auth";
import { mockApi, renderWithClient, respond } from "./helpers";

vi.mock("next/navigation", () => ({ usePathname: () => "/" }));

const KEY = "ae_0a1b2c3d_" + "A".repeat(43);

function authHeader(call: { init?: RequestInit } | undefined): string | undefined {
  return (call?.init?.headers as Record<string, string> | undefined)?.Authorization;
}

describe("key storage", () => {
  it("keeps the key in sessionStorage only (trimmed), and signing out forgets it", () => {
    setApiKey(`  ${KEY}\n`);
    expect(getApiKey()).toBe(KEY);
    expect(sessionStorage.getItem("agenteval.apiKey")).toBe(KEY);
    expect(localStorage.length).toBe(0);
    clearApiKey();
    expect(getApiKey()).toBeNull();
    expect(sessionStorage.getItem("agenteval.apiKey")).toBeNull();
  });

  it("returns a stable snapshot until something changes", () => {
    setApiKey(KEY);
    expect(getAuthState()).toBe(getAuthState());
    const before = getAuthState();
    setApiKey(KEY + "x");
    expect(getAuthState()).not.toBe(before);
  });
});

describe("the client and the key", () => {
  it("sends the key as a bearer token, and nothing when there is none", async () => {
    const { calls } = mockApi({ "/stats/overview": {} });
    await api.getOverview();
    expect(authHeader(calls[0])).toBeUndefined();
    setApiKey(KEY);
    await api.getOverview();
    expect(authHeader(calls[1])).toBe(`Bearer ${KEY}`);
  });

  it("on 401 forgets the key, flags it as rejected, and raises a readable error", async () => {
    setApiKey(KEY);
    mockApi({ "/stats/overview": respond(401, { title: "Unauthorized", status: 401, detail: "This API key has been revoked" }) });
    const err = (await api.getOverview().catch((e: unknown) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(401);
    expect(err.message).toMatch(/rejected the API key/);
    expect(getApiKey()).toBeNull();
    expect(getAuthState().rejected).toBe(true);
  });

  it("on 403 keeps the key and names the missing scope", async () => {
    setApiKey(KEY);
    mockApi({ "/runs/r1/cancel": respond(403, { title: "Forbidden", status: 403, detail: "This API key lacks the write scope" }) });
    const err = (await api.cancelRun("r1").catch((e: unknown) => e)) as ApiError;
    expect(err.status).toBe(403);
    expect(err.message).toBe("This API key lacks the write scope");
    expect(getApiKey()).toBe(KEY);
  });

  it("on 429 says how long to wait", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ title: "Too Many Requests", status: 429, detail: "Rate limit of 600 read requests per minute exceeded" }), {
          status: 429,
          headers: { "Content-Type": "application/problem+json", "Retry-After": "12" },
        }),
      ),
    );
    const err = (await api.getOverview().catch((e: unknown) => e)) as ApiError;
    expect(err.status).toBe(429);
    expect(err.message).toContain("Retry in 12 s.");
  });

  it("a 401 without a stored key does not flag a rejection", async () => {
    mockApi({ "/stats/overview": respond(401, { title: "Unauthorized", status: 401, detail: "Missing API key" }) });
    await api.getOverview().catch(() => undefined);
    expect(getAuthState().rejected).toBe(false);
  });
});

// A deployment that wants a key: /health answers, with neither demo flag.
const PRIVATE = { "/health": { status: "ok", read_only: false } };

describe("AuthGate", () => {
  const page = <AuthGate><div>secret page</div></AuthGate>;

  it("asks for a key first, then shows the page and keeps the key in sessionStorage", async () => {
    mockApi(PRIVATE);
    renderWithClient(page);
    expect(await screen.findByRole("form", { name: "API key" })).toBeInTheDocument();
    expect(screen.queryByText("secret page")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Use key" })).toBeDisabled();

    await userEvent.type(screen.getByLabelText("API key", { selector: "input" }), KEY);
    await userEvent.click(screen.getByRole("button", { name: "Use key" }));
    expect(await screen.findByText("secret page")).toBeInTheDocument();
    expect(sessionStorage.getItem("agenteval.apiKey")).toBe(KEY);
  });

  it("shows the page at once when a key is stored", async () => {
    setApiKey(KEY);
    renderWithClient(page);
    expect(await screen.findByText("secret page")).toBeInTheDocument();
    expect(screen.queryByRole("form", { name: "API key" })).not.toBeInTheDocument();
  });

  it("returns to the key form, with a message, when the API answers 401", async () => {
    setApiKey(KEY);
    mockApi({ "/stats/overview": respond(401, { title: "Unauthorized", status: 401, detail: "revoked" }) });
    renderWithClient(page);
    expect(await screen.findByText("secret page")).toBeInTheDocument();
    await api.getOverview().catch(() => undefined);
    expect(await screen.findByRole("alert")).toHaveTextContent("rejected the key");
    expect(screen.queryByText("secret page")).not.toBeInTheDocument();
    expect(getApiKey()).toBeNull();
  });

  it("Sign out clears the key and shows the form again", async () => {
    mockApi(PRIVATE);
    setApiKey(KEY);
    renderWithClient(
      <>
        <Nav />
        {page}
      </>,
    );
    await userEvent.click(await screen.findByRole("button", { name: "Sign out" }));
    expect(await screen.findByRole("form", { name: "API key" })).toBeInTheDocument();
    expect(getApiKey()).toBeNull();
    await waitFor(() => expect(screen.queryByRole("button", { name: "Sign out" })).not.toBeInTheDocument());
  });

  it("rejectApiKey without a key is not a rejection", () => {
    rejectApiKey();
    expect(getAuthState()).toEqual({ key: null, rejected: false });
  });
});

describe("AuthGate while the API is unreachable", () => {
  it("says it is waking the API instead of asking for a key", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    renderWithClient(<AuthGate><div>secret page</div></AuthGate>);
    expect(await screen.findByText(/takes about a minute to start/, {}, { timeout: 8000 })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Waking the API");
    expect(screen.queryByRole("form", { name: "API key" })).not.toBeInTheDocument();
  }, 10_000);
});
