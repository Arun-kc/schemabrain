// Pure policy-editor logic — the per-column verb state machine + YAML
// highlighter, kept framework-free (no React) so it is unit-testable in
// isolation.
//
// The Policy page matches the design handoff exactly: a single per-column
// grid with a 3-way `block | redact | allow` control (ADR 0008). The
// engine, however, blocks by *category*, not per-column, and the canonical
// policy is `block:` (categories) + `column_overrides:` (per-column
// reclassification). So the grid is a CLIENT PROJECTION over that real
// model — this module maps each column's verb to/from the staged
// (blockSet × overrides) pair and is the single source of truth for that
// mapping. See ADR 0008 for the honest-mapping rationale + caveats.

import {
  CATASTROPHIC_LEAK_CATEGORIES,
  isCatastrophic,
  type PIICategory,
  type PolicyColumnEntry,
  type Sensitivity,
} from "@/lib/types";

/** The sensitivity an `allow` verb asserts (matches the engine's
 * card_number_last4 false-positive fix: `sensitivity: internal`). */
export const MARK_SAFE_SENSITIVITY: Sensitivity = "internal";

/** Per-column control states. `floor` is the locked catastrophic state. */
export type PolicyVerb = "block" | "redact" | "allow";

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
 * The column's effective categories under the staged state — the override's
 * categories if one is staged, otherwise its base (classifier/operator)
 * categories. This is what the block intersection is computed against.
 */
function effectiveCategories(
  baseCategories: readonly PIICategory[],
  stagedOverrides: StagedOverrides,
  qualifiedColumn: string,
): readonly PIICategory[] {
  const override = stagedOverrides.get(qualifiedColumn);
  return override ? override.categories : baseCategories;
}

/**
 * Derive a column's displayed verb from the staged state. Floor columns
 * (any catastrophic base category) are locked — callers render them as a
 * non-interactive `floor · locked` row rather than a 3-way control.
 *
 *   block   — an effective category is in the block set (refused at the gate)
 *   allow   — a `categories: []` override asserts the column non-sensitive
 *   redact  — classified but not blocked (aggregates allowed; the floor
 *             still redacts descriptions where a catastrophic tag applies)
 */
export function columnVerb(
  qualifiedColumn: string,
  baseCategories: readonly PIICategory[],
  stagedBlock: ReadonlySet<PIICategory>,
  stagedOverrides: StagedOverrides,
): PolicyVerb | "floor" {
  if (columnIsFloor(baseCategories)) return "floor";
  const cats = effectiveCategories(baseCategories, stagedOverrides, qualifiedColumn);
  if (cats.some((c) => stagedBlock.has(c))) return "block";
  const override = stagedOverrides.get(qualifiedColumn);
  if (override && override.categories.length === 0) return "allow";
  return "redact";
}

/**
 * Apply a verb to one column, returning the new staged (block, overrides).
 * Immutable — inputs are never mutated.
 *
 *   block   — add the column's base categories to the block set (CATEGORY-
 *             WIDE: sibling columns sharing those categories also become
 *             blocked) and drop any override.
 *   redact  — remove the column's base categories from the block set (also
 *             category-wide) and drop any override → classified-not-blocked.
 *   allow   — add a `{internal, []}` override (per-column; leaves the block
 *             set untouched so only THIS column is exempted).
 */
export function applyVerb(
  stagedBlock: ReadonlySet<PIICategory>,
  stagedOverrides: StagedOverrides,
  qualifiedColumn: string,
  baseCategories: readonly PIICategory[],
  verb: PolicyVerb,
): { block: Set<PIICategory>; overrides: Map<string, StagedOverride> } {
  const block = new Set(stagedBlock);
  const overrides = new Map(stagedOverrides);
  if (verb === "block") {
    for (const c of baseCategories) block.add(c);
    overrides.delete(qualifiedColumn);
  } else if (verb === "redact") {
    for (const c of baseCategories) block.delete(c);
    overrides.delete(qualifiedColumn);
  } else {
    overrides.set(qualifiedColumn, { sensitivity: MARK_SAFE_SENSITIVITY, categories: [] });
  }
  return { block, overrides };
}

/**
 * How many OTHER non-floor columns would actually FLIP enforcement verb if
 * this column's category-wide verb were toggled to its opposite (block↔
 * redact) — i.e. the true category-wide blast radius, NOT raw category
 * overlap. Computed by simulating the toggle (`applyVerb`) and diffing each
 * sibling's derived `columnVerb` before vs after.
 *
 * This precisely excludes siblings that DON'T move: ones on an `allow`
 * override (immune — effective categories []), ones kept blocked by a
 * second blocked category, and ones already in the target state. Drives the
 * inline disclosure note, which must not over-claim (ADR 0008 §3).
 */
export function siblingsAffectedByVerb(
  perColumn: readonly PolicyColumnEntry[],
  qualifiedColumn: string,
  baseCategories: readonly PIICategory[],
  verb: PolicyVerb,
  stagedBlock: ReadonlySet<PIICategory>,
  stagedOverrides: StagedOverrides,
): number {
  // `allow` is per-column — it never moves siblings.
  if (verb === "allow") return 0;
  const opposite: PolicyVerb = verb === "block" ? "redact" : "block";
  const alt = applyVerb(stagedBlock, stagedOverrides, qualifiedColumn, baseCategories, opposite);
  let count = 0;
  for (const row of perColumn) {
    if (row.qualified_column === qualifiedColumn) continue;
    if (columnIsFloor(row.categories)) continue;
    const before = columnVerb(row.qualified_column, row.categories, stagedBlock, stagedOverrides);
    const after = columnVerb(row.qualified_column, row.categories, alt.block, alt.overrides);
    if (before !== after) count += 1;
  }
  return count;
}

