// SOURCE: derived from schemabrain/dashboard/sidecar.py GET /api/graph
// (the v15 graph projection — schemabrain/core/graph.py + ADR 0010).
//
// The persisted knowledge-graph read-model: one node per entity, one
// edge per canonical join, plus the ordered canonical path (the diameter
// spine). `pii_level` is a LIVE value the route recomputes from current
// PII tags, so it never disagrees with the PII matrix. `cardinality`
// rides on declared FK edges only (ADR 0011).

import type { Cardinality, Group, PiiLevel } from "./meta";

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
  /**
   * LIVE PII severity, consistent with the PII matrix (one source of
   * truth). `catastrophic` drives the alarm node; the non-catastrophic
   * tiers drive the lighter halo. Do NOT re-derive catastrophic-ness
   * client-side from a chip-kind helper — gate on this string.
   */
  pii_level: PiiLevel;
  /** Cached row-count estimate; null when the backend can't estimate it. */
  row_count: number | null;
  /**
   * LIVE count of `mcp_audit` refusals attributed to this entity (v17 /
   * dashboard 1.8). 0 when none. Honest aggregate — refusals the engine
   * could not attribute to one entity are NOT folded in here; they live in
   * `GraphResponse.unattributed_refusals`, so the two always reconcile with
   * the audit log. Drives the refusal-hotspot overlay (PR-17b).
   */
  refusal_count: number;
}

export interface GraphEdge {
  /** Canonical-join name — the stable edge id. */
  id: string;
  source: string;
  target: string;
  evidence: GraphEdgeEvidence;
  /** 0 = off the highlighted path, 1 = primary canonical path, 2 = alternate. */
  canonical_path_rank: 0 | 1 | 2;
  /**
   * Equi-join cardinality — present on declared FK edges only; null for
   * log-mined / inferred edges (the engine never verified their shape) and
   * for FKs whose uniqueness the connector could not confirm. Never a guess.
   */
  cardinality: Cardinality | null;
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
  /**
   * LIVE count of refused `mcp_audit` rows not shown on any visible node: a
   * NULL `anchor_entity` (the engine could not pin it to one entity) OR an
   * orphaned anchor naming an entity no longer in the projection. Surfaced
   * explicitly so the per-node `refusal_count` badges plus this figure always
   * reconcile with the audit log — a refusal is never silently dropped (PR-17b).
   */
  unattributed_refusals: number;
}
