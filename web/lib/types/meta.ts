// SOURCE: derived from schemabrain/dashboard/sidecar.py routes
//
// Dashboard-only shapes the sidecar JSON routes synthesise. No
// direct Python equivalent — these compose existing core types
// into the response shapes the UI consumes.

import type { InferenceMethod, ValidationState } from "./envelope";
import type { ColumnPiiTag } from "./pii";

/** GET /api/meta response. */
export interface Meta {
  charter_version: string;
  dashboard_schema_version: string;
  fingerprint_version: string;
  store_path: string;
  default_source_connection_id: string | null;
  source_connection_ids: readonly string[];
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

/** /api/entities/{name}/columns response. */
export interface EntityDrilldownResponse {
  entity: EntitySummary;
  columns: readonly EntityColumn[];
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
