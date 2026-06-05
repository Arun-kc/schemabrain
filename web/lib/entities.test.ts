import { describe, expect, it } from "vitest";
import type { EntityColumn, EntityListItem } from "@/lib/types";
import {
  cardinalityLabel,
  categoryLabel,
  columnIsCatastrophic,
  topSimilarColumns,
  compareEntities,
  confidenceDisplay,
  confidenceRank,
  DEFAULT_ENTITY_SORT,
  formatRowCount,
  groupChipKind,
  groupDotVar,
  groupLabel,
  nextSort,
  piiLevelChip,
  piiLevelRank,
  sortEntities,
} from "./entities";

function item(over: Partial<EntityListItem>): EntityListItem {
  return {
    name: "user",
    description: "",
    qualified_table: "public.users",
    identity: "id",
    group: "identity",
    origin: "suggested",
    inference_method: "llm_suggested",
    validation_state: "applied",
    rows: 1000,
    confidence: "high",
    pii_level: "none",
    metrics_count: 0,
    joins_count: 0,
    ...over,
  };
}

describe("groupLabel / groupChipKind", () => {
  it("labels every group in the closed enum", () => {
    expect(groupLabel("identity")).toBe("Identity");
    expect(groupLabel("billing")).toBe("Billing & revenue");
    expect(groupLabel("activity")).toBe("Activity & logs");
    expect(groupLabel("other")).toBe("Other");
  });

  it("maps identity→green, billing→cyan, everything else neutral", () => {
    expect(groupChipKind("identity")).toBe("green");
    expect(groupChipKind("billing")).toBe("cyan");
    expect(groupChipKind("activity")).toBe("neutral");
    expect(groupChipKind("other")).toBe("neutral");
  });
});

describe("groupDotVar", () => {
  it("colours by group when not catastrophic", () => {
    expect(groupDotVar("identity", "none")).toBe("var(--green)");
    expect(groupDotVar("billing", "pii")).toBe("var(--cyan)");
    expect(groupDotVar("activity", "internal")).toBe("var(--violet)");
    expect(groupDotVar("other", "confidential")).toBe("var(--ink-3)");
  });

  it("overrides to the alarm rail for the catastrophic floor regardless of group", () => {
    expect(groupDotVar("identity", "catastrophic")).toBe("var(--alarm)");
    expect(groupDotVar("activity", "catastrophic")).toBe("var(--alarm)");
  });
});

describe("formatRowCount", () => {
  it("renders '—' for an unknown (null) count, never a fabricated 0", () => {
    expect(formatRowCount(null)).toBe("—");
  });

  it("renders a genuine 0-row table as '0'", () => {
    expect(formatRowCount(0)).toBe("0");
  });

  it("formats millions and thousands compactly, trimming trailing .0", () => {
    expect(formatRowCount(1_200_000)).toBe("1.2M");
    expect(formatRowCount(94_000_000)).toBe("94M");
    expect(formatRowCount(1_000_000)).toBe("1M");
    expect(formatRowCount(12_500)).toBe("12.5k");
    expect(formatRowCount(1_000)).toBe("1k");
    expect(formatRowCount(999)).toBe("999");
  });

  it("promotes the 1k→1M rounding boundary instead of emitting '1000k'", () => {
    expect(formatRowCount(999_999)).toBe("1M");
    expect(formatRowCount(999_950)).toBe("1M");
    expect(formatRowCount(999_949)).toBe("999.9k");
  });
});

describe("confidenceDisplay / confidenceRank", () => {
  it("maps each band to a label + chip kind, never a percentage", () => {
    expect(confidenceDisplay("high")).toEqual({ label: "High", kind: "green" });
    expect(confidenceDisplay("medium")).toEqual({ label: "Medium", kind: "cyan" });
    expect(confidenceDisplay("low")).toEqual({ label: "Low", kind: "neutral" });
  });

  it("returns null for a hand-authored entity so the caller omits the pill", () => {
    expect(confidenceDisplay(null)).toBeNull();
  });

  it("ranks high > medium > low > null", () => {
    expect(confidenceRank("high")).toBe(3);
    expect(confidenceRank("medium")).toBe(2);
    expect(confidenceRank("low")).toBe(1);
    expect(confidenceRank(null)).toBe(0);
  });
});

