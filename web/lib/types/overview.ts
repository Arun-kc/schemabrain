// SOURCE: derived from schemabrain/dashboard/sidecar.py GET /api/overview
//
// Dashboard-only summary the sidecar rolls up for the Overview ("home")
// surface. No single Python equivalent — the route composes the same
// honest, store-only signals the per-surface routes expose (entities,
// /api/pii/policy verdict, drift freshness, audit, graph) into one read.
// Every figure is derived live; absent values are `null` so the UI renders
// "—", never a fabricated 0.

/** Connected source: credential-safe label + table count + last index. */
export interface OverviewSource {
  label: string;
  /** Physical table count for this source; null when unknown. */
  tables: number | null;
  /** Epoch seconds of the newest index; null when no physical tables. */
  last_indexed_at: number | null;
}

export interface OverviewEntityGroups {
  identity: number;
  billing: number;
  activity: number;
  other: number;
}

/**
 * Categorical bind-confidence distribution. SchemaBrain's `bind_confidence`
 * is high/medium/low (never a numeric %); hand-authored entities have no
 * model rating and fall in `unrated`.
 */
export interface OverviewConfidence {
  high: number;
  medium: number;
  low: number;
  unrated: number;
}

export interface OverviewEntities {
  count: number;
  catastrophic_count: number;
  by_group: OverviewEntityGroups;
  confidence: OverviewConfidence;
}

/**
 * Per-column protection posture (Option A). `catastrophic` columns carry a
 * catastrophic-floor category (always hard-blocked — the `--alarm` share);
 * `policy_blocked` are blocked by the operator's active policy but are not
 * catastrophic; `allowed` are neither. `columns` is the classified total.
 */
export interface OverviewProtection {
  catastrophic: number;
  policy_blocked: number;
  allowed: number;
  columns: number;
}

export interface OverviewFreshness {
  /** True iff there are zero drift items. */
  fresh: boolean;
  change_count: number;
  risk: { high: number; med: number; low: number };
  /** Epoch seconds of the newest enrichment; null when never enriched. */
  last_enriched: number | null;
}

export interface OverviewRefusalItem {
  tool: string;
  /** First PII category on the refused call; null when none recorded. */
  category: string | null;
  /** Epoch seconds; null on a malformed timestamp. */
  occurred_at: number | null;
}

export interface OverviewRefusals {
  count_24h: number;
  recent: readonly OverviewRefusalItem[];
}

export interface OverviewAuditItem {
  tool: string;
  /** Append-only row id (sequence). */
  seq: number;
  refused: boolean;
  /** Derived from structured columns (refusal reason / status) — never the
   * agent's request text, which is not persisted. */
  summary: string;
  occurred_at: number | null;
}

export interface OverviewAudit {
  /** Total tool calls in the append-only audit log. */
  total: number;
  recent: readonly OverviewAuditItem[];
}

export interface OverviewSemantic {
  metrics_count: number;
  joins_count: number;
  /** Joins mined from query logs the schema never declared. */
  mined_count: number;
  metric_names: readonly string[];
}

export interface OverviewResponse {
  source_connection_id: string;
  source: OverviewSource;
  entities: OverviewEntities;
  protection: OverviewProtection;
  freshness: OverviewFreshness;
  refusals: OverviewRefusals;
  audit: OverviewAudit;
  semantic: OverviewSemantic;
}
