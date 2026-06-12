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
 * Index-time PII-confidence band (ADR 0009) — an ADVISORY signal on a
 * classified column, never an enforcement gate. `floor_locked` marks a
 * catastrophic-floor column (locked); `high`/`medium`/`low` grade a
 * non-floor classification's corroboration. The classifier emits only
 * `floor_locked`/`high`/`medium` in v1 — `low` stays reserved (withholding
 * it under-states sensitivity, the safe direction). A column with no band
 * (legacy / SQLite / unclassified) serialises as null and renders '—',
 * never a faked 0. The raw 0..1 score stays internal: only the band crosses
 * the API.
 *
 * SOURCE: schemabrain/pii/categories.py PiiConfidenceBand — DO NOT EDIT
 * WITHOUT BUMPING CHARTER (test_ts_charter_sync).
 */
export type PiiConfidenceBand = "floor_locked" | "high" | "medium" | "low";

export const PII_CONFIDENCE_BANDS: readonly PiiConfidenceBand[] = [
  "floor_locked",
  "high",
  "medium",
  "low",
] as const;

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

export type EffectiveEnforcement = "allowed" | "floor_blocked" | "blocked";

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

/**
 * Drift between schemabrain serve's recorded YAML mtime and the
 * current on-disk YAML mtime. `detected=true` means the operator
 * edited `pii_policy.yaml` after serve booted, so the running
 * firewall is enforcing a stale policy; the dashboard surfaces a
 * "restart `schemabrain serve`" banner. Both timestamps are ISO8601
 * UTC; either may be null when serve isn't running or there's no
 * YAML on disk.
 */
export interface PolicyDriftState {
  detected: boolean;
  recorded_at: string | null;
  current_mtime: string | null;
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
  policy_drift: PolicyDriftState;
}

/* ───────── Policy preview (GET /api/pii/policy/preview) ───────── */

export type PolicyDiffKind = "context" | "add" | "remove";

export interface PolicyDiffLine {
  kind: PolicyDiffKind;
  text: string;
}

/**
 * Server-rendered preview of a *staged* policy (ADR 0006/0007). The
 * editor posts its staged block set + column overrides as query params
 * and the sidecar returns the canonical `policy_to_yaml` rendering plus
 * the line diff against the current on-disk policy. The dashboard never
 * re-implements the YAML grammar — `staged_yaml` is the byte-exact text
 * `schemabrain policy apply` will parse.
 */
export interface PolicyPreviewResponse {
  staged_yaml: string;
  current_yaml: string;
  diff_lines: readonly PolicyDiffLine[];
  changed: boolean;
  staged_block: readonly PIICategory[];
  current_parse_error: string | null;
}
