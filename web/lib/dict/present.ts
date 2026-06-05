// Pure presentation helpers for the Data Dictionary surface — the PII
// derivations and chip mapping the browse view needs. Kept out of the
// component so they are unit-tested and never re-derive catastrophic-ness
// from a chip-kind string (gate on `isCatastrophic`, per the PII charter).

import type { DictColumn, DictEntity, Group, PIICategory } from "@/lib/types";
import { isCatastrophic } from "@/lib/types";
import type { PiiChipKind } from "@/components/kit";

/** Decisive PII severity for an entity, derived from its columns. */
export type EntityPiiLevel = "catastrophic" | "pii" | "none";

/** Fixed left-index group order; "other" sinks last. */
export const DICT_GROUP_ORDER: readonly Group[] = ["identity", "billing", "activity", "other"];

export function columnIsCatastrophic(column: DictColumn): boolean {
  return column.pii_categories.some(isCatastrophic);
}

export function columnHasPii(column: DictColumn): boolean {
  return column.pii_categories.length > 0 || column.pii_sensitivity === "pii";
}

/** Catastrophic if any column is on the floor; else pii if any carries PII. */
export function entityPiiLevel(entity: DictEntity): EntityPiiLevel {
  if (entity.columns.some(columnIsCatastrophic)) return "catastrophic";
  if (entity.columns.some(columnHasPii)) return "pii";
  return "none";
}

/** The index dot colour for an entity; null = no dot (no PII). */
export function entityDotVar(entity: DictEntity): string | null {
  const level = entityPiiLevel(entity);
  if (level === "catastrophic") return "var(--alarm)";
  if (level === "pii") return "var(--cyan)";
  return null;
}

/** Columns carrying at least one PII category — the entry-head flag chips. */
export function piiColumns(entity: DictEntity): readonly DictColumn[] {
  return entity.columns.filter((column) => column.pii_categories.length > 0);
}

/**
 * The category that should label a column: a catastrophic-floor tag wins,
 * else the first tag, else null (no PII). Drives the column's single chip.
 */
export function leadCategory(column: DictColumn): PIICategory | null {
  return column.pii_categories.find(isCatastrophic) ?? column.pii_categories[0] ?? null;
}

/**
 * Chip variant for a PII category. Only the catastrophic floor takes an
 * alarm-coloured chip; `contact` its amber chip; everything else stays muted.
 */
export function categoryChipKind(category: PIICategory): PiiChipKind {
  if (isCatastrophic(category)) return category === "payment_card" ? "payment" : "auth";
  if (category === "contact") return "contact";
  return "neutral";
}
