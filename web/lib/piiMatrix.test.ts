import { describe, expect, it } from "vitest";
import type { PiiMatrixColumn } from "@/lib/types";
import type { PolicyFilter } from "@/lib/policy";
import {
  cellAriaLabel,
  cellState,
  columnStatus,
  filterMatrixColumns,
  isFloorColumn,
  isPiiColumn,
  matchesMatrixFilter,
  qualifiedColumn,
  sortFloorFirst,
  statusLabel,
  summarizeMatrix,
} from "./piiMatrix";

function col(overrides: Partial<PiiMatrixColumn> = {}): PiiMatrixColumn {
  return {
    entity: "users",
    qualified_table: "public.users",
    column_name: "email",
    sensitivity: "pii",
    categories: ["contact"],
    pii_confidence: "high",
    ...overrides,
  };
}

const NO_FILTER: PolicyFilter = { query: "", category: null, status: null };

describe("qualifiedColumn", () => {
  it("joins qualified table and column name", () => {
    expect(qualifiedColumn(col({ qualified_table: "public.cards", column_name: "pan" }))).toBe(
      "public.cards.pan",
    );
  });
});

describe("isFloorColumn", () => {
  it("is true when a category is catastrophic", () => {
    expect(isFloorColumn(col({ categories: ["payment_card"] }))).toBe(true);
    expect(isFloorColumn(col({ categories: ["contact", "credential"] }))).toBe(true);
  });

  it("is false for non-catastrophic categories and for empty columns", () => {
    expect(isFloorColumn(col({ categories: ["contact", "location"] }))).toBe(false);
    expect(isFloorColumn(col({ categories: [] }))).toBe(false);
  });
});

describe("isPiiColumn", () => {
  it("is true only when the column carries a category", () => {
    expect(isPiiColumn(col({ categories: ["contact"] }))).toBe(true);
    expect(isPiiColumn(col({ categories: [], sensitivity: "public" }))).toBe(false);
  });
});

describe("columnStatus", () => {
  it("blocks floor columns regardless of sensitivity", () => {
    expect(columnStatus(col({ categories: ["credential"], sensitivity: "pii" }))).toBe("block");
  });

  it("redacts non-floor pii and confidential columns", () => {
    expect(columnStatus(col({ categories: ["contact"], sensitivity: "pii" }))).toBe("redact");
    expect(columnStatus(col({ categories: ["location"], sensitivity: "confidential" }))).toBe(
      "redact",
    );
  });

  it("allows public and internal columns", () => {
    expect(columnStatus(col({ categories: [], sensitivity: "public" }))).toBe("allow");
    expect(columnStatus(col({ categories: [], sensitivity: "internal" }))).toBe("allow");
  });
});

describe("cellState", () => {
  it("is unlit for a category the column does not carry", () => {
    expect(cellState(col({ categories: ["contact"] }), "health")).toBe("unlit");
  });

  it("maps the column band onto a member cell", () => {
    expect(cellState(col({ categories: ["contact"], pii_confidence: "floor_locked" }), "contact")).toBe(
      "floor",
    );
    expect(cellState(col({ categories: ["contact"], pii_confidence: "high" }), "contact")).toBe(
      "high",
    );
    expect(cellState(col({ categories: ["contact"], pii_confidence: "medium" }), "contact")).toBe(
      "medium",
    );
    expect(cellState(col({ categories: ["contact"], pii_confidence: "low" }), "contact")).toBe("low");
  });

  it("renders a member cell with no band as nullband, never a faked 0", () => {
    expect(cellState(col({ categories: ["contact"], pii_confidence: null }), "contact")).toBe(
      "nullband",
    );
  });

  it("applies the column band uniformly to every lit cell", () => {
    const multi = col({ categories: ["payment_card", "financial"], pii_confidence: "floor_locked" });
    expect(cellState(multi, "payment_card")).toBe("floor");
    expect(cellState(multi, "financial")).toBe("floor");
  });
});

describe("sortFloorFirst", () => {
  it("pins floor columns to the top while preserving order within each group", () => {
    const a = col({ column_name: "a", categories: ["contact"] });
    const cred = col({ column_name: "cred", categories: ["credential"], pii_confidence: "floor_locked" });
    const b = col({ column_name: "b", categories: ["location"] });
    const pan = col({ column_name: "pan", categories: ["payment_card"], pii_confidence: "floor_locked" });

    const sorted = sortFloorFirst([a, cred, b, pan]);

    expect(sorted.map((c) => c.column_name)).toEqual(["cred", "pan", "a", "b"]);
  });

  it("returns a new array and leaves the input untouched", () => {
    const input = [col()];
    const out = sortFloorFirst(input);
    expect(out).not.toBe(input);
  });
});

