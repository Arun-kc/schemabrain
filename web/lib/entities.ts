// Pure entity-surface view logic — group labelling/colour, row-count
// formatting, the categorical bind-confidence pill, the PII-level chip, and the
// Entities-index sort comparator. Framework-free (no React) so it is
// unit-testable in isolation. Sibling to `lib/refusals.ts` / `lib/piiMatrix.ts`.
//
// HONESTY CONTRACT: bind confidence is CATEGORICAL ({high,medium,low}) and is
// rendered as a labelled band — never a numeric percentage or a synthetic
// meter (ADR 0009 / v15 lock). A null confidence (hand-authored entity) returns
// null so the caller OMITS the pill rather than fabricating a band. Row counts
// are the cached estimate or "—" when unknown — a real 0-row table still shows
// "0", but an unknown count is never rendered as a fabricated 0.

import { isCatastrophic } from "@/lib/types";
import type {
  BindConfidence,
  Cardinality,
  EntityColumn,
  EntityListItem,
  Group,
  PIICategory,
  PiiLevel,
} from "@/lib/types";

/** Human label for a cosmetic entity group (handoff `GROUP_LABEL`). */
export const GROUP_LABEL: Record<Group, string> = {
  identity: "Identity",
  billing: "Billing & revenue",
  activity: "Activity & logs",
  other: "Other",
};

export function groupLabel(group: Group): string {
  return GROUP_LABEL[group];
}

/**
 * Header group-chip kind (handoff: identity → green, billing → cyan, everything
 * else neutral). The group is purely cosmetic (ADR 0010/0011) — it carries no
 * enforcement meaning, so the chip never reads as policy-relevant.
 */
export function groupChipKind(group: Group): "green" | "cyan" | "neutral" {
  if (group === "identity") return "green";
  if (group === "billing") return "cyan";
  return "neutral";
}

const GROUP_DOT: Record<Group, string> = {
  identity: "var(--green)",
  billing: "var(--cyan)",
  activity: "var(--violet)",
  other: "var(--ink-3)",
};

/**
 * Index status-dot colour token. The catastrophic floor always overrides the
 * cosmetic group colour with the alarm rail — the floor is the one signal that
 * outranks the group (handoff `GCOL` + the `pii === "catastrophic"` override).
 */
export function groupDotVar(group: Group, piiLevel: PiiLevel): string {
  return piiLevel === "catastrophic" ? "var(--alarm)" : GROUP_DOT[group];
}

function trimDecimal(value: number): string {
  // 94.0 → "94", 1.2 → "1.2"
  return Number(value.toFixed(1)).toString();
}

/**
 * Compact row-count display: "1.2M" / "12.5k" / "999". `null` (unknown — SQLite
 * source, never-analyzed table, hand-authored entity) renders "—", never a
 * fabricated 0. A genuine 0-row table renders "0".
 */
export function formatRowCount(rows: number | null): string {
  if (rows === null) return "—";
  if (rows >= 1_000_000) return `${trimDecimal(rows / 1_000_000)}M`;
  if (rows >= 1_000) {
    // Guard the boundary: 999_950..999_999 rounds to "1000" under toFixed(1) —
    // promote to "1M" rather than emit a malformed "1000k".
    const thousands = trimDecimal(rows / 1_000);
    return thousands === "1000" ? `${trimDecimal(rows / 1_000_000)}M` : `${thousands}k`;
  }
  return String(rows);
}

/** A categorical bind-confidence band rendered as a labelled chip. */
export interface ConfidenceDisplay {
  label: string;
  kind: "green" | "cyan" | "neutral";
}

const CONFIDENCE_DISPLAY: Record<BindConfidence, ConfidenceDisplay> = {
  high: { label: "High", kind: "green" },
  medium: { label: "Medium", kind: "cyan" },
  low: { label: "Low", kind: "neutral" },
};

/**
 * Categorical bind-confidence → pill label + chip kind. `null` (hand-authored
 * entity) returns `null` so the caller omits the pill. Alarm is reserved for
 * the catastrophic floor, so a low band reads muted (neutral), not alarm.
 */
export function confidenceDisplay(confidence: BindConfidence | null): ConfidenceDisplay | null {
  return confidence === null ? null : CONFIDENCE_DISPLAY[confidence];
}

const CONFIDENCE_RANK: Record<BindConfidence, number> = { high: 3, medium: 2, low: 1 };

/** Sort rank for a band; `null` (hand-authored) ranks below `low`. */
export function confidenceRank(confidence: BindConfidence | null): number {
  return confidence === null ? 0 : CONFIDENCE_RANK[confidence];
}

const PII_LEVEL_RANK: Record<PiiLevel, number> = {
  catastrophic: 4,
  pii: 3,
  confidential: 2,
  internal: 1,
  none: 0,
};

/** Sort rank for the decisive PII severity badge (catastrophic outranks all). */
export function piiLevelRank(level: PiiLevel): number {
  return PII_LEVEL_RANK[level];
}

/** PII-level badge label + chip kind. Alarm (`auth`) is reserved for the
 * catastrophic floor; `pii` reads amber (`contact`); lower levels stay muted. */
