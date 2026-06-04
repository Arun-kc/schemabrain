import { describe, expect, it } from "vitest";
import type { PIICategory, PolicyColumnEntry } from "@/lib/types";
import {
  applyVerb,
  buildPreviewQuery,
  categoryWideNote,
  columnIsFloor,
  columnVerb,
  countStagedChanges,
  EMPTY_POLICY_FILTER,
  filterPerColumn,
  groupByTable,
  highlightYaml,
  highlightYamlLine,
  initialOverridesFromPerColumn,
  isColumnOverrideChanged,
  isEmptyPolicyFilter,
  countVerbs,
  MARK_SAFE_SENSITIVITY,
  matchesPolicyFilter,
  type PolicyFilter,
  prettyCategory,
  serializeOverride,
  siblingsAffectedByVerb,
  type StagedOverride,
  type StagedOverrides,
  summarizePolicyCategories,
  toggleCategoryBlock,
} from "@/lib/policy";

function entry(over: Partial<PolicyColumnEntry>): PolicyColumnEntry {
  return {
    qualified_table: "public.users",
    column_name: "email",
    qualified_column: "public.users.email",
    sensitivity: "pii",
    categories: ["contact"],
    origin: "heuristic",
    effective_enforcement: "allowed",
    ...over,
  };
}

describe("columnIsFloor", () => {
  it("is true when any category is catastrophic", () => {
    expect(columnIsFloor(["credential"])).toBe(true);
    expect(columnIsFloor(["contact", "payment_card"])).toBe(true);
  });
  it("is false for non-floor categories and empty", () => {
    expect(columnIsFloor(["contact"])).toBe(false);
    expect(columnIsFloor([])).toBe(false);
  });
});

describe("columnVerb", () => {
  const QC = "public.users.email";
  it("returns floor for catastrophic columns regardless of staging", () => {
    expect(columnVerb("s.t.pw", ["credential"], new Set(), new Map())).toBe("floor");
  });
  it("returns block when an effective category is in the block set", () => {
    expect(columnVerb(QC, ["contact"], new Set<PIICategory>(["contact"]), new Map())).toBe("block");
  });
  it("returns allow when a categories:[] override is staged", () => {
    const overrides = new Map<string, StagedOverride>([
      [QC, { sensitivity: "internal", categories: [] }],
    ]);
    // even if base category is contact, the empty override wins
    expect(columnVerb(QC, ["contact"], new Set<PIICategory>(["contact"]), overrides)).toBe("allow");
  });
  it("returns redact when classified but not blocked", () => {
    expect(columnVerb(QC, ["contact"], new Set(), new Map())).toBe("redact");
  });
  it("derives against the override's categories for a non-empty reclassification", () => {
    // operator reclassified a contact column to financial; block set has
    // contact (the BASE) but not financial (the EFFECTIVE) → redact, not block.
    const overrides = new Map<string, StagedOverride>([
      [QC, { sensitivity: "pii", categories: ["financial"] }],
    ]);
    expect(columnVerb(QC, ["contact"], new Set<PIICategory>(["contact"]), overrides)).toBe(
      "redact",
    );
    // and block when the EFFECTIVE category is in the block set
    expect(columnVerb(QC, ["contact"], new Set<PIICategory>(["financial"]), overrides)).toBe(
      "block",
    );
  });
  it("keeps a floor column locked even with a staged block-clear + empty override (projection can't unlock the floor)", () => {
    const overrides = new Map<string, StagedOverride>([
      ["s.t.pw", { sensitivity: "internal", categories: [] }],
    ]);
    expect(columnVerb("s.t.pw", ["credential"], new Set(), overrides)).toBe("floor");
  });
});

describe("categoryWideNote", () => {
  it("discloses both block and redact when siblings are affected", () => {
    expect(categoryWideNote("block", 2)).toMatch(/also blocks 2 other columns/);
    expect(categoryWideNote("redact", 1)).toMatch(/also un-blocks 1 other column/);
  });
  it("returns null for allow and for zero siblings", () => {
    expect(categoryWideNote("allow", 5)).toBeNull();
    expect(categoryWideNote("block", 0)).toBeNull();
    expect(categoryWideNote("redact", 0)).toBeNull();
  });
});

