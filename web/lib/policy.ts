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
  PII_CATEGORIES,
  type PIICategory,
  type PolicyColumnEntry,
  type Sensitivity,
} from "@/lib/types";

/** Display form of a category id — underscores to spaces (`online_identifier`
 * → `online identifier`). Shared by the grid, the search haystack, and the
 * category strip so the prettified text is consistent everywhere. */
export function prettyCategory(category: string): string {
  return category.replace(/_/g, " ");
}

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

/* ───────── scaling: grouping · filtering · category summary ─────────
 *
 * These derive RENDER views over the per-column listing so the page holds
 * up on a real multi-table schema. They are deliberately decoupled from the
 * disclosure math: `siblingsAffectedByVerb` / `categoryWideNote` must always
 * be computed over the FULL `per_column` set, never a grouped/filtered subset
 * (ADR 0008 §3 — a search filter must not change how many siblings a verb
 * reports as affected). Callers pass the full array to the sibling math and
 * the filtered array only to the renderer.
 */

/** One table's columns, in the server's `(qualified_table, column_name)`
 * order. `groupByTable` partitions the (pre-sorted) per-column listing. */
export interface PolicyTableGroup {
  qualifiedTable: string;
  columns: readonly PolicyColumnEntry[];
}

/**
 * Partition a per-column listing into per-table groups, PRESERVING order:
 * groups appear in first-seen table order and columns keep their incoming
 * order. The API is already sorted by `(qualified_table, column_name)`
 * (store.py), so a single insertion-ordered pass is order-correct without
 * re-sorting. Pure; empty input → empty array.
 */
export function groupByTable(
  perColumn: readonly PolicyColumnEntry[],
): readonly PolicyTableGroup[] {
  const groups = new Map<string, PolicyColumnEntry[]>();
  for (const row of perColumn) {
    const existing = groups.get(row.qualified_table);
    if (existing) existing.push(row);
    else groups.set(row.qualified_table, [row]);
  }
  return [...groups.entries()].map(([qualifiedTable, columns]) => ({
    qualifiedTable,
    columns,
  }));
}

/** The enforcement-status facet — mirrors the derived verb, plus `floor`
 * (the locked catastrophic state `columnVerb` returns). */
export type PolicyStatusFilter = "block" | "redact" | "allow" | "floor";

export interface PolicyFilter {
  /** Free text — matched case-insensitively against table, column, qualified
   * column, and prettified categories. */
  query: string;
  /** Restrict to columns whose BASE categories include this one (null = any). */
  category: PIICategory | null;
  /** Restrict to columns whose DERIVED verb equals this (null = any). */
  status: PolicyStatusFilter | null;
}

export const EMPTY_POLICY_FILTER: PolicyFilter = {
  query: "",
  category: null,
  status: null,
};

/** True when no facet of the filter is active (renderers skip filtering). */
export function isEmptyPolicyFilter(filter: PolicyFilter): boolean {
  return filter.query.trim() === "" && filter.category === null && filter.status === null;
}

/**
 * Whether one column passes the active filter under the STAGED state.
 * `query` is a substring match across table / column / qualified column /
 * prettified categories. `category` matches the column's BASE categories
 * (the operator filters by what was classified). `status` matches the
 * column's DERIVED verb (via `columnVerb`), so it stays consistent with the
 * grid being edited.
 */
export function matchesPolicyFilter(
  row: PolicyColumnEntry,
  filter: PolicyFilter,
  stagedBlock: ReadonlySet<PIICategory>,
  stagedOverrides: StagedOverrides,
): boolean {
  const query = filter.query.trim().toLowerCase();
  if (query) {
    const haystack = [
      row.qualified_table,
      row.column_name,
      row.qualified_column,
      ...row.categories.map(prettyCategory),
    ]
      .join(" ")
      .toLowerCase();
    if (!haystack.includes(query)) return false;
  }
  if (filter.category && !row.categories.includes(filter.category)) return false;
  if (filter.status) {
    const verb = columnVerb(row.qualified_column, row.categories, stagedBlock, stagedOverrides);
    if (verb !== filter.status) return false;
  }
  return true;
}

/** Filter a per-column listing (order-preserving). Composes
 * `matchesPolicyFilter`; pure. Used for RENDERING only — never feed the
 * result into `siblingsAffectedByVerb`. */
