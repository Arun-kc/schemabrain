// Pure policy-editor logic — staging math + preview-query encoding.
//
// Kept framework-free (no React) so it is unit-testable in isolation and
// carries the editor's correctness-critical bits: which categories are the
// always-on catastrophic floor, how a column override serialises onto the
// read-only preview route's wire (ADR 0006/0007), and how many levers are
// staged. The component (components/policy/PolicyEditor.tsx) holds the
// React state and renders; everything decision-shaped lives here.

import {
  type PIICategory,
  type PolicyColumnEntry,
  type Sensitivity,
  isCatastrophic,
} from "@/lib/types";

/** The sensitivity a "mark safe" override asserts (matches the engine's
 * card_number_last4 false-positive fix: `sensitivity: internal`). */
export const MARK_SAFE_SENSITIVITY: Sensitivity = "internal";

export interface StagedOverride {
  sensitivity: Sensitivity;
  categories: readonly PIICategory[];
}

export type StagedOverrides = ReadonlyMap<string, StagedOverride>;

/** A column is floor-locked when any of its categories is catastrophic. */
export function columnIsFloor(categories: readonly PIICategory[]): boolean {
  return categories.some(isCatastrophic);
}

/**
 * Encode one override for the preview route's `override` query param:
 * `<qualified_column>|<sensitivity>|<comma-joined categories>`. The
 * trailing field is empty for the canonical "mark safe" (no categories).
 * Categories are sorted so the same logical override always produces the
 * same string (stable query keys / cache hits).
 */
export function serializeOverride(qualifiedColumn: string, override: StagedOverride): string {
  const categories = [...override.categories].sort().join(",");
  return `${qualifiedColumn}|${override.sensitivity}|${categories}`;
}

/**
 * Build the `{block, override}` query payload for api.piiPolicyPreview
 * from the staged levers. Both arrays are sorted so identical staged
 * state yields an identical request (TanStack Query key stability).
 */
export function buildPreviewQuery(
  stagedBlock: ReadonlySet<PIICategory>,
  stagedOverrides: StagedOverrides,
): { block: string[]; override: string[] } {
  const block = [...stagedBlock].sort();
  const override = [...stagedOverrides.entries()]
    .map(([qualifiedColumn, value]) => serializeOverride(qualifiedColumn, value))
    .sort();
  return { block, override };
}

/**
 * Reconstruct the current operator overrides from the `/api/pii/policy`
 * per-column listing. Operator-origin rows ARE the on-disk
 * `column_overrides`; seeding staged state from them means an untouched
 * editor reproduces the current policy exactly (preview `changed=false`).
 * Heuristic rows are NOT overrides — they stay out of the map.
 */
export function initialOverridesFromPerColumn(
  perColumn: readonly PolicyColumnEntry[],
): Map<string, StagedOverride> {
  const map = new Map<string, StagedOverride>();
  for (const row of perColumn) {
    if (row.origin === "operator") {
      map.set(row.qualified_column, {
        sensitivity: row.sensitivity,
        categories: [...row.categories],
      });
    }
  }
  return map;
}

/** True when a column is currently staged as "marked safe" (override with
 * the mark-safe sensitivity and no categories). */
export function isMarkedSafe(
  qualifiedColumn: string,
  stagedOverrides: StagedOverrides,
): boolean {
  const override = stagedOverrides.get(qualifiedColumn);
  return override !== undefined && override.categories.length === 0;
}

/**
 * Immutably stage (or clear) a "mark safe" override for one column.
 * `safe=true` asserts `{internal, []}`; `safe=false` drops the override
 * so the column reverts to the heuristic classification.
 */
export function toggleMarkSafe(
  stagedOverrides: StagedOverrides,
  qualifiedColumn: string,
  safe: boolean,
): Map<string, StagedOverride> {
  const next = new Map(stagedOverrides);
  if (safe) {
    next.set(qualifiedColumn, { sensitivity: MARK_SAFE_SENSITIVITY, categories: [] });
  } else {
    next.delete(qualifiedColumn);
  }
  return next;
}

/** Immutably add/remove a non-floor category from the staged block set. */
export function toggleBlockCategory(
  stagedBlock: ReadonlySet<PIICategory>,
  category: PIICategory,
): Set<PIICategory> {
  const next = new Set(stagedBlock);
  if (next.has(category)) {
    next.delete(category);
  } else {
    next.add(category);
  }
  return next;
}

/** Count the levers that differ between two override maps. */
function countOverrideChanges(initial: StagedOverrides, staged: StagedOverrides): number {
  const keys = new Set<string>([...initial.keys(), ...staged.keys()]);
  let changed = 0;
  for (const key of keys) {
    if (serializeOne(initial, key) !== serializeOne(staged, key)) changed += 1;
  }
  return changed;
}

function serializeOne(map: StagedOverrides, key: string): string | null {
  const value = map.get(key);
  return value === undefined ? null : serializeOverride(key, value);
}

/** True when one column's staged override differs from its baseline —
 * drives the per-row "pending" highlight. */
export function isColumnOverrideChanged(
  initial: StagedOverrides,
  staged: StagedOverrides,
  qualifiedColumn: string,
): boolean {
  return serializeOne(initial, qualifiedColumn) !== serializeOne(staged, qualifiedColumn);
}

/**
 * Number of staged changes (category toggles + override edits) versus the
 * initial baseline — drives the "N staged" count and gates Apply/Discard.
 */
export function countStagedChanges(
  initialBlock: ReadonlySet<PIICategory>,
  stagedBlock: ReadonlySet<PIICategory>,
  initialOverrides: StagedOverrides,
  stagedOverrides: StagedOverrides,
): number {
  const blockCategories = new Set<PIICategory>([...initialBlock, ...stagedBlock]);
  let blockChanges = 0;
  for (const category of blockCategories) {
    if (initialBlock.has(category) !== stagedBlock.has(category)) blockChanges += 1;
  }
  return blockChanges + countOverrideChanges(initialOverrides, stagedOverrides);
}