describe("applyVerb", () => {
  const QC = "public.users.email";
  it("block adds the column's categories to the block set + drops override", () => {
    const overrides = new Map<string, StagedOverride>([
      [QC, { sensitivity: "internal", categories: [] }],
    ]);
    const next = applyVerb(new Set(), overrides, QC, ["contact"], "block");
    expect(next.block.has("contact")).toBe(true);
    expect(next.overrides.has(QC)).toBe(false);
  });
  it("redact removes the column's categories from the block set + drops override", () => {
    const next = applyVerb(new Set<PIICategory>(["contact"]), new Map(), QC, ["contact"], "redact");
    expect(next.block.has("contact")).toBe(false);
  });
  it("allow writes a {internal, []} override and leaves the block set untouched", () => {
    const block = new Set<PIICategory>(["contact"]);
    const next = applyVerb(block, new Map(), QC, ["contact"], "allow");
    expect(next.overrides.get(QC)).toEqual({ sensitivity: MARK_SAFE_SENSITIVITY, categories: [] });
    expect(next.block.has("contact")).toBe(true); // sibling contact columns still blocked
  });
  it("is immutable — inputs are not mutated", () => {
    const block = new Set<PIICategory>();
    const overrides = new Map<string, StagedOverride>();
    applyVerb(block, overrides, QC, ["contact"], "block");
    expect(block.size).toBe(0);
    expect(overrides.size).toBe(0);
  });
  it("round-trips: redact then block then redact returns to unblocked", () => {
    let state = { block: new Set<PIICategory>(), overrides: new Map<string, StagedOverride>() };
    state = applyVerb(state.block, state.overrides, QC, ["contact"], "block");
    expect(columnVerb(QC, ["contact"], state.block, state.overrides)).toBe("block");
    state = applyVerb(state.block, state.overrides, QC, ["contact"], "redact");
    expect(columnVerb(QC, ["contact"], state.block, state.overrides)).toBe("redact");
  });
  it("multi-category: block adds all, redact removes all", () => {
    const cats: PIICategory[] = ["contact", "financial"];
    const blocked = applyVerb(new Set(), new Map(), QC, cats, "block");
    expect(blocked.block.has("contact")).toBe(true);
    expect(blocked.block.has("financial")).toBe(true);
    const cleared = applyVerb(blocked.block, blocked.overrides, QC, cats, "redact");
    expect(cleared.block.has("contact")).toBe(false);
    expect(cleared.block.has("financial")).toBe(false);
  });
  it("full state machine block→allow→block tracks the derived verb each step", () => {
    let s = { block: new Set<PIICategory>(), overrides: new Map<string, StagedOverride>() };
    s = applyVerb(s.block, s.overrides, QC, ["contact"], "block");
    expect(columnVerb(QC, ["contact"], s.block, s.overrides)).toBe("block");
    s = applyVerb(s.block, s.overrides, QC, ["contact"], "allow");
    expect(columnVerb(QC, ["contact"], s.block, s.overrides)).toBe("allow");
    s = applyVerb(s.block, s.overrides, QC, ["contact"], "block");
    expect(columnVerb(QC, ["contact"], s.block, s.overrides)).toBe("block");
  });
  it("category-wide: blocking column A flips sibling column B's derived verb", () => {
    const a = "s.t.a";
    const b = "s.t.b";
    const blocked = applyVerb(new Set(), new Map(), a, ["contact"], "block");
    // B shares 'contact' and was redact → now derives block through the shared category
    expect(columnVerb(b, ["contact"], blocked.block, blocked.overrides)).toBe("block");
  });
});

