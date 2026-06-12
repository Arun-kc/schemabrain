// SOURCE: derived from schemabrain/dashboard/sidecar.py GET /api/dict
// (the build_dictionary aggregator — schemabrain/datadict/model.py +
// model_json.py).
//
// The full data-dictionary model: a schema-version echo plus, per source,
// every curated entity with its physical columns (type + identity role +
// PII tags), semantic joins (the server-rendered, catastrophic-redacted ON
// clause + raw cardinality + a pre-labelled provenance), and entity-anchored
// metrics. This is the SAME model the `schemabrain docs` CLI renders, so the
// dashboard surface browses it and its "Export Markdown" re-renders it
// client-side (lib/dict/serialize.ts) byte-for-byte with the CLI golden.

import type { Cardinality, Group } from "./meta";
import type { PIICategory, Sensitivity } from "./pii";

/** Closed-grammar metric aggregation. Mirrors core/metric.py AggFunction. */
export type AggFunction = "sum" | "count" | "count_distinct" | "avg" | "min" | "max";

/** Closed-grammar metric time grain. Mirrors core/metric.py TimeGrain. */
export type TimeGrain = "day" | "week" | "month" | "quarter" | "year";

export interface DictColumn {
  name: string;
  data_type: string;
  nullable: boolean;
  is_primary_key: boolean;
  /** name === the owning entity's identity column. */
  is_identity: boolean;
  /** Free-text meaning; null when un-enriched (renders "—"). */
  description: string | null;
  pii_sensitivity: Sensitivity;
  /** Ordered category tags; empty when the column carries no PII. */
  pii_categories: readonly PIICategory[];
}

export interface DictJoin {
  name: string;
  /** "" when undescribed (renders "—"). */
  description: string;
  source_entity: string;
  target_entity: string;
  /** Server-rendered ON predicate; a catastrophic FK column is redacted. */
  on_clause: string;
  /** Raw equi-join cardinality; null when unknown (renders "—"). */
  cardinality: Cardinality | null;
  /** Human provenance label, pre-rendered by the aggregator. */
  provenance: string;
}

export interface DictMetric {
  name: string;
  description: string;
  agg: AggFunction;
  /** Column name or expression text. */
  measure: string;
  measure_is_expression: boolean;
  /** "<entity>.<column>" or null (renders "—"). */
  time_dimension: string | null;
  time_grains: readonly TimeGrain[];
}

export interface DictEntity {
  name: string;
  description: string;
  /** "schema.table". */
  qualified_table: string;
  identity: string;
  /** v15 cosmetic grouping (identity | billing | activity | other). */
  group: Group;
  columns: readonly DictColumn[];
  joins: readonly DictJoin[];
  metrics: readonly DictMetric[];
}

export interface DictSource {
  source_connection_id: string;
  entities: readonly DictEntity[];
}

/** GET /api/dict response — the complete dictionary model. */
export interface DictionaryModel {
  /** store.SCHEMA_VERSION echo, surfaced in the rendered page header. */
  schema_version: string;
  sources: readonly DictSource[];
}
