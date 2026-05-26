// SOURCE: schemabrain/mcp/envelope.py — DO NOT EDIT WITHOUT BUMPING CHARTER
//
// Mirrors of the Pydantic models + Literal types in the Python charter.
// Drift between this file and envelope.py is asserted in
// tests/dashboard/test_ts_charter_sync.py — a literal added on the
// Python side without a matching update here fails CI.
//
// The dashboard sidecar reconstructs `ToolResponse` shapes for the
// /api/audit/refusals/{id} route from the audit row's structured
// columns (the historical full envelope is not persisted by design).
// Surface components consume the shape; they should not depend on
// the reconstruction vs. live distinction.

import type { PIICategory } from "./pii";

/** ToolResponse status — 6 values, charter v1.1+ adds "refused". */
export type Status =
  | "success"
  | "empty"
  | "partial"
  | "degraded"
  | "error"
  | "refused";

/** 1D trust label. Derived from (InferenceMethod, ValidationState) in v1.2. */
export type Confidence = "HIGH" | "MEDIUM" | "LOW";

/** v1.2 — how a fact was derived. Names the production path. */
export type InferenceMethod =
  | "manually_authored"
  | "llm_suggested"
  | "fk_constraint"
  | "dbt_import"
  | "observed_in_query_log";

/** v1.2 — how validated a fact is. Orthogonal to InferenceMethod. */
export type ValidationState = "draft" | "applied" | "confirmed";

/** v1.0 1D provenance label kept for back-compat with older clients. */
export type ProvenanceSource = "schema" | "llm" | "inferred";

/** Closed set of error kinds the agent can switch on. 26 total in v1.2. */
export type ErrorKind =
  // v1.0
  | "unknown_name"
  | "malformed_name"
  | "missing_credential"
  | "index_not_ready"
  | "schema_drift"
  | "cost_cap_exceeded"
  | "internal_error"
  // v1.1 refuse-before-execute
  | "pii_blocked"
  | "policy_blocked"
  | "allowlist_violation"
  // canonical-join surface
  | "no_canonical_join"
  | "ambiguous_join"
  | "unknown_join_name"
  | "join_name_mismatch"
  // metric surface
  | "unknown_metric"
  | "unreachable_entity"
  | "ambiguous_path"
  | "unknown_via_join"
  | "unknown_order_by_column"
  | "unknown_group_by_column"
  | "unknown_filter_column"
  | "unknown_measure_column"
  | "invalid_time_grain"
  | "grain_mismatch"
  // v1.2 time-dimension surface
  | "ambiguous_time_dimension"
  // MCP dispatch surface (internal — never seen by agent in ToolResponse)
  | "invalid_argument";

/** Closed-grammar reason for status="degraded". */
export type DegradationReason =
  | "fan_out_join"
  | "missing_order_by_with_limit"
  | "time_dimension_unavailable";

/** Subset of ErrorKinds that support v1.1 Recovery rewrites + widening hints. */
export const REFUSAL_KINDS: readonly ErrorKind[] = [
  "pii_blocked",
  "policy_blocked",
  "allowlist_violation",
] as const;

/** Per-response provenance annotation. */
export interface Provenance {
  source: ProvenanceSource;
  model?: string | null;
  observed_in?: Record<string, unknown> | null;
  inference_method?: InferenceMethod | null;
  validation_state?: ValidationState | null;
}

/** Suggested rewrite the agent can adopt after a refusal (v1.1). */
export interface SuggestedRewrite {
  rewritten: string;
  reason: string;
}

/** Scope-widening hint after an allowlist_violation (v1.1). */
export interface WideningHint {
  scope_requested: string;
  would_unblock: boolean;
  reason: string;
}

/** Recovery hints — the agent's next move after an error or refusal. */
export interface Recovery {
  suggested_tool?: string | null;
  suggested_args?: Record<string, unknown> | null;
  fuzzy_matches: readonly string[];
  suggested_rewrite?: SuggestedRewrite | null;
  widening_hint?: WideningHint | null;
}

/** Structured error inside a ToolResponse with status="error" or "refused". */
export interface ToolError {
  kind: ErrorKind;
  message: string;
  recovery: Recovery;
  pii_categories: readonly PIICategory[];
}

/** The envelope every SchemaBrain MCP tool returns. */
export interface ToolResponse<T = unknown> {
  status: Status;
  data: T | null;
  error: ToolError | null;
  confidence: Confidence | null;
  provenance?: Provenance | null;
  follow_up_hints?: readonly string[] | null;
  degradation_reason?: DegradationReason | null;
  charter_version: string;
}

/**
 * Derive the 1D Confidence label from the 2D trust signal.
 *
 * Matches `schemabrain.mcp.envelope.derive_confidence` byte-for-byte;
 * dashboard components that show inline trust badges call this
 * helper rather than encoding the matrix in JSX.
 */
export function deriveConfidence(
  inferenceMethod: InferenceMethod,
  validationState: ValidationState,
): Confidence {
  if (validationState === "confirmed") return "HIGH";
  if (validationState === "applied") {
    if (
      inferenceMethod === "manually_authored" ||
      inferenceMethod === "fk_constraint" ||
      inferenceMethod === "dbt_import"
    ) {
      return "HIGH";
    }
    return "MEDIUM";
  }
  // draft
  if (
    inferenceMethod === "manually_authored" ||
    inferenceMethod === "fk_constraint" ||
    inferenceMethod === "dbt_import"
  ) {
    return "MEDIUM";
  }
  return "LOW";
}
