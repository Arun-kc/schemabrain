"use client";

import { useQuery } from "@tanstack/react-query";
import type { GraphResponse } from "./types/graph";
import { api } from "./api";
import { useSourceId } from "./useSourceId";

interface UseGraphResult {
  /** The knowledge-graph projection for the active source (null until ready). */
  graph: GraphResponse | null;
  status: "loading" | "ready" | "error";
}

/**
 * Fetch the knowledge-graph projection (GET /api/graph) for the active source.
 *
 * Keys on the resolved source id so it refetches when the shell source
 * selector changes, and stays disabled until a source resolves. An
 * explicit `sourceId` overrides the resolved one. `SidecarError`
 * propagates from `api.graph` through TanStack Query as the error status.
 */
export function useGraph(sourceId?: string): UseGraphResult {
  const { sourceId: resolvedSourceId, status: sourceStatus } = useSourceId();
  const activeSourceId = sourceId ?? resolvedSourceId ?? undefined;

  const query = useQuery({
    queryKey: ["graph", activeSourceId],
    queryFn: () => api.graph(activeSourceId),
    enabled: sourceId !== undefined || sourceStatus !== "loading",
  });

  if (query.isPending) return { graph: null, status: "loading" };
  if (query.isError) return { graph: null, status: "error" };
  return { graph: query.data, status: "ready" };
}
