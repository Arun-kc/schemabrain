"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "./api";
import { useSourceStore } from "./sourceStore";

/**
 * Resolve the dashboard's active source_connection_id.
 *
 * Precedence: the shell source selector's explicit choice (the
 * `useSourceStore` selection) wins; otherwise the sidecar-resolved
 * default from /api/meta (`default_source_connection_id`). This is how
 * the source selector "drives the active source" — surfaces that key
 * their query on the resolved id (e.g. the PII matrix) refetch when the
 * selection changes, while surfaces that never touch the selector keep
 * the default behaviour unchanged (the selection starts null).
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
  const selected = useSourceStore((s) => s.selectedSourceId);
  const query = useQuery({
    queryKey: ["meta"],
    queryFn: () => api.meta(),
    staleTime: 5 * 60 * 1000,
  });

  // An explicit selection is authoritative — trust it without waiting on
  // (or surfacing) the meta query's loading/error state.
  if (selected) return { sourceId: selected, status: "ready" };
  if (query.isPending) return { sourceId: null, status: "loading" };
  if (query.isError) return { sourceId: null, status: "error" };
  return {
    sourceId: query.data.default_source_connection_id,
    status: "ready",
  };
}
