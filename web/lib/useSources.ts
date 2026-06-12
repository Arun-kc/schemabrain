"use client";

import { useQuery } from "@tanstack/react-query";
import type { SourceInfo } from "./types/meta";
import { api } from "./api";
import { useSourceStore } from "./sourceStore";

interface UseSourcesResult {
  /** Every indexed source the sidecar knows about (empty while loading/erroring). */
  sources: readonly SourceInfo[];
  /** The sidecar-resolved default source (config or first indexed). */
  defaultSourceId: string | null;
  /** The source the UI is scoped to: the explicit selection, else the default. */
  activeSourceId: string | null;
  status: "loading" | "ready" | "error";
}

/**
 * Source list + active selection for the shell source selector.
 *
 * Shares the `["meta"]` query key with useSourceId so the single
 * /api/meta fetch is deduped/cached across both hooks. `activeSourceId`
 * is the explicit store selection when set, otherwise the meta default —
 * the same precedence useSourceId applies, surfaced here so the selector
 * can highlight the active row.
 */
export function useSources(): UseSourcesResult {
  const selected = useSourceStore((s) => s.selectedSourceId);
  const query = useQuery({
    queryKey: ["meta"],
    queryFn: () => api.meta(),
    staleTime: 5 * 60 * 1000,
  });

  if (query.isPending) {
    return { sources: [], defaultSourceId: null, activeSourceId: selected, status: "loading" };
  }
  if (query.isError) {
    return { sources: [], defaultSourceId: null, activeSourceId: selected, status: "error" };
  }
  const defaultSourceId = query.data.default_source_connection_id;
  return {
    sources: query.data.sources,
    defaultSourceId,
    activeSourceId: selected ?? defaultSourceId,
    status: "ready",
  };
}
