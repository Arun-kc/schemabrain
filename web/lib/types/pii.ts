// SOURCE: schemabrain/pii/categories.py — DO NOT EDIT WITHOUT BUMPING CHARTER
//
// The 2-layer PII taxonomy. Sensitivity is a 4-level scale; PIICategory
// is a 12-value enum used as a set on each column. CATASTROPHIC_LEAK_
// CATEGORIES is the 3-element subset where SchemaBrain refuses by
// default (PR #110).

/** Layer 1 — sensitivity. Ordered for MAX-propagation across joins. */
export type Sensitivity = "public" | "internal" | "confidential" | "pii";

export const SENSITIVITIES: readonly Sensitivity[] = [
  "public",
  "internal",
  "confidential",
  "pii",
] as const;

/** Layer 2 — categorical PII tags. Set-valued at column sites. */
export type PIICategory =
  | "contact"
  | "financial"
  | "payment_card"
  | "health"
  | "genetic"
  | "biometric"
  | "behavioral"
  | "online_identifier"
  | "credential"
  | "government_id"
  | "location"
  | "demographic_protected";

/**
 * The 12 known categories enumerated as a readonly array — useful for
 * iterating column counts in the PII Viz matrix.
 */
export const PII_CATEGORIES: readonly PIICategory[] = [
  "contact",
  "financial",
  "payment_card",
  "health",
  "genetic",
  "biometric",
  "behavioral",
  "online_identifier",
  "credential",
  "government_id",
  "location",
  "demographic_protected",
] as const;

/**
 * Catastrophic-leak categories — the subset SchemaBrain refuses by
 * default and that `describe_entity` always redacts in column
 * descriptions. Stable closed set; additions are minor charter bumps.
 */
export const CATASTROPHIC_LEAK_CATEGORIES: readonly PIICategory[] = [
  "credential",
  "payment_card",
  "government_id",
] as const;

export function isCatastrophic(category: PIICategory): boolean {
  return CATASTROPHIC_LEAK_CATEGORIES.includes(category);
}

/**
 * Per-column PII tag — the (sensitivity, categories) pair the Python
 * side calls `ColumnPiiTag`. Serialised by the sidecar route
 * /api/entities/{name}/columns.
 */
export interface ColumnPiiTag {
  sensitivity: Sensitivity;
  pii_categories: readonly PIICategory[];
}

/* ───────── PII policy response (GET /api/pii/policy) ───────── */

export type PolicyOrigin = "heuristic" | "operator";

export type EffectiveEnforcement = "allowed" | "describe_blocked" | "blocked";

export type PolicyBlockSource = "yaml" | "default";

export interface PolicyCategoryRollup {
  category: PIICategory;
  column_count: number;
  entity_count: number;
  sample_columns: readonly string[];
  blocked_by_active_policy: boolean;
  blocked_by_catastrophic_floor: boolean;
}

export interface PolicyColumnEntry {
  qualified_table: string;
  column_name: string;
  qualified_column: string;
  sensitivity: Sensitivity;
  categories: readonly PIICategory[];
  origin: PolicyOrigin;
  effective_enforcement: EffectiveEnforcement;
}

export interface PolicyDiffPreview {
  current_blocked: number;
  if_all_blocked: number;
  if_catastrophic_only: number;
  if_none_blocked: number;
}

export interface PolicyResponse {
  source_connection_id: string;
  policy_path: string;
  block_source: PolicyBlockSource;
  active_block: readonly PIICategory[];
  catastrophic_floor: readonly PIICategory[];
  effective_block_for_describe: readonly PIICategory[];
  category_rollup: readonly PolicyCategoryRollup[];
  per_column: readonly PolicyColumnEntry[];
  diff_preview: PolicyDiffPreview;
  yaml_parse_error: string | null;
}