describe("piiLevelChip / piiLevelRank", () => {
  it("reserves the alarm chip for the catastrophic floor", () => {
    expect(piiLevelChip("catastrophic")).toEqual({ label: "Catastrophic", kind: "auth" });
    expect(piiLevelChip("pii")).toEqual({ label: "PII", kind: "contact" });
    expect(piiLevelChip("confidential").kind).toBe("neutral");
    expect(piiLevelChip("internal").kind).toBe("neutral");
    expect(piiLevelChip("none")).toEqual({ label: "None", kind: "none" });
  });

  it("ranks catastrophic highest and none lowest", () => {
    expect(piiLevelRank("catastrophic")).toBe(4);
    expect(piiLevelRank("pii")).toBe(3);
    expect(piiLevelRank("confidential")).toBe(2);
    expect(piiLevelRank("internal")).toBe(1);
    expect(piiLevelRank("none")).toBe(0);
  });
});

describe("compareEntities / sortEntities", () => {
  it("sorts rows descending by default (largest first), null counts last", () => {
    const items = [
      item({ name: "a", rows: 100 }),
      item({ name: "b", rows: 5000 }),
      item({ name: "c", rows: null }),
    ];
    const sorted = sortEntities(items, DEFAULT_ENTITY_SORT);
    expect(sorted.map((r) => r.name)).toEqual(["b", "a", "c"]);
  });

  it("sorts by name ascending alphabetically", () => {
    const items = [item({ name: "zebra" }), item({ name: "alpha" }), item({ name: "mid" })];
    const sorted = sortEntities(items, { key: "name", dir: 1 });
    expect(sorted.map((r) => r.name)).toEqual(["alpha", "mid", "zebra"]);
  });

  it("sorts by PII severity rank, not the label string", () => {
    const items = [
      item({ name: "none", pii_level: "none" }),
      item({ name: "floor", pii_level: "catastrophic" }),
      item({ name: "pii", pii_level: "pii" }),
    ];
    const sorted = sortEntities(items, { key: "pii_level", dir: -1 });
    expect(sorted.map((r) => r.name)).toEqual(["floor", "pii", "none"]);
  });

  it("sorts by group label string, ascending", () => {
    const items = [
      item({ name: "a", group: "identity" }), // "Identity"
      item({ name: "b", group: "activity" }), // "Activity & logs"
      item({ name: "c", group: "billing" }), // "Billing & revenue"
    ];
    const sorted = sortEntities(items, { key: "group", dir: 1 });
    expect(sorted.map((r) => r.name)).toEqual(["b", "c", "a"]);
  });

  it("sorts by confidence rank with null ranked last (descending)", () => {
    const items = [
      item({ name: "lo", confidence: "low" }),
      item({ name: "hi", confidence: "high" }),
      item({ name: "none", confidence: null }),
      item({ name: "med", confidence: "medium" }),
    ];
    const sorted = sortEntities(items, { key: "confidence", dir: -1 });
    expect(sorted.map((r) => r.name)).toEqual(["hi", "med", "lo", "none"]);
  });

  it("sorts by numeric metrics_count and joins_count", () => {
    const items = [
      item({ name: "a", metrics_count: 1, joins_count: 5 }),
      item({ name: "b", metrics_count: 9, joins_count: 0 }),
    ];
    expect(sortEntities(items, { key: "metrics_count", dir: -1 }).map((r) => r.name)).toEqual([
      "b",
      "a",
    ]);
    expect(sortEntities(items, { key: "joins_count", dir: -1 }).map((r) => r.name)).toEqual([
      "a",
      "b",
    ]);
  });

  it("does not mutate the input array", () => {
    const items = [item({ name: "a", rows: 1 }), item({ name: "b", rows: 2 })];
    const snapshot = items.map((r) => r.name);
    sortEntities(items, { key: "rows", dir: -1 });
    expect(items.map((r) => r.name)).toEqual(snapshot);
  });
});

