// Pure stat helpers for the Overview surface. Kept out of the component so
// the percentage / scaling / "is-rated" logic is unit-tested directly
// (the component itself is covered by the e2e spec).

import type { OverviewConfidence } from "@/lib/types";

/**
 * `value` as a percentage of `total`, clamped to [0, 100]. Returns 0 when
 * `total` is 0 so an empty source renders a flat bar, never NaN%.
 */
export function toPercent(value: number, total: number): number {
  if (total <= 0) return 0;
  return Math.min(100, Math.max(0, (value / total) * 100));
}

/**
 * The largest value, or 0 when empty. Used to scale the entities-by-group
 * bars to the busiest group.
 */
export function maxCount(values: readonly number[]): number {
  return values.length === 0 ? 0 : Math.max(...values);
}

/**
 * True when at least one entity carries a model confidence band
 * (high/medium/low). Hand-authored entities are `unrated`, so an all-unrated
 * source returns false — the card shows an honest "no model rating" note
 * instead of an empty distribution bar.
 */
export function confidenceRated(confidence: OverviewConfidence): boolean {
  return confidence.high + confidence.medium + confidence.low > 0;
}
