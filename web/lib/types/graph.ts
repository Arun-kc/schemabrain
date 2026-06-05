// SOURCE: derived from schemabrain/dashboard/sidecar.py GET /api/graph
// (the v15 graph projection — schemabrain/core/graph.py + ADR 0010).
//
// The persisted knowledge-graph read-model: one node per entity, one
// edge per canonical join, plus the ordered canonical path (the diameter
// spine). `catastrophic` is a LIVE flag the route recomputes from current
// PII tags, so it never disagrees with the PII matrix.

import type { Group } from "./meta";

/**
 * Honest edge provenance. `declared` = backed by a DB foreign key;
 * `log_mined` = recovered from query-log mining; `inferred` = an
 * LLM-suggested / hand-authored join with no FK backing. Never a value
 * implying the engine inspected agent-authored SQL.
 */
export type GraphEdgeEvidence = "declared" | "log_mined" | "inferred";

export interface GraphNode {
  /** Entity name — the stable node id (unique per source). */
  id: string;
  label: string;
  /** Cosmetic clustering group (node colour); not a trust signal. */
  group: Group;
  /** Catastrophic-floor PII present — LIVE, consistent with the PII matrix. */
  catastrophic: boolean;
  /** Cached row-count estimate; null when the backend can't estimate it. */
  row_count: number | null;
}

export interface GraphEdge {
  /** Canonical-join name — the stable edge id. */
  id: string;
  source: string;
  target: string;
  evidence: GraphEdgeEvidence;
  /** 0 = off the highlighted path, 1 = primary canonical path, 2 = alternate. */
  canonical_path_rank: 0 | 1 | 2;
}

/** The single canonical path of the schema — its longest join chain. */
export interface CanonicalPath {
  /** Ordered entity names, anchor..target inclusive. */
  nodes: readonly string[];
  /** Ordered canonical-join names traversed. */
  edges: readonly string[];
  hops: number;
}

export interface GraphResponse {
  /** Credential-safe source id (hashed; never a raw connection URL). */
  source_connection_id: string;
  nodes: readonly GraphNode[];
  edges: readonly GraphEdge[];
  canonical_path: CanonicalPath;
}
