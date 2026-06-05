import { describe, expect, it } from "vitest";
import type { DictColumn, DictEntity } from "@/lib/types";
import {
  categoryChipKind,
  columnHasPii,
  columnIsCatastrophic,
  entityDotVar,
  entityPiiLevel,
  leadCategory,
  piiColumns,
} from "@/lib/dict/present";

function col(overrides: Partial<DictColumn>): DictColumn {
  return {
    name: "c",
    data_type: "text",
    nullable: false,
    is_primary_key: false,
    is_identity: false,
    description: null,
    pii_sensitivity: "public",
    pii_categories: [],
    ...overrides,
  };
}

function entity(columns: DictColumn[]): DictEntity {
  return {
    name: "e",
    description: "",
    qualified_table: "public.e",
    identity: "id",
    group: "identity",
    columns,
    joins: [],
    metrics: [],
  };
}

describe("column PII predicates", () => {
  it("flags a catastrophic-floor column", () => {
    expect(columnIsCatastrophic(col({ pii_categories: ["credential"] }))).toBe(true);
    expect(columnIsCatastrophic(col({ pii_categories: ["contact"] }))).toBe(false);
  });

  it("treats categories OR pii-sensitivity as PII", () => {
    expect(columnHasPii(col({ pii_categories: ["contact"] }))).toBe(true);
    expect(columnHasPii(col({ pii_sensitivity: "pii" }))).toBe(true);
    expect(columnHasPii(col({ pii_sensitivity: "internal" }))).toBe(false);
  });
});

describe("entityPiiLevel / entityDotVar", () => {
  it("is catastrophic when any column is on the floor", () => {
    const e = entity([col({}), col({ pii_categories: ["government_id"] })]);
    expect(entityPiiLevel(e)).toBe("catastrophic");
    expect(entityDotVar(e)).toBe("var(--alarm)");
  });

  it("is pii when a non-floor PII column exists", () => {
    const e = entity([col({ pii_categories: ["contact"] })]);
    expect(entityPiiLevel(e)).toBe("pii");
    expect(entityDotVar(e)).toBe("var(--cyan)");
  });

  it("is none with no PII → no dot", () => {
    const e = entity([col({})]);
    expect(entityPiiLevel(e)).toBe("none");
    expect(entityDotVar(e)).toBeNull();
  });
});

describe("piiColumns + leadCategory", () => {
  it("returns only columns carrying a category", () => {
    const tagged = col({ name: "email", pii_categories: ["contact"] });
    const e = entity([col({ name: "id" }), tagged]);
    expect(piiColumns(e)).toEqual([tagged]);
  });

  it("prefers a catastrophic tag as the lead", () => {
    expect(leadCategory(col({ pii_categories: ["contact", "credential"] }))).toBe("credential");
    expect(leadCategory(col({ pii_categories: ["contact"] }))).toBe("contact");
    expect(leadCategory(col({}))).toBeNull();
  });
});

describe("categoryChipKind", () => {
  it("alarms only the catastrophic floor; muted otherwise", () => {
    expect(categoryChipKind("payment_card")).toBe("payment");
    expect(categoryChipKind("credential")).toBe("auth");
    expect(categoryChipKind("government_id")).toBe("auth");
    expect(categoryChipKind("contact")).toBe("contact");
    expect(categoryChipKind("location")).toBe("neutral");
  });
});
