"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "./api";

/**
 * Resolve the dashboard's active source_connection_id by querying
 * the sidecar's /api/meta endpoint.
 *
 * Returns the canonical hashed source_id (never a URL with creds —
 * the sidecar redacts before responding; see
 * `schemabrain/core/source_id.py`). Falls back to `null` when the
 * store has no indexed source yet, which surfaces the "no source
 * connected" state on the dashboard.
 *
 * Cached across components — TanStack Query dedupes simultaneous
 * fetches and reuses the result for 5 minutes (sidecar meta rarely
 * changes mid-session).
 */
export function useSourceId(): {
  sourceId: string | null;
  status: "loading" | "ready" | "error";
} {
  const query = useQuery({
    queryKey: ["meta"],
    queryFn: () => api.meta(),
    staleTime: 5 * 60 * 1000,
  });

  if (query.isPending) return { sourceId: null, status: "loading" };
  if (query.isError) return { sourceId: null, status: "error" };
  return {
    sourceId: query.data.default_source_connection_id,
    status: "ready",
  };
}
