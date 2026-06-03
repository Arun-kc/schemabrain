import { describe, expect, it } from "vitest";
import type { PIICategory, PolicyColumnEntry } from "@/lib/types";
import {
  buildPreviewQuery,
  columnIsFloor,
  countStagedChanges,
  initialOverridesFromPerColumn,
  isColumnOverrideChanged,
  isMarkedSafe,
  MARK_SAFE_SENSITIVITY,
  serializeOverride,
  type StagedOverride,
  toggleBlockCategory,
  toggleMarkSafe,
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

describe("serializeOverride", () => {
  it("encodes column|sensitivity|comma-joined-sorted-categories", () => {
    const over: StagedOverride = { sensitivity: "pii", categories: ["location", "contact"] };
    expect(serializeOverride("s.t.c", over)).toBe("s.t.c|pii|contact,location");
  });
  it("emits an empty trailing field for mark-safe (no categories)", () => {
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
});

describe("toggleMarkSafe / isMarkedSafe", () => {
  it("stages a mark-safe override then clears it", () => {
    const start = new Map<string, StagedOverride>();
    const safe = toggleMarkSafe(start, "s.t.c", true);
    expect(isMarkedSafe("s.t.c", safe)).toBe(true);
    expect(safe.get("s.t.c")).toEqual({ sensitivity: MARK_SAFE_SENSITIVITY, categories: [] });
    const cleared = toggleMarkSafe(safe, "s.t.c", false);
    expect(isMarkedSafe("s.t.c", cleared)).toBe(false);
    // immutability — original map untouched
    expect(start.size).toBe(0);
  });
  it("does not treat a non-empty override as marked safe", () => {
    const map = new Map<string, StagedOverride>([
      ["s.t.c", { sensitivity: "pii", categories: ["contact"] }],
    ]);
    expect(isMarkedSafe("s.t.c", map)).toBe(false);
  });
});

describe("toggleBlockCategory", () => {
  it("adds then removes immutably", () => {
    const start = new Set<PIICategory>();
    const added = toggleBlockCategory(start, "contact");
    expect(added.has("contact")).toBe(true);
    expect(start.has("contact")).toBe(false);
    const removed = toggleBlockCategory(added, "contact");
    expect(removed.has("contact")).toBe(false);
  });
});

describe("isColumnOverrideChanged", () => {
  it("detects add, remove, and value changes", () => {
    const initial = new Map<string, StagedOverride>([
      ["s.t.a", { sensitivity: "pii", categories: ["contact"] }],
    ]);
    const staged = new Map<string, StagedOverride>([
      ["s.t.a", { sensitivity: "internal", categories: [] }], // changed
      ["s.t.b", { sensitivity: "internal", categories: [] }], // added
    ]);
    expect(isColumnOverrideChanged(initial, staged, "s.t.a")).toBe(true);
    expect(isColumnOverrideChanged(initial, staged, "s.t.b")).toBe(true);
    expect(isColumnOverrideChanged(initial, initial, "s.t.a")).toBe(false);
    expect(isColumnOverrideChanged(initial, staged, "s.t.unknown")).toBe(false);
  });
});

describe("countStagedChanges", () => {
  it("counts block toggles plus override edits", () => {
    const initialBlock = new Set<PIICategory>(["credential"]);
    const stagedBlock = new Set<PIICategory>(["credential", "contact"]); // +1
    const initialOverrides = new Map<string, StagedOverride>();
    const stagedOverrides = new Map<string, StagedOverride>([
      ["s.t.c", { sensitivity: "internal", categories: [] }], // +1
    ]);
    expect(
      countStagedChanges(initialBlock, stagedBlock, initialOverrides, stagedOverrides),
    ).toBe(2);
  });
  it("is zero when nothing changed", () => {
    const block = new Set<PIICategory>(["credential", "payment_card", "government_id"]);
    const overrides = new Map<string, StagedOverride>();
    expect(countStagedChanges(block, new Set(block), overrides, new Map(overrides))).toBe(0);
  });
});