describe("siblingsAffectedByVerb", () => {
  const rows = [
    entry({ qualified_column: "s.t.a", categories: ["contact"] }),
    entry({ qualified_column: "s.t.b", categories: ["contact"] }),
    entry({ qualified_column: "s.t.c", categories: ["financial"] }),
    entry({ qualified_column: "s.t.pw", categories: ["credential"] }), // floor, excluded
  ];

  it("counts a sibling that genuinely flips when blocking", () => {
    // A is block (contact in block); toggling A to redact would un-block B.
    const block = new Set<PIICategory>(["contact"]);
    expect(siblingsAffectedByVerb(rows, "s.t.a", ["contact"], "block", block, new Map())).toBe(1);
  });

  it("returns 0 for allow", () => {
    const block = new Set<PIICategory>(["contact"]);
    expect(siblingsAffectedByVerb(rows, "s.t.a", ["contact"], "allow", block, new Map())).toBe(0);
  });

  it("counts a sibling that would flip when the verb is redact (opposite = block)", () => {
    // A is redact (contact not blocked); toggling A to block would block B.
    expect(siblingsAffectedByVerb(rows, "s.t.a", ["contact"], "redact", new Set(), new Map())).toBe(
      1,
    );
  });

  it("does NOT count a sibling that is on an allow override (immune)", () => {
    // B reclassified to allow (empty override) → blocking A doesn't move B.
    const block = new Set<PIICategory>(["contact"]);
    const overrides = new Map<string, StagedOverride>([
      ["s.t.b", { sensitivity: "internal", categories: [] }],
    ]);
    expect(siblingsAffectedByVerb(rows, "s.t.a", ["contact"], "block", block, overrides)).toBe(0);
  });

  it("does NOT count a redact sibling kept blocked by a SECOND category", () => {
    // B has [contact, financial]; both in block. Redacting A (removes
    // contact) leaves B blocked via financial → B does not flip.
    const multi = [
      entry({ qualified_column: "s.t.a", categories: ["contact"] }),
      entry({ qualified_column: "s.t.b", categories: ["contact", "financial"] }),
    ];
    const block = new Set<PIICategory>(["contact", "financial"]);
    // A is block here; toggling to redact removes only contact.
    expect(siblingsAffectedByVerb(multi, "s.t.a", ["contact"], "block", block, new Map())).toBe(0);
  });
});

describe("serializeOverride", () => {
  it("encodes column|sensitivity|comma-joined-sorted-categories", () => {
    const over: StagedOverride = { sensitivity: "pii", categories: ["location", "contact"] };
    expect(serializeOverride("s.t.c", over)).toBe("s.t.c|pii|contact,location");
  });
  it("emits an empty trailing field for an empty-categories override", () => {
    expect(serializeOverride("s.t.c", { sensitivity: "internal", categories: [] })).toBe(
      "s.t.c|internal|",
    );
  });
});

describe("buildPreviewQuery", () => {
  it("sorts block + override for stable query keys", () => {
    const block = new Set<PIICategory>(["payment_card", "contact"]);
    const overrides = new Map<string, StagedOverride>([
      ["z.z.z", { sensitivity: "internal", categories: [] }],
      ["a.a.a", { sensitivity: "internal", categories: [] }],
    ]);
    const { block: b, override: o } = buildPreviewQuery(block, overrides);
    expect(b).toEqual(["contact", "payment_card"]);
    expect(o).toEqual(["a.a.a|internal|", "z.z.z|internal|"]);
  });
});

describe("initialOverridesFromPerColumn", () => {
  it("keeps only operator-origin rows", () => {
    const rows = [
      entry({ qualified_column: "s.t.a", origin: "operator", sensitivity: "internal", categories: [] }),
      entry({ qualified_column: "s.t.b", origin: "heuristic", categories: ["contact"] }),
    ];
    const map = initialOverridesFromPerColumn(rows);
    expect([...map.keys()]).toEqual(["s.t.a"]);
    expect(map.get("s.t.a")).toEqual({ sensitivity: "internal", categories: [] });
  });
  it("preserves a non-empty operator reclassification (drives on-first-paint verb)", () => {
    const rows = [
      entry({ qualified_column: "s.t.a", origin: "operator", sensitivity: "pii", categories: ["financial"] }),
    ];
    const map = initialOverridesFromPerColumn(rows);
    expect(map.get("s.t.a")).toEqual({ sensitivity: "pii", categories: ["financial"] });
    // and it derives correctly on first paint against a block set
    expect(columnVerb("s.t.a", ["contact"], new Set<PIICategory>(["financial"]), map)).toBe("block");
  });
});