describe("matchesMatrixFilter", () => {
  it("matches a query against table, column, and prettified categories", () => {
    const c = col({ qualified_table: "public.orders", column_name: "ship_to", categories: ["location"] });
    expect(matchesMatrixFilter(c, { ...NO_FILTER, query: "orders" })).toBe(true);
    expect(matchesMatrixFilter(c, { ...NO_FILTER, query: "ship_to" })).toBe(true);
    expect(matchesMatrixFilter(c, { ...NO_FILTER, query: "location" })).toBe(true);
    expect(matchesMatrixFilter(c, { ...NO_FILTER, query: "nope" })).toBe(false);
  });

  it("matches the category facet against the column's categories", () => {
    const c = col({ categories: ["contact"] });
    expect(matchesMatrixFilter(c, { ...NO_FILTER, category: "contact" })).toBe(true);
    expect(matchesMatrixFilter(c, { ...NO_FILTER, category: "health" })).toBe(false);
  });

  it("matches the derived status facet", () => {
    const floor = col({ categories: ["credential"], pii_confidence: "floor_locked" });
    const pii = col({ categories: ["contact"], sensitivity: "pii" });
    const open = col({ categories: [], sensitivity: "public" });
    expect(matchesMatrixFilter(floor, { ...NO_FILTER, status: "block" })).toBe(true);
    expect(matchesMatrixFilter(pii, { ...NO_FILTER, status: "redact" })).toBe(true);
    expect(matchesMatrixFilter(open, { ...NO_FILTER, status: "allow" })).toBe(true);
    expect(matchesMatrixFilter(pii, { ...NO_FILTER, status: "block" })).toBe(false);
  });

  it("treats a floor status filter as floor-column membership", () => {
    const floor = col({ categories: ["payment_card"], pii_confidence: "floor_locked" });
    const pii = col({ categories: ["contact"] });
    expect(matchesMatrixFilter(floor, { ...NO_FILTER, status: "floor" })).toBe(true);
    expect(matchesMatrixFilter(pii, { ...NO_FILTER, status: "floor" })).toBe(false);
  });
});

describe("filterMatrixColumns", () => {
  it("returns the same reference when the filter is empty", () => {
    const cols = [col()];
    expect(filterMatrixColumns(cols, NO_FILTER)).toBe(cols);
  });

  it("filters by the active facets", () => {
    const cols = [
      col({ column_name: "email", categories: ["contact"] }),
      col({ column_name: "pan", categories: ["payment_card"], pii_confidence: "floor_locked" }),
    ];
    const out = filterMatrixColumns(cols, { ...NO_FILTER, status: "block" });
    expect(out.map((c) => c.column_name)).toEqual(["pan"]);
  });
});

describe("summarizeMatrix", () => {
  it("tallies dispositions over the full set", () => {
    const cols = [
      col({ categories: ["credential"], pii_confidence: "floor_locked" }),
      col({ categories: ["contact"], sensitivity: "pii" }),
      col({ categories: ["location"], sensitivity: "confidential" }),
      col({ categories: [], sensitivity: "public" }),
    ];
    expect(summarizeMatrix(cols)).toEqual({ total: 4, blocked: 1, redacted: 2, allowed: 1 });
  });

  it("is zero across the board for an empty set", () => {
    expect(summarizeMatrix([])).toEqual({ total: 0, blocked: 0, redacted: 0, allowed: 0 });
  });
});

describe("cellAriaLabel", () => {
  it("describes every band beyond colour", () => {
    expect(
      cellAriaLabel(col({ categories: ["credential"], pii_confidence: "floor_locked" }), "credential"),
    ).toMatch(/floor-locked/);
    expect(cellAriaLabel(col({ categories: ["contact"], pii_confidence: "high" }), "contact")).toMatch(
      /band high/,
    );
    expect(
      cellAriaLabel(col({ categories: ["contact"], pii_confidence: "medium" }), "contact"),
    ).toMatch(/band medium/);
    expect(cellAriaLabel(col({ categories: ["contact"], pii_confidence: "low" }), "contact")).toMatch(
      /band low/,
    );
    expect(cellAriaLabel(col({ categories: ["contact"], pii_confidence: null }), "contact")).toMatch(
      /no advisory band/,
    );
    expect(cellAriaLabel(col({ categories: ["contact"] }), "health")).toMatch(/not classified/);
  });

  it("prefixes the qualified column and prettified category", () => {
    const label = cellAriaLabel(
      col({ qualified_table: "public.users", column_name: "ip", categories: ["online_identifier"] }),
      "online_identifier",
    );
    expect(label).toMatch(/public\.users\.ip · online identifier/);
  });
});

describe("statusLabel", () => {
  it("renders the uppercase verb", () => {
    expect(statusLabel("block")).toBe("BLOCKED");
    expect(statusLabel("redact")).toBe("REDACT");
    expect(statusLabel("allow")).toBe("ALLOW");
  });
});