export interface PiiLevelChip {
  label: string;
  kind: "auth" | "contact" | "neutral" | "none";
}

const PII_LEVEL_CHIP: Record<PiiLevel, PiiLevelChip> = {
  catastrophic: { label: "Catastrophic", kind: "auth" },
  pii: { label: "PII", kind: "contact" },
  confidential: { label: "Confidential", kind: "neutral" },
  internal: { label: "Internal", kind: "neutral" },
  none: { label: "None", kind: "none" },
};

export function piiLevelChip(level: PiiLevel): PiiLevelChip {
  return PII_LEVEL_CHIP[level];
}

const CARDINALITY_LABEL: Record<Cardinality, string> = {
  one_to_one: "1:1",
  one_to_many: "1:N",
  many_to_one: "N:1",
  many_to_many: "N:N",
};

/** Compact cardinality badge ("N:1"); `null` (authored before the column
 * existed) returns null so the caller omits the badge. */
export function cardinalityLabel(cardinality: Cardinality | null): string | null {
  return cardinality === null ? null : CARDINALITY_LABEL[cardinality];
}

/**
 * Whether a column carries any catastrophic-floor category — drives the alarm
 * row tint in the drilldown's physical pane. Uses the data-model category enum
 * via `isCatastrophic`, NOT the chip-kind strings (the
 * `piiCategoryToChipKind` footgun).
 */
export function columnIsCatastrophic(categories: readonly PIICategory[]): boolean {
  return categories.some(isCatastrophic);
}

/** A PII category as an uppercased badge label ("payment_card" → "PAYMENT CARD"). */
export function categoryLabel(category: PIICategory): string {
  return category.replace(/_/g, " ").toUpperCase();
}

/** One (source column → nearest neighbour) pair for the drilldown's
 * embeddings section — the per-column `similar` lists flattened across the
 * entity, each tagged with its source column. No free-text reason (the route
 * deliberately ships only the structured neighbour + score). */
export interface SimilarPair {
  /** The entity's own column the neighbour is near. */
  source: string;
  schema: string;
  table: string;
  column: string;
  /** Cosine similarity in [-1, 1]. */
  score: number;
}

/**
 * Flatten every column's nearest-neighbour list into entity-level pairs,
 * highest score first, capped at `limit`. Returns the shown pairs plus the
 * hidden count so the caller can disclose the truncation (never a silent cap).
 */
export function topSimilarColumns(
  columns: readonly EntityColumn[],
  limit: number,
): { shown: SimilarPair[]; hidden: number } {
  const pairs: SimilarPair[] = columns.flatMap((col) =>
    col.similar.map((s) => ({
      source: col.name,
      schema: s.schema,
      table: s.table,
      column: s.column,
      score: s.score,
    })),
  );
  pairs.sort((a, b) => b.score - a.score);
  const shown = pairs.slice(0, limit);
  return { shown, hidden: pairs.length - shown.length };
}

/** The sortable columns of the Entities index. */
export type EntitySortKey =
  | "name"
  | "group"
  | "rows"
  | "pii_level"
  | "confidence"
  | "metrics_count"
  | "joins_count";

export interface EntitySort {
  key: EntitySortKey;
  /** 1 = ascending, -1 = descending. */
  dir: 1 | -1;
}

/** The default index sort — largest tables first (handoff `{ k: "rows", dir: -1 }`). */
export const DEFAULT_ENTITY_SORT: EntitySort = { key: "rows", dir: -1 };

function sortValue(item: EntityListItem, key: EntitySortKey): number | string {
  switch (key) {
    case "name":
      return item.name;
    case "group":
      return groupLabel(item.group);
    case "rows":
      // null (unknown) ranks below a real 0-row table.
      return item.rows ?? -1;
    case "pii_level":
      return piiLevelRank(item.pii_level);
    case "confidence":
      return confidenceRank(item.confidence);
    case "metrics_count":
      return item.metrics_count;
    case "joins_count":
      return item.joins_count;
    default:
      // Exhaustiveness sentinel — a new sort key must add a case here.
      key satisfies never;
      return item.name;
  }
}

/** Pure comparator for two index rows under a given sort. */
export function compareEntities(a: EntityListItem, b: EntityListItem, sort: EntitySort): number {
  const av = sortValue(a, sort.key);
  const bv = sortValue(b, sort.key);
  if (typeof av === "string" && typeof bv === "string") {
    return av.localeCompare(bv) * sort.dir;
  }
  return ((av as number) - (bv as number)) * sort.dir;
}

/** Sort a copy of the index rows (immutable; V8 sort is stable so ties keep
 * their server order). */
export function sortEntities(
  items: readonly EntityListItem[],
  sort: EntitySort,
): EntityListItem[] {
  return [...items].sort((a, b) => compareEntities(a, b, sort));
}

/**
 * Toggle direction when re-selecting the active column, else switch to the new
 * column descending-first (handoff `setS`). Pure — returns the next sort state.
 */
export function nextSort(current: EntitySort, key: EntitySortKey): EntitySort {
  if (current.key === key) {
    return { key, dir: current.dir === 1 ? -1 : 1 };
  }
  return { key, dir: -1 };
}
