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

/** v15 cosmetic graph grouping for an entity (node colour / clustering). */
export type Group = "identity" | "billing" | "activity" | "other";

/** Equi-join cardinality; null when authored before the column existed. */
export type Cardinality =
  | "one_to_one"
  | "one_to_many"
  | "many_to_one"
  | "many_to_many";

/**
 * One decisive PII severity badge per entity. "catastrophic" outranks the
 * 4-level sensitivity ladder; "none" when every column is public/untagged.
 */
export type PiiLevel = "catastrophic" | "pii" | "confidential" | "internal" | "none";

/** Entity header shape shared by the list rows and the drilldown. */
export interface EntitySummary {
  name: string;
  description: string;
  qualified_table: string;
  identity: string;
  group: Group;
  origin: Origin;
  inference_method: InferenceMethod;
  validation_state: ValidationState;
}

/** One row in the /api/entities index — entity header + rollup counts. */
export interface EntityListItem extends EntitySummary {
  pii_level: PiiLevel;
  metrics_count: number;
  joins_count: number;
}

/** /api/entities list response. */
export interface EntityListResponse {
  source_connection_id: string;
  items: readonly EntityListItem[];
  count: number;
  summary: {
    entities: number;
    metrics: number;
    joins: number;
    catastrophic_entities: number;
  };
}

/** A column→column nearest neighbour for the drilldown "similar" field. */
export interface SimilarColumn {
  schema: string;
  table: string;
  column: string;
  /** Cosine similarity to the reference column, in [-1, 1]. */
  score: number;
}

/** One column inside the /api/entities/{name}/columns response. */
export interface EntityColumn {
  name: string;
  data_type: string;
  /** Free-text "meaning"; null when the column is un-enriched. */
  description: string | null;
  sensitivity: ColumnPiiTag["sensitivity"];
  pii_categories: readonly ColumnPiiTag["pii_categories"][number][];
  similar: readonly SimilarColumn[];
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
  /** Server-rendered, quoted, catastrophic-FK-redacted ON predicate. */
  on_clause: string;
  cardinality: Cardinality | null;
  origin: Origin;
  inference_method: InferenceMethod;
  /** True when `inference_method === "observed_in_query_log"`. */
  is_log_mined: boolean;
}

/** /api/entities/{name}/columns response. */
export interface EntityDrilldownResponse {
  entity: EntitySummary;
  columns: readonly EntityColumn[];
  metrics: readonly EntityMetric[];
  joins: readonly EntityJoin[];
  referenced_by: {
    metrics: number;
    joins: number;
  };
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
