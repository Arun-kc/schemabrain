// SOURCE: derived from schemabrain/dashboard/sidecar.py GET /api/drift
//
// Dashboard-only shape the sidecar synthesises for the Drift surface.
// No direct Python equivalent — the route composes _compute_policy_drift
// (config drift) + store.has_stale_prompt_version (enrichment drift) +
// summarize_sources / latest_enriched_at (freshness) into this envelope.

/**
 * The drift kinds the read-only, store-only sidecar can honestly verify
 * from persisted state. Schema-drift (table/column missing) and
 * YAML-vs-disk definition drift need a live DB connection the sidecar
 * does not have, so they are intentionally NOT in this union — the
 * surface never fabricates a card it cannot back.
 */
export type DriftKind = "config" | "enrichment";

/** Raw risk vocabulary from the route — "med", not "medium". */
export type DriftRisk = "high" | "med" | "low";

/** A copy-the-CLI-command remedy. The dashboard never writes (ADR 0006). */
export interface DriftAction {
  label: string;
  /** CLI command to copy; the route always supplies one today. */
  command: string;
}

export interface DriftItem {
  kind: DriftKind;
  risk: DriftRisk;
  title: string;
  /** ISO8601 for config drift; null for enrichment (no single timestamp). */
  when: string | null;
  detail: string;
  action: DriftAction;
}

/** Roll-up over items[]. `fresh` is true iff items is empty. */
export interface DriftFreshness {
  fresh: boolean;
  change_count: number;
  high_risk: number;
  /**
   * Epoch seconds of the newest enrichment write; null when nothing has
   * been enriched yet — render "—", never a fabricated 0.
   */
  last_enriched: number | null;
  /** Epoch seconds of the newest index; null when no physical tables. */
  last_indexed: number | null;
  /** Credential-safe hashed source id (never the raw connection URL). */
  source_label: string;
  /** Live prompt version the persisted descriptions are diffed against. */
  prompt_version: string;
}

export interface DriftResponse {
  source_connection_id: string;
  freshness: DriftFreshness;
  items: readonly DriftItem[];
}
