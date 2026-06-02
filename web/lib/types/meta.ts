// SOURCE: derived from schemabrain/dashboard/sidecar.py routes
//
// Dashboard-only shapes the sidecar JSON routes synthesise. No
// direct Python equivalent — these compose existing core types
// into the response shapes the UI consumes.

import type { InferenceMethod, ValidationState } from "./envelope";
import type { ColumnPiiTag } from "./pii";

/**
 * Per-source index state. Only "indexed" is emitted today — a source
 * appears once it has persisted artifacts, and there is no in-flight
 * indexing registry behind "indexing"/"empty". The wider union is
 * reserved so the shell's source dot + the (future) graph indexing
 * overlay can light up without a wire-shape change.
 */
export type SourceState = "indexed" | "indexing" | "empty";

/** One source in the /api/meta `sources` rollup — drives the source selector. */
export interface SourceInfo {
  source_id: string;
  /** "postgres" for the configured connection; null when the dialect is unknown. */
  engine: string | null;
  state: SourceState;
  /** Most recent index time (Unix epoch seconds); null when no physical tables. */
  last_indexed_at: number | null;
  tables: number;
  entities: number;
}

/** GET /api/meta response. */
export interface Meta {
  charter_version: string;
  dashboard_schema_version: string;
  fingerprint_version: string;
  store_path: string;
  default_source_connection_id: string | null;
  source_connection_ids: readonly string[];
  sources: readonly SourceInfo[];
}

/** Origin tag carried by entity / metric / join writes. */
export type Origin = "manual" | "suggested" | "dbt_import";

/** Item shape in the /api/entities list. */
export interface EntitySummary {
  name: string;
  description: string;
  qualified_table: string;
  identity: string;
  origin: Origin;
  inference_method: InferenceMethod;
  validation_state: ValidationState;
}

/** /api/entities list response. */
export interface EntityListResponse {
  source_connection_id: string;
  items: readonly EntitySummary[];
  count: number;
}

/** One column inside the /api/entities/{name}/columns response. */
export interface EntityColumn {
  name: string;
  sensitivity: ColumnPiiTag["sensitivity"];
  pii_categories: readonly ColumnPiiTag["pii_categories"][number][];
}

/** One metric linked to an entity. */
export interface EntityMetric {
  name: string;
  description: string;
  measure: {
    agg: string;
    column: string | null;
    expression: string | null;
  };
  time_grains: readonly string[];
}

/** One canonical join linked to an entity. */
export interface EntityJoin {
  name: string;
  description: string;
  source_entity: string;
  target_entity: string;
  on: readonly { source_column: string; target_column: string }[];
}

/** /api/entities/{name}/columns response. */
export interface EntityDrilldownResponse {
  entity: EntitySummary;
  columns: readonly EntityColumn[];
  metrics: readonly EntityMetric[];
  joins: readonly EntityJoin[];
}

/** One row in the PII Viz matrix — entity × category counts. */
export interface PiiMatrixEntity {
  name: string;
  qualified_table: string;
  identity: string;
  origin: Origin;
  inference_method: InferenceMethod;
  validation_state: ValidationState;
  /** Map from PIICategory to the number of columns on this entity carrying that category. */
  counts: Record<string, number>;
  catastrophic_column_count: number;
  has_catastrophic: boolean;
}

/** /api/entities/pii-matrix response — drives The Ledger surface. */
export interface PiiMatrixResponse {
  source_connection_id: string;
  entities: readonly PiiMatrixEntity[];
  /** The 12 PII categories in canonical column order for the matrix. */
  categories: readonly string[];
  /** Subset of `categories` that get the catastrophic underline + stamp treatment. */
  catastrophic_categories: readonly string[];
  totals: {
    entities: number;
    columns: number;
    catastrophic_columns: number;
    pii_columns: number;
    confidential_columns: number;
    internal_or_public_columns: number;
  };
}

/** /api/health response. */
export interface HealthResponse {
  status: "ok";
  store: "ok" | "degraded";
  store_reason: string | null;
  uptime_s: number;
}