describe("isColumnOverrideChanged", () => {
  it("detects add, remove, and value changes", () => {
    const initial = new Map<string, StagedOverride>([
      ["s.t.a", { sensitivity: "pii", categories: ["contact"] }],
    ]);
    const staged = new Map<string, StagedOverride>([
      ["s.t.a", { sensitivity: "internal", categories: [] }],
      ["s.t.b", { sensitivity: "internal", categories: [] }],
    ]);
    expect(isColumnOverrideChanged(initial, staged, "s.t.a")).toBe(true);
    expect(isColumnOverrideChanged(initial, staged, "s.t.b")).toBe(true);
    expect(isColumnOverrideChanged(initial, initial, "s.t.a")).toBe(false);
    expect(isColumnOverrideChanged(initial, staged, "s.t.unknown")).toBe(false);
  });
});

describe("countStagedChanges", () => {
  it("counts block-set deltas plus override edits", () => {
    const initialBlock = new Set<PIICategory>(["credential"]);
    const stagedBlock = new Set<PIICategory>(["credential", "contact"]); // +1
    const initialOverrides = new Map<string, StagedOverride>();
    const stagedOverrides = new Map<string, StagedOverride>([
      ["s.t.c", { sensitivity: "internal", categories: [] }], // +1
    ]);
    expect(countStagedChanges(initialBlock, stagedBlock, initialOverrides, stagedOverrides)).toBe(2);
  });
  it("is zero when nothing changed", () => {
    const block = new Set<PIICategory>(["credential", "payment_card", "government_id"]);
    const overrides = new Map<string, StagedOverride>();
    expect(countStagedChanges(block, new Set(block), overrides, new Map(overrides))).toBe(0);
  });
  it("counts a block-set swap as two changes (one out, one in)", () => {
    const initialBlock = new Set<PIICategory>(["contact"]);
    const stagedBlock = new Set<PIICategory>(["financial"]);
    const overrides = new Map<string, StagedOverride>();
    expect(countStagedChanges(initialBlock, stagedBlock, overrides, new Map(overrides))).toBe(2);
  });
  it("counts a same-key override value change as one (not two, not zero)", () => {
    const block = new Set<PIICategory>();
    const initialOverrides = new Map<string, StagedOverride>([
      ["s.t.c", { sensitivity: "pii", categories: ["contact"] }],
    ]);
    const stagedOverrides = new Map<string, StagedOverride>([
      ["s.t.c", { sensitivity: "internal", categories: [] }],
    ]);
    expect(countStagedChanges(block, new Set(block), initialOverrides, stagedOverrides)).toBe(1);
  });
});

describe("highlightYamlLine", () => {
  it("colors a key/value line", () => {
    const tokens = highlightYamlLine("version: 1");
    expect(tokens.find((t) => t.kind === "key")?.text).toBe("version");
    expect(tokens.find((t) => t.kind === "value")?.text.trim()).toBe("1");
  });
  it("marks a comment line", () => {
    expect(highlightYamlLine("# compiled policy")[0].kind).toBe("comment");
  });
  it("colors a list value, with catastrophic categories in alarm", () => {
    expect(highlightYamlLine("- contact").some((t) => t.kind === "value")).toBe(true);
    expect(highlightYamlLine("- credential").some((t) => t.kind === "alarm")).toBe(true);
  });
  it("blank line stays plain", () => {
    expect(highlightYamlLine("")[0].kind).toBe("plain");
  });
  it("an unrecognised non-empty line falls back to plain", () => {
    const tokens = highlightYamlLine("barewordnocolon");
    expect(tokens).toEqual([{ text: "barewordnocolon", kind: "plain" }]);
  });
  it("does NOT alarm a catastrophic token in a key position (alarm is list-value-only)", () => {
    // a key like `credential:` must not be alarm-colored
    const tokens = highlightYamlLine("credential: x");
    expect(tokens.some((t) => t.kind === "alarm")).toBe(false);
    expect(tokens.find((t) => t.kind === "key")?.text).toBe("credential");
  });
  it("alarms a catastrophic list value even with trailing whitespace (trim match)", () => {
    expect(highlightYamlLine("  - credential ").some((t) => t.kind === "alarm")).toBe(true);
  });
});

