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

/** /api/health response. */
export interface HealthResponse {
  status: "ok";
  store: "ok" | "degraded";
  store_reason: string | null;
  uptime_s: number;
}
