import { describe, expect, it } from "vitest";
import type { PIICategory, PolicyColumnEntry } from "@/lib/types";
import {
  applyVerb,
  buildPreviewQuery,
  categoryWideNote,
  columnIsFloor,
  columnVerb,
  countStagedChanges,
  highlightYaml,
  highlightYamlLine,
  initialOverridesFromPerColumn,
  isColumnOverrideChanged,
  MARK_SAFE_SENSITIVITY,
  serializeOverride,
  siblingsAffectedByVerb,
  type StagedOverride,
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
