import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";
import { clearApiKey } from "@/lib/auth";

// Recharts measures its container with ResizeObserver, which jsdom lacks.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
globalThis.ResizeObserver = ResizeObserverStub as unknown as typeof ResizeObserver;

afterEach(() => {
  cleanup();
  clearApiKey(); // no key (and no rejected flag) leaks between tests
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