describe("highlightYaml", () => {
  it("tokenises every line", () => {
    const lines = highlightYaml("version: 1\nblock:\n- contact");
    expect(lines).toHaveLength(3);
    expect(lines[2].some((t) => t.kind === "value" && t.text === "contact")).toBe(true);
  });
});

/* ───────── scaling: grouping · filtering · category summary ───────── */

describe("prettyCategory", () => {
  it("turns underscores into spaces", () => {
    expect(prettyCategory("online_identifier")).toBe("online identifier");
    expect(prettyCategory("demographic_protected")).toBe("demographic protected");
  });
  it("leaves single-word categories unchanged", () => {
    expect(prettyCategory("contact")).toBe("contact");
  });
});

describe("groupByTable", () => {
  it("returns an empty array for empty input", () => {
    expect(groupByTable([])).toEqual([]);
  });
  it("groups rows by qualified_table preserving first-seen + intra-table order", () => {
    const rows = [
      entry({ qualified_table: "public.users", column_name: "email", qualified_column: "public.users.email" }),
      entry({ qualified_table: "public.users", column_name: "phone", qualified_column: "public.users.phone" }),
      entry({ qualified_table: "public.orders", column_name: "card", qualified_column: "public.orders.card" }),
    ];
    const groups = groupByTable(rows);
    expect(groups.map((g) => g.qualifiedTable)).toEqual(["public.users", "public.orders"]);
    expect(groups[0].columns.map((c) => c.column_name)).toEqual(["email", "phone"]);
    expect(groups[1].columns.map((c) => c.column_name)).toEqual(["card"]);
  });
  it("keeps same column-name across tables in distinct groups", () => {
    const rows = [
      entry({ qualified_table: "a.t", column_name: "id", qualified_column: "a.t.id" }),
      entry({ qualified_table: "b.t", column_name: "id", qualified_column: "b.t.id" }),
    ];
    expect(groupByTable(rows)).toHaveLength(2);
  });
});

