// Pure matrix logic for the PII confidence heatmap — the column × category
// projection's derived view, kept framework-free (no React) so it is
// unit-testable in isolation.
//
// HONESTY CONTRACT (ADR 0009): the backend gives one ADVISORY band per
// column (`floor_locked`/`high`/`medium`/`low`/null) plus the column's set
// of categories. There is NO per-(column, category) score. So a cell is lit
// purely by category MEMBERSHIP, and its intensity is the column's single
// band, applied uniformly to that column's lit cells. A null band renders
// '—', never a faked 0. The "Status" verdict is a CLASSIFICATION-derived
// disposition (the always-on floor → blocked; other PII/confidential →
// redacted by default; public/internal → allowed); the operator's live,
// editable enforcement lives on /policy (ADR 0008). This module never
// fabricates a number the data can't back.

import { isCatastrophic, type PIICategory } from "@/lib/types";
import type { PiiMatrixColumn } from "@/lib/types";
import {
  isEmptyPolicyFilter,
  type PolicyFilter,
  prettyCategory,
} from "@/lib/policy";

/** The classification-derived disposition shown in the matrix's Status column. */
export type MatrixStatus = "block" | "redact" | "allow";

/** A heatmap cell's visual state at one (column, category) intersection. */
export type CellState = "floor" | "high" | "medium" | "low" | "nullband" | "unlit";

/** `<qualified_table>.<column_name>` — the stable identity of one matrix row. */
export function qualifiedColumn(col: PiiMatrixColumn): string {
  return `${col.qualified_table}.${col.column_name}`;
}

/** A column is floor-locked when any of its categories is catastrophic. The
 * floor is the only disposition the matrix asserts as a hard guarantee — it
 * is always enforced regardless of policy. */
export function isFloorColumn(col: PiiMatrixColumn): boolean {
  return col.categories.some(isCatastrophic);
}

/** True for columns carrying at least one PII category — the set the heatmap
 * shows by default (public/no-category columns have no sensitivity story and
 * flood the grid; a toggle reveals them). */
export function isPiiColumn(col: PiiMatrixColumn): boolean {
  return col.categories.length > 0;
}

/**
 * The column's classification-derived disposition:
 *   block  — a catastrophic-floor column (always enforced; the hard guarantee)
 *   redact — classified pii/confidential, not floor (descriptions redacted by
 *            default; the operator can tighten to block on /policy)
 *   allow  — public/internal, no sensitive classification
 *
 * This is the DEFAULT disposition from classification, not the operator's
 * live block-set (the matrix endpoint doesn't carry it). The floor → block
 * mapping never weakens real enforcement; a non-floor "redact" understates
 * only if the operator has additionally blocked that category on /policy.
 */
export function columnStatus(col: PiiMatrixColumn): MatrixStatus {
  if (isFloorColumn(col)) return "block";
  if (col.sensitivity === "pii" || col.sensitivity === "confidential") return "redact";
  return "allow";
}

/**
 * The heatmap cell state at (column, category). Unlit unless the category is
 * in the column's set (membership, not a score); a lit cell's intensity is
 * the column's single band. A classified column with no band → '—'
 * (`nullband`), never a faked 0.
 */
export function cellState(col: PiiMatrixColumn, category: PIICategory): CellState {
  if (!col.categories.includes(category)) return "unlit";
  switch (col.pii_confidence) {
    case "floor_locked":
      return "floor";
    case "high":
      return "high";
    case "medium":
      return "medium";
    case "low":
      return "low";
    default:
      return "nullband";
  }
}

/**
 * Floor columns first (pinned to the top as the alarm band), then the rest —
 * each group preserves the server's `(entity, column)` order. Pure +
 * order-stable; returns a new array.
 */
export function sortFloorFirst(
  columns: readonly PiiMatrixColumn[],
): readonly PiiMatrixColumn[] {
  const floor: PiiMatrixColumn[] = [];
  const rest: PiiMatrixColumn[] = [];
  for (const col of columns) (isFloorColumn(col) ? floor : rest).push(col);
  return [...floor, ...rest];
}

/**
 * Whether one column passes the active filter. `query` is a substring match
 * across qualified table / column / qualified column / prettified categories;
 * `category` matches the column's categories; `status` matches the derived
 * disposition (a `floor` status filter matches floor columns — they read as
 * blocked). Mirrors the /policy grid's filter semantics for consistency.
 */
export function matchesMatrixFilter(col: PiiMatrixColumn, filter: PolicyFilter): boolean {
  const query = filter.query.trim().toLowerCase();
  if (query) {
    const haystack = [
      col.qualified_table,
      col.column_name,
      qualifiedColumn(col),
      ...col.categories.map(prettyCategory),
    ]
      .join(" ")
      .toLowerCase();
    if (!haystack.includes(query)) return false;
  }
  if (filter.category && !col.categories.includes(filter.category)) return false;
  if (filter.status) {
    if (filter.status === "floor") {
      if (!isFloorColumn(col)) return false;
    } else if (columnStatus(col) !== filter.status) {
      return false;
    }
  }
  return true;
}

/** Filter a column listing (order-preserving). Pure. */
export function filterMatrixColumns(
  columns: readonly PiiMatrixColumn[],
  filter: PolicyFilter,
): readonly PiiMatrixColumn[] {
  if (isEmptyPolicyFilter(filter)) return columns;
  return columns.filter((col) => matchesMatrixFilter(col, filter));
}

/** Disposition tally over a column set — drives the summary strip. Computed
 * over the FULL set (never the filtered subset) so the headline counts don't
 * shift as the operator searches. */
export interface MatrixSummary {
  total: number;
  blocked: number;
  redacted: number;
  allowed: number;
}

export function summarizeMatrix(columns: readonly PiiMatrixColumn[]): MatrixSummary {
  const summary: MatrixSummary = { total: columns.length, blocked: 0, redacted: 0, allowed: 0 };
  for (const col of columns) {
    const status = columnStatus(col);
    if (status === "block") summary.blocked += 1;
    else if (status === "redact") summary.redacted += 1;
    else summary.allowed += 1;
  }
  return summary;
}

/** Screen-reader phrase for a cell — band beyond colour (WCAG 1.4.1). */
export function cellAriaLabel(col: PiiMatrixColumn, category: PIICategory): string {
  const id = `${qualifiedColumn(col)} · ${prettyCategory(category)}`;
  switch (cellState(col, category)) {
    case "floor":
      return `${id}: floor-locked — hard-blocked for every agent`;
    case "high":
      return `${id}: band high`;
    case "medium":
      return `${id}: band medium`;
    case "low":
      return `${id}: band low`;
    case "nullband":
      return `${id}: classified, no advisory band`;
    default:
      return `${id}: not classified`;
  }
}

/** Uppercase verb shown in the Status column. */
export function statusLabel(status: MatrixStatus): string {
  if (status === "block") return "BLOCKED";
  if (status === "redact") return "REDACT";
  return "ALLOW";
}