/**
 * The category-wide disclosure note for a verb that mutates the block set.
 * Both `block` and `redact` are category-grained (ADR 0008 §3): blocking a
 * column also refuses every sibling sharing its category, and redacting one
 * also *un-blocks* those siblings. Returns the note text when the action
 * reaches `siblings` other columns, else null (`allow` is per-column, so it
 * never discloses). The loosening (`redact`) direction is the more
 * dangerous one and must be disclosed too.
 */
export function categoryWideNote(verb: PolicyVerb, siblings: number): string | null {
  if (siblings <= 0) return null;
  const cols = `${siblings} other ${siblings === 1 ? "column" : "columns"}`;
  if (verb === "block") return `category-wide: also blocks ${cols}`;
  if (verb === "redact") return `category-wide: also un-blocks ${cols}`;
  return null;
}

/** Encode one override for the preview route's `override` query param:
 * `<qualified_column>|<sensitivity>|<comma-joined sorted categories>`. */
export function serializeOverride(qualifiedColumn: string, override: StagedOverride): string {
  const categories = [...override.categories].sort().join(",");
  return `${qualifiedColumn}|${override.sensitivity}|${categories}`;
}

/**
 * Build the `{block, override}` query payload for api.piiPolicyPreview.
 * Both arrays sorted so identical staged state yields an identical request
 * (TanStack Query key stability + cache hits).
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

function serializeOne(map: StagedOverrides, key: string): string | null {
  const value = map.get(key);
  return value === undefined ? null : serializeOverride(key, value);
}

/** True when one column's staged override differs from its baseline. */
export function isColumnOverrideChanged(
  initial: StagedOverrides,
  staged: StagedOverrides,
  qualifiedColumn: string,
): boolean {
  return serializeOne(initial, qualifiedColumn) !== serializeOne(staged, qualifiedColumn);
}

function countOverrideChanges(initial: StagedOverrides, staged: StagedOverrides): number {
  const keys = new Set<string>([...initial.keys(), ...staged.keys()]);
  let changed = 0;
  for (const key of keys) {
    if (serializeOne(initial, key) !== serializeOne(staged, key)) changed += 1;
  }
  return changed;
}

/** Number of staged changes (block-set deltas + override edits) vs the
 * baseline — drives the "N staged" count and gates Apply/Discard. */
export function countStagedChanges(
  initialBlock: ReadonlySet<PIICategory>,
  stagedBlock: ReadonlySet<PIICategory>,
  initialOverrides: StagedOverrides,
  stagedOverrides: StagedOverrides,
): number {
  const categories = new Set<PIICategory>([...initialBlock, ...stagedBlock]);
  let blockChanges = 0;
  for (const category of categories) {
    if (initialBlock.has(category) !== stagedBlock.has(category)) blockChanges += 1;
  }
  return blockChanges + countOverrideChanges(initialOverrides, stagedOverrides);
}

/* ───────── YAML syntax highlighter (handoff po-yaml coloring) ───────── */

export type YamlTokenKind = "comment" | "key" | "value" | "alarm" | "punct" | "plain";

export interface YamlToken {
  text: string;
  kind: YamlTokenKind;
}

const CATASTROPHIC_SET: ReadonlySet<string> = new Set(CATASTROPHIC_LEAK_CATEGORIES);

/**
 * Tokenise one line of the canonical policy YAML for display coloring,
 * matching the handoff's po-yaml treatment (keys cyan, values green,
 * comments muted) with one honest addition: catastrophic-floor category
 * values render in the alarm color. Pure + deterministic; the renderer
 * maps each token kind to a CSS class.
 */
export function highlightYamlLine(line: string): YamlToken[] {
  if (line.trim() === "") return [{ text: line || " ", kind: "plain" }];
  if (line.trimStart().startsWith("#")) return [{ text: line, kind: "comment" }];

  const listMatch = line.match(/^(\s*)(- )(.*)$/);
  if (listMatch) {
    const value = listMatch[3];
    return [
      { text: listMatch[1], kind: "plain" },
      { text: listMatch[2], kind: "punct" },
      { text: value, kind: CATASTROPHIC_SET.has(value.trim()) ? "alarm" : "value" },
    ];
  }

  const kvMatch = line.match(/^(\s*)([^:]+)(:)(.*)$/);
  if (kvMatch) {
    const tokens: YamlToken[] = [
      { text: kvMatch[1], kind: "plain" },
      { text: kvMatch[2], kind: "key" },
      { text: kvMatch[3], kind: "punct" },
    ];
    if (kvMatch[4]) {
      tokens.push({ text: kvMatch[4], kind: "value" });
    }
    return tokens;
  }

  return [{ text: line, kind: "plain" }];
}

/** Highlight a full YAML body into per-line token arrays. */
export function highlightYaml(yaml: string): YamlToken[][] {
  return yaml.split("\n").map(highlightYamlLine);
}