describe("matchesPolicyFilter / filterPerColumn", () => {
  const rows = [
    entry({
      qualified_table: "public.users",
      column_name: "email",
      qualified_column: "public.users.email",
      categories: ["contact"],
    }),
    entry({
      qualified_table: "public.users",
      column_name: "ssn",
      qualified_column: "public.users.ssn",
      categories: ["government_id"],
    }),
    entry({
      qualified_table: "public.orders",
      column_name: "ip_addr",
      qualified_column: "public.orders.ip_addr",
      categories: ["online_identifier"],
    }),
  ];
  const noStaged = { block: new Set<PIICategory>(), overrides: new Map() as StagedOverrides };

  it("empty filter passes everything (and short-circuits)", () => {
    expect(isEmptyPolicyFilter(EMPTY_POLICY_FILTER)).toBe(true);
    expect(filterPerColumn(rows, EMPTY_POLICY_FILTER, noStaged.block, noStaged.overrides)).toBe(rows);
  });
  it("query matches table, column, qualified column, and prettified category (case-insensitive)", () => {
    const byTable: PolicyFilter = { query: "ORDERS", category: null, status: null };
    expect(
      filterPerColumn(rows, byTable, noStaged.block, noStaged.overrides).map((r) => r.column_name),
    ).toEqual(["ip_addr"]);

    const byCol: PolicyFilter = { query: "email", category: null, status: null };
    expect(filterPerColumn(rows, byCol, noStaged.block, noStaged.overrides)).toHaveLength(1);

    const byQualified: PolicyFilter = { query: "users.ssn", category: null, status: null };
    expect(filterPerColumn(rows, byQualified, noStaged.block, noStaged.overrides)).toHaveLength(1);

    const byPrettyCat: PolicyFilter = { query: "online identifier", category: null, status: null };
    expect(
      filterPerColumn(rows, byPrettyCat, noStaged.block, noStaged.overrides).map((r) => r.column_name),
    ).toEqual(["ip_addr"]);
  });
  it("category facet matches base categories", () => {
    const f: PolicyFilter = { query: "", category: "contact", status: null };
    expect(filterPerColumn(rows, f, noStaged.block, noStaged.overrides).map((r) => r.column_name)).toEqual([
      "email",
    ]);
  });
  it("status facet matches the derived verb under staged state", () => {
    // government_id is catastrophic → floor; online_identifier blocked when staged.
    const blocked = new Set<PIICategory>(["online_identifier"]);
    const floorF: PolicyFilter = { query: "", category: null, status: "floor" };
    expect(filterPerColumn(rows, floorF, blocked, noStaged.overrides).map((r) => r.column_name)).toEqual([
      "ssn",
    ]);
    const blockF: PolicyFilter = { query: "", category: null, status: "block" };
    expect(filterPerColumn(rows, blockF, blocked, noStaged.overrides).map((r) => r.column_name)).toEqual([
      "ip_addr",
    ]);
    const redactF: PolicyFilter = { query: "", category: null, status: "redact" };
    expect(filterPerColumn(rows, redactF, blocked, noStaged.overrides).map((r) => r.column_name)).toEqual([
      "email",
    ]);
  });
  it("combines facets with AND", () => {
    const f: PolicyFilter = { query: "users", category: "contact", status: "redact" };
    expect(matchesPolicyFilter(rows[0], f, noStaged.block, noStaged.overrides)).toBe(true);
    expect(matchesPolicyFilter(rows[1], f, noStaged.block, noStaged.overrides)).toBe(false);
  });
});

describe("summarizePolicyCategories", () => {
  const rows = [
    entry({ qualified_column: "a.t.email", categories: ["contact"] }),
    entry({ qualified_column: "a.t.phone", categories: ["contact"] }),
    entry({ qualified_column: "a.t.ssn", categories: ["government_id"] }),
    entry({ qualified_column: "a.t.card", categories: ["payment_card", "financial"] }),
  ];
  const noOverrides = new Map() as StagedOverrides;

  it("emits one entry per present category, ordered by PII_CATEGORIES, omitting absent", () => {
    const summary = summarizePolicyCategories(rows, new Set(), noOverrides);
    expect(summary.map((s) => s.category)).toEqual([
      "contact",
      "financial",
      "payment_card",
      "government_id",
    ]);
  });
  it("breakdown counts sum to total and reflect derived verbs", () => {
    const summary = summarizePolicyCategories(rows, new Set<PIICategory>(["contact"]), noOverrides);
    const contact = summary.find((s) => s.category === "contact")!;
    expect(contact.total).toBe(2);
    expect(contact.blocked).toBe(2);
    expect(contact.blocked + contact.redacted + contact.allowed + contact.floor).toBe(contact.total);
    expect(contact.inBlockSet).toBe(true);
    expect(contact.isCatastrophic).toBe(false);
  });
  it("catastrophic categories are locked (inBlockSet + isCatastrophic) with floor counts", () => {
    const summary = summarizePolicyCategories(rows, new Set(), noOverrides);
    const gov = summary.find((s) => s.category === "government_id")!;
    expect(gov.isCatastrophic).toBe(true);
    expect(gov.inBlockSet).toBe(true);
    expect(gov.floor).toBe(1);
    // payment_card column co-tagged financial: the column is floor under BOTH
    const financial = summary.find((s) => s.category === "financial")!;
    expect(financial.floor).toBe(1);
  });
  it("allow override moves a column out of blocked into allowed", () => {
    const overrides = new Map([["a.t.email", { sensitivity: "internal" as const, categories: [] }]]);
    const summary = summarizePolicyCategories(rows, new Set<PIICategory>(["contact"]), overrides);
    const contact = summary.find((s) => s.category === "contact")!;
    expect(contact.allowed).toBe(1);
    expect(contact.blocked).toBe(1);
  });
});

