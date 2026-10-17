import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { vi } from "vitest";

/** An explicit HTTP response (status + JSON body); any other handler value is a 200 JSON body. */
export class MockResponse {
  constructor(
    readonly status: number,
    readonly body?: unknown,
  ) {}
}
export const respond = (status: number, body?: unknown) => new MockResponse(status, body);
export type Handler = unknown;

/**
 * Stub global fetch. Keys are API paths below /api/v1 ("/runs", "/runs/abc"); a key matches
 * the request pathname exactly. Unmatched requests answer 404 as problem+json.
 */
export function mockApi(routes: Record<string, Handler>) {
  const calls: { url: URL; init?: RequestInit }[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input));
    calls.push({ url, init });
    const key = url.pathname.replace(/^\/api\/v1/, "");
    const handler = routes[key];
    let status = 404;
    let body: unknown = { type: "about:blank", title: "Not Found", status: 404, detail: `no mock for ${key}` };
    if (handler !== undefined) {
      const out = typeof handler === "function" ? (handler as (u: URL, i?: RequestInit) => unknown)(url, init) : handler;
      if (out instanceof MockResponse) {
        status = out.status;
        body = out.body;
      } else {
        status = 200;
        body = out;
      }
    }
    return new Response(body === undefined ? null : JSON.stringify(body), {
      status,
      headers: { "Content-Type": status >= 400 ? "application/problem+json" : "application/json" },
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls, fetchMock };
}

export function renderWithClient(ui: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}
