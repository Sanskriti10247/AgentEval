"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { ApiError } from "@/lib/api/client";

// Auth and rate-limit answers (401, 403, 429) will not change on an immediate retry.
const NO_RETRY = new Set([401, 403, 429]);

export function Providers({ children }: { children: ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 5_000,
            retry: (count, error) => count < 1 && !(error instanceof ApiError && NO_RETRY.has(error.status)),
            refetchOnWindowFocus: false,
          },
        },
      }),
  );
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