describe("cardinalityLabel", () => {
  it("maps each cardinality to its compact badge", () => {
    expect(cardinalityLabel("one_to_one")).toBe("1:1");
    expect(cardinalityLabel("one_to_many")).toBe("1:N");
    expect(cardinalityLabel("many_to_one")).toBe("N:1");
    expect(cardinalityLabel("many_to_many")).toBe("N:N");
  });

  it("returns null when cardinality is unknown so the badge is omitted", () => {
    expect(cardinalityLabel(null)).toBeNull();
  });
});

describe("columnIsCatastrophic", () => {
  it("is true when any category is a catastrophic floor category", () => {
    expect(columnIsCatastrophic(["credential"])).toBe(true);
    expect(columnIsCatastrophic(["contact", "payment_card"])).toBe(true);
  });

  it("is false for non-floor categories and empty lists", () => {
    expect(columnIsCatastrophic(["contact"])).toBe(false);
    expect(columnIsCatastrophic([])).toBe(false);
  });
});

describe("categoryLabel", () => {
  it("uppercases and de-underscores the category name", () => {
    expect(categoryLabel("payment_card")).toBe("PAYMENT CARD");
    expect(categoryLabel("credential")).toBe("CREDENTIAL");
    expect(categoryLabel("government_id")).toBe("GOVERNMENT ID");
  });
});

describe("topSimilarColumns", () => {
  function col(name: string, similar: EntityColumn["similar"]): EntityColumn {
    return { name, data_type: "text", description: null, sensitivity: "public", pii_categories: [], similar };
  }

  it("flattens per-column neighbours into source-tagged pairs, highest score first", () => {
    const columns = [
      col("email", [{ schema: "public", table: "invoice", column: "billing_email", score: 0.94 }]),
      col("id", [{ schema: "public", table: "session", column: "user_id", score: 0.71 }]),
    ];
    const { shown, hidden } = topSimilarColumns(columns, 10);
    expect(hidden).toBe(0);
    expect(shown.map((p) => `${p.source}~${p.column}@${p.score}`)).toEqual([
      "email~billing_email@0.94",
      "id~user_id@0.71",
    ]);
  });

  it("caps at the limit and reports the hidden remainder (never a silent cap)", () => {
    const columns = [
      col("a", [
        { schema: "s", table: "t", column: "c1", score: 0.9 },
        { schema: "s", table: "t", column: "c2", score: 0.8 },
        { schema: "s", table: "t", column: "c3", score: 0.7 },
      ]),
    ];
    const { shown, hidden } = topSimilarColumns(columns, 2);
    expect(shown).toHaveLength(2);
    expect(hidden).toBe(1);
    expect(shown.map((p) => p.column)).toEqual(["c1", "c2"]);
  });

  it("returns nothing when no column has neighbours", () => {
    const { shown, hidden } = topSimilarColumns([col("x", [])], 6);
    expect(shown).toEqual([]);
    expect(hidden).toBe(0);
  });
});

describe("nextSort", () => {
  it("toggles direction when re-selecting the active column", () => {
    expect(nextSort({ key: "rows", dir: -1 }, "rows")).toEqual({ key: "rows", dir: 1 });
    expect(nextSort({ key: "rows", dir: 1 }, "rows")).toEqual({ key: "rows", dir: -1 });
  });

  it("switches to a new column descending-first", () => {
    expect(nextSort({ key: "rows", dir: 1 }, "name")).toEqual({ key: "name", dir: -1 });
  });
});