export function filterPerColumn(
  perColumn: readonly PolicyColumnEntry[],
  filter: PolicyFilter,
  stagedBlock: ReadonlySet<PIICategory>,
  stagedOverrides: StagedOverrides,
): readonly PolicyColumnEntry[] {
  if (isEmptyPolicyFilter(filter)) return perColumn;
  return perColumn.filter((row) => matchesPolicyFilter(row, filter, stagedBlock, stagedOverrides));
}

/** Per-category overview for the summary strip — one entry per category that
 * appears on any column, ordered by `PII_CATEGORIES`. Counts are by DERIVED
 * verb under the staged state; `inBlockSet` drives the one-click toggle and
 * `isCatastrophic` renders the cell as a locked floor. */
export interface PolicyCategorySummary {
  category: PIICategory;
  total: number;
  blocked: number;
  redacted: number;
  allowed: number;
  floor: number;
  /** Staged block-set membership (toggle state). Always true for the floor. */
  inBlockSet: boolean;
  /** A catastrophic-floor category — locked, can't be toggled off. */
  isCatastrophic: boolean;
}

interface CategoryTally {
  total: number;
  blocked: number;
  redacted: number;
  allowed: number;
  floor: number;
}

/**
 * Per-category breakdown computed over the FULL per-column listing under the
 * staged state. A column carrying N categories contributes to N tallies; its
 * derived verb (the whole column's verb, via `columnVerb`) is what's counted,
 * so a column blocked by one category but carrying a second shows as `blocked`
 * under both. Categories with zero columns are omitted. Pure.
 */
export function summarizePolicyCategories(
  perColumn: readonly PolicyColumnEntry[],
  stagedBlock: ReadonlySet<PIICategory>,
  stagedOverrides: StagedOverrides,
): readonly PolicyCategorySummary[] {
  const tallies = new Map<PIICategory, CategoryTally>();
  for (const row of perColumn) {
    const verb = columnVerb(row.qualified_column, row.categories, stagedBlock, stagedOverrides);
    for (const category of row.categories) {
      let tally = tallies.get(category);
      if (!tally) {
        tally = { total: 0, blocked: 0, redacted: 0, allowed: 0, floor: 0 };
        tallies.set(category, tally);
      }
      tally.total += 1;
      if (verb === "block") tally.blocked += 1;
      else if (verb === "redact") tally.redacted += 1;
      else if (verb === "allow") tally.allowed += 1;
      else tally.floor += 1;
    }
  }
  const out: PolicyCategorySummary[] = [];
  for (const category of PII_CATEGORIES) {
    const tally = tallies.get(category);
    if (!tally) continue;
    const catastrophic = isCatastrophic(category);
    out.push({
      category,
      total: tally.total,
      blocked: tally.blocked,
      redacted: tally.redacted,
      allowed: tally.allowed,
      floor: tally.floor,
      inBlockSet: catastrophic ? true : stagedBlock.has(category),
      isCatastrophic: catastrophic,
    });
  }
  return out;
}

/** Verb tally across a set of columns (each column counted once by its
 * derived verb). Drives the per-table mini-summary in a collapsed group
 * header ("3 blocked · 1 floor"). */
export interface VerbCounts {
  block: number;
  redact: number;
  allow: number;
  floor: number;
}

/** Count each column's derived verb across `columns` under the staged state.
 * Pure. Used for the collapsed table-group header summary. */
export function countVerbs(
  columns: readonly PolicyColumnEntry[],
  stagedBlock: ReadonlySet<PIICategory>,
  stagedOverrides: StagedOverrides,
): VerbCounts {
  const counts: VerbCounts = { block: 0, redact: 0, allow: 0, floor: 0 };
  for (const row of columns) {
    const verb = columnVerb(row.qualified_column, row.categories, stagedBlock, stagedOverrides);
    counts[verb] += 1;
  }
  return counts;
}

/**
 * Block (or un-block) an entire category in one action — the strip toggle.
 * Adds/removes the category from the block set ONLY; column overrides are
 * untouched, so `allow`-overridden columns stay immune (consistent with the
 * engine grain + `siblingsAffectedByVerb`). Un-blocking a catastrophic
 * category is a no-op (the floor is non-negotiable). Immutable.
 */
export function toggleCategoryBlock(
  stagedBlock: ReadonlySet<PIICategory>,
  category: PIICategory,
  block: boolean,
): Set<PIICategory> {
  const next = new Set(stagedBlock);
  if (block) {
    next.add(category);
  } else if (!isCatastrophic(category)) {
    next.delete(category);
  }
  return next;
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
