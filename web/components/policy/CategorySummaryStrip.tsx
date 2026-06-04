"use client";

import { Icon } from "@/components/kit";
import { cn } from "@/lib/cn";
import { type PolicyCategorySummary, prettyCategory } from "@/lib/policy";
import { type PIICategory } from "@/lib/types";
import styles from "./policy-editor.module.css";

/**
 * CategorySummaryStrip — a per-category overview above the grid. Because
 * `block`/`redact` are category-wide anyway (the engine blocks by category,
 * ADR 0008), each cell is a one-click toggle that blocks/un-blocks the whole
 * category at once — leaning into the engine's true grain. Catastrophic-floor
 * categories render locked and can't be toggled off; per-column `allow`
 * overrides stay immune (the toggle only touches the block set).
 *
 * The strip is computed over the FULL per-column listing (never the filtered
 * view) so it always reflects the schema's true category posture.
 */
export function CategorySummaryStrip({
  summaries,
  onToggleCategory,
}: {
  summaries: readonly PolicyCategorySummary[];
  onToggleCategory: (category: PIICategory, block: boolean) => void;
}) {
  if (summaries.length === 0) return null;

  return (
    <section className={styles.strip} aria-label="category overview">
      {summaries.map((summary) => (
        <CategoryCell key={summary.category} summary={summary} onToggle={onToggleCategory} />
      ))}
    </section>
  );
}

function CategoryCell({
  summary,
  onToggle,
}: {
  summary: PolicyCategorySummary;
  onToggle: (category: PIICategory, block: boolean) => void;
}) {
  const { category, total, blocked, inBlockSet, isCatastrophic } = summary;
  const pretty = prettyCategory(category);
  const countLabel = `${total} ${total === 1 ? "col" : "cols"}`;

  if (isCatastrophic) {
    return (
      <div className={cn(styles.stripCell, styles.stripFloor)} aria-label={`${pretty} — locked floor`}>
        <span className={styles.stripCat}>
          <Icon name="lock" size={11} /> {pretty}
        </span>
        <span className={styles.stripMeta}>{countLabel} · floor</span>
      </div>
    );
  }

  return (
    <button
      type="button"
      className={cn(styles.stripCell, styles.stripToggle, inBlockSet && styles.stripOn)}
      aria-pressed={inBlockSet}
      aria-label={`${inBlockSet ? "un-block" : "block"} all ${pretty} columns`}
      onClick={() => onToggle(category, !inBlockSet)}
    >
      <span className={styles.stripCat}>{pretty}</span>
      <span className={styles.stripMeta}>
        {countLabel}
        {inBlockSet ? ` · ${blocked} blocked` : ""}
      </span>
    </button>
  );
}
