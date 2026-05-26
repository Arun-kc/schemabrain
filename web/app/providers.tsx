"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";

/**
 * Client-side providers mounted in the root layout. The QueryClient
 * lives in component state so each browser session gets its own
 * cache (no module-level singleton crossing requests).
 *
 * Defaults tuned for a dashboard against a localhost sidecar:
 *  - 30s stale time matches the SSE tick (RFC §3 — audit stream pushes
 *    every 2s but data routes are polled-on-mount via useQuery).
 *  - 1 retry on failure with 1s backoff. The sidecar is on localhost,
 *    so transient failures are rare; longer retries just delay error UI.
 *  - refetchOnWindowFocus: false. The operator is reading, not
 *    monitoring; a focus refetch on every Cmd-Tab feels jittery.
 */
export function Providers({ children }: { children: ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 30_000,
            retry: 1,
            retryDelay: 1_000,
            refetchOnWindowFocus: false,
          },
        },
      }),
  );
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
