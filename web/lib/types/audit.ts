// SOURCE: schemabrain/audit/*.py — DO NOT EDIT WITHOUT BUMPING CHARTER
//
// Mirrors of the audit substrate Python types: cost class, refusal
// reason, the per-row AuditRow shape, and the chain-walker mismatch
// shape. Plus dashboard-derived shapes (AuditChainStatus, RefusalEntry)
// the sidecar JSON routes serialize.

import type { PIICategory } from "./pii";
import type { ToolResponse } from "./envelope";

/** Cost class stamped on every audit row. */
export type CostClass = "small" | "medium" | "large" | "refused";

/** Refusal reason — the 6 closed values from ADR 0001. */
export type RefusalReason =
  | "pii_blocked"
  | "allowlist_violation"
  | "fragment_unsafe"
  | "cost_cap_exceeded"
  | "ambiguous_resolution"
  | "schema_drift";

/**
 * One row in the mcp_audit table, JSON-serialised. Mirrors
 * `schemabrain.audit.writer.AuditRow`, with bytes-typed fields
 * surfaced as hex strings (the sidecar does the conversion).
 *
 * `pii_categories` is the CSV-decoded array form rather than the
 * comma-string the DB stores — easier to render as chips.
 */
export interface AuditRow {
  id: number;
  occurred_at: string;
  source_connection_id: string;
  caller_id: string | null;
  tool_name: string;
  status: "success" | "empty" | "partial" | "degraded" | "error" | "refused";
  refusal_reason: RefusalReason | null;
  cost_class: CostClass;
  pii_categories: readonly PIICategory[];
  ast_shape_hash_hex: string | null;
  rule_id: string | null;
  fingerprint_hex: string;
  fingerprint_version: string;
  chain_hash_hex: string;
}

/** One detected mismatch between stored + recomputed chain hash. */
export interface ChainMismatch {
  row_id: number;
  expected_hex: string;
  actual_hex: string;
}

/**
 * Aggregated chain status returned by /api/audit/verify. The chain
 * ribbon in the Audit Viewer surface renders this directly.
 */
export interface AuditChainStatus {
  status: "intact" | "broken";
  walked_through_id: number | null;
  cursor_id: number | null;
  mismatches: readonly ChainMismatch[];
}

/**
 * Refusal entry — an audit row + reconstructed envelope view that
 * the Refusal UI feed renders. `envelope_reconstructed` is true
 * when the sidecar rebuilt the envelope from the row's structured
 * columns (the historical verbatim envelope is not persisted by
 * design — see privacy-by-construction in audit/fingerprint.py).
 */
export interface RefusalEntry extends AuditRow {
  envelope_reconstructed: boolean;
  envelope?: ToolResponse<null>;
}

/** Detail payload for /api/audit/rows/{id}. */
export interface AuditRowDetail extends AuditRow {
  prev_chain_hash_hex: string | null;
}

/**
 * Derived RFC-6962 Merkle root over every audit row (id-ASC). Returned by
 * /api/audit/merkle/root. `root_hex` is always 64 lowercase hex chars;
 * the empty log yields the SHA-256 of the empty string, never null.
 */
export interface MerkleRootResponse {
  tree_size: number;
  root_hex: string;
}

/**
 * Inclusion proof for one audit row, returned by
 * /api/audit/rows/{id}/proof. `leaf_index` is the 0-based position in
 * id-ASC order (NOT the id-DESC render order of /api/audit/rows).
 * `audit_path` is the leaf->root sibling-hash list (empty for a single-row
 * tree); the browser folds it with `web/lib/merkle.ts` and reconciles to
 * the global root. `root_hex` is the root of the tree as computed when this
 * proof was served.
 */
export interface MerkleProofResponse {
  leaf_index: number;
  tree_size: number;
  root_hex: string;
  leaf_hash_hex: string;
  audit_path: readonly string[];
}

/** Paginated wrapper for /api/audit/rows and /api/audit/refusals. */
export interface PaginatedAuditResponse<T> {
  items: readonly T[];
  limit: number;
  offset: number;
  total: number;
  since_cursor_id?: number | null;
}
