import { describe, expect, it } from "vitest";
import { PII_CATEGORIES, SENSITIVITIES } from "@/lib/types";
import {
  CATEGORY_LABELS,
  isCatastrophicCategory,
  SENSITIVITY_LABELS,
} from "@/lib/dict/labels";

// Completeness pin: every category / sensitivity in the closed taxonomy has
// a label, so the serialiser never renders a bare enum token. (Value
// correctness for exercised entries is pinned by the golden parity test.)
describe("dictionary labels", () => {
  it("labels every PII category", () => {
    expect(Object.keys(CATEGORY_LABELS).sort()).toEqual([...PII_CATEGORIES].sort());
  });

  it("labels every sensitivity level", () => {
    expect(Object.keys(SENSITIVITY_LABELS).sort()).toEqual([...SENSITIVITIES].sort());
  });

  it("flags exactly the catastrophic-leak categories", () => {
    const flagged = PII_CATEGORIES.filter(isCatastrophicCategory).sort();
    expect(flagged).toEqual(["credential", "government_id", "payment_card"]);
  });
});