describe("toggleCategoryBlock", () => {
  it("adds a category to the block set when block=true (immutable)", () => {
    const base = new Set<PIICategory>(["contact"]);
    const next = toggleCategoryBlock(base, "financial", true);
    expect([...next].sort()).toEqual(["contact", "financial"]);
    expect([...base]).toEqual(["contact"]); // input untouched
  });
  it("removes a non-catastrophic category when block=false", () => {
    const next = toggleCategoryBlock(new Set<PIICategory>(["contact", "financial"]), "contact", false);
    expect([...next]).toEqual(["financial"]);
  });
  it("un-blocking a catastrophic category is a no-op", () => {
    const base = new Set<PIICategory>(["payment_card"]);
    const next = toggleCategoryBlock(base, "payment_card", false);
    expect([...next]).toEqual(["payment_card"]);
  });
});

describe("countVerbs", () => {
  it("tallies each column's derived verb once", () => {
    const cols = [
      entry({ qualified_column: "a.t.email", categories: ["contact"] }),
      entry({ qualified_column: "a.t.phone", categories: ["contact"] }),
      entry({ qualified_column: "a.t.ssn", categories: ["government_id"] }),
      entry({
        qualified_column: "a.t.x",
        categories: ["online_identifier"],
        origin: "operator",
        sensitivity: "internal",
      }),
    ];
    const overrides = new Map([["a.t.x", { sensitivity: "internal" as const, categories: [] }]]);
    const counts = countVerbs(cols, new Set<PIICategory>(["contact"]), overrides);
    expect(counts).toEqual({ block: 2, redact: 0, allow: 1, floor: 1 });
  });
  it("is all-zero for empty input", () => {
    expect(countVerbs([], new Set(), new Map())).toEqual({ block: 0, redact: 0, allow: 0, floor: 0 });
  });
});

describe("filter/sibling decoupling invariant (ADR 0008 §3)", () => {
  // A `contact` column with siblings in other tables must report the SAME
  // sibling blast radius whether or not a filter would hide those siblings.
  const rows = [
    entry({ qualified_table: "a.t", column_name: "email", qualified_column: "a.t.email", categories: ["contact"] }),
    entry({ qualified_table: "b.t", column_name: "email", qualified_column: "b.t.email", categories: ["contact"] }),
    entry({ qualified_table: "c.t", column_name: "email", qualified_column: "c.t.email", categories: ["contact"] }),
  ];
  it("sibling count is computed over the FULL set, not the filtered view", () => {
    // contact is staged-blocked → all three columns are currently `block`, so
    // the disclosure on a.t.email reads "also blocks N other columns".
    const block = new Set<PIICategory>(["contact"]);
    const overrides = new Map() as StagedOverrides;

    const full = siblingsAffectedByVerb(rows, "a.t.email", ["contact"], "block", block, overrides);
    expect(full).toBe(2); // b.t.email + c.t.email ride the same block

    // Filter to just table a — the RENDERED set shrinks to 1 row...
    const filtered = filterPerColumn(rows, { query: "a.t", category: null, status: null }, block, overrides);
    expect(filtered).toHaveLength(1);

    // ...but the disclosure math must STILL use the full array and report 2.
    const stillFull = siblingsAffectedByVerb(rows, "a.t.email", ["contact"], "block", block, overrides);
    expect(stillFull).toBe(2);
    // Guard against the bug: feeding the filtered view in would under-count.
    const wrong = siblingsAffectedByVerb(filtered, "a.t.email", ["contact"], "block", block, overrides);
    expect(wrong).toBe(0);
    expect(wrong).not.toBe(stillFull);
  });
});
