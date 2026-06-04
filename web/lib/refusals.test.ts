import { describe, expect, it } from "vitest";
import type { PIICategory, RefusalReason } from "@/lib/types";
import type { RefusalEntry } from "@/lib/types";
import {
  buildRecoveryHint,
  bucketBlurb,
  bucketChipKind,
  bucketCounts,
  bucketIcon,
  bucketLabel,
  BUCKET_ORDER,
  categoryChipKind,
  filterByBucket,
  isFloorRefusal,
  refusalBucket,
  summarizeRefusals,
} from "./refusals";

// A minimal RefusalEntry factory — only the fields the helper reads matter.
function entry(over: Partial<RefusalEntry> = {}): RefusalEntry {
  return {
    id: 1,
    occurred_at: "2024-01-01T00:00:00Z",
    source_connection_id: "src",
    caller_id: null,
    tool_name: "query_rows",
    status: "refused",
    refusal_reason: "pii_blocked",
    cost_class: "refused",
    pii_categories: ["contact"],
    ast_shape_hash_hex: null,
    rule_id: null,
    fingerprint_hex: "ab",
    fingerprint_version: "1",
    chain_hash_hex: "cd",
    envelope_reconstructed: false,
    ...over,
  };
}

describe("refusalBucket", () => {
  it("groups the six reasons into the three locked buckets", () => {
    expect(refusalBucket("pii_blocked")).toBe("sensitive");
    expect(refusalBucket("allowlist_violation")).toBe("sensitive");
    expect(refusalBucket("fragment_unsafe")).toBe("unsafe");
    expect(refusalBucket("cost_cap_exceeded")).toBe("unsafe");
    expect(refusalBucket("ambiguous_resolution")).toBe("integrity");
    expect(refusalBucket("schema_drift")).toBe("integrity");
  });

  it("defaults a null reason to the sensitive bucket (the protective default)", () => {
    expect(refusalBucket(null)).toBe("sensitive");
  });

  it("falls back to sensitive for an unknown reason (future enum value, never dropped)", () => {
    // Simulates a 7th RefusalReason added to the engine before this map is
    // updated — the exhaustiveness sentinel fails the build, but at runtime the
    // value still maps to the protective default rather than being dropped.
    expect(refusalBucket("rate_limited" as RefusalReason)).toBe("sensitive");
  });
});

describe("bucket presentation helpers", () => {
  it("labels every bucket", () => {
    expect(BUCKET_ORDER.map(bucketLabel)).toEqual([
      "Sensitive-data",
      "Unsafe-query",
      "Integrity",
    ]);
  });

  it("assigns a distinct grayscale-safe icon per bucket", () => {
    expect(bucketIcon("sensitive")).toBe("shield-check");
    expect(bucketIcon("unsafe")).toBe("ban");
    expect(bucketIcon("integrity")).toBe("scale");
  });

  it("gives each bucket a plain-language blurb", () => {
    for (const b of BUCKET_ORDER) {
      expect(bucketBlurb(b).length).toBeGreaterThan(0);
    }
  });
});

describe("isFloorRefusal", () => {
  it("is true when any category is catastrophic", () => {
    expect(isFloorRefusal({ pii_categories: ["credential"] })).toBe(true);
    expect(isFloorRefusal({ pii_categories: ["contact", "payment_card"] })).toBe(true);
    expect(isFloorRefusal({ pii_categories: ["government_id"] })).toBe(true);
  });

  it("is false for non-catastrophic or empty category sets", () => {
    expect(isFloorRefusal({ pii_categories: ["contact"] })).toBe(false);
    expect(isFloorRefusal({ pii_categories: ["location", "behavioral"] })).toBe(false);
    expect(isFloorRefusal({ pii_categories: [] })).toBe(false);
  });
});

describe("categoryChipKind (the floor-aware fix)", () => {
  it("maps the catastrophic categories to the alarm chip — the bug the matrix helper has", () => {
    // piiCategoryToChipKind("credential") returns "none"; this MUST not.
    expect(categoryChipKind("credential")).toBe("auth");
    expect(categoryChipKind("payment_card")).toBe("auth");
    expect(categoryChipKind("government_id")).toBe("auth");
  });

  it("maps contact to the amber contact chip", () => {
    expect(categoryChipKind("contact")).toBe("contact");
  });

  it("maps every other category to the muted none chip", () => {
    const muted: PIICategory[] = [
      "financial",
      "health",
      "genetic",
      "biometric",
      "behavioral",
      "online_identifier",
      "location",
      "demographic_protected",
    ];
    for (const c of muted) expect(categoryChipKind(c)).toBe("none");
  });
});

describe("bucketChipKind", () => {
  it("alarms the sensitive badge only when a floor category is present", () => {
    expect(bucketChipKind("sensitive", true)).toBe("auth");
    expect(bucketChipKind("sensitive", false)).toBe("cyan");
  });

  it("keeps the other buckets neutral regardless of floor", () => {
    expect(bucketChipKind("unsafe", true)).toBe("neutral");
    expect(bucketChipKind("unsafe", false)).toBe("neutral");
    expect(bucketChipKind("integrity", true)).toBe("neutral");
  });
});

describe("bucketCounts + filterByBucket", () => {
  const entries = [
    entry({ id: 1, refusal_reason: "pii_blocked" }),
    entry({ id: 2, refusal_reason: "pii_blocked" }),
    entry({ id: 3, refusal_reason: "fragment_unsafe" }),
    entry({ id: 4, refusal_reason: "schema_drift" }),
  ];

  it("tallies per bucket over the full set", () => {
    expect(bucketCounts(entries)).toEqual({ sensitive: 2, unsafe: 1, integrity: 1 });
  });

  it("filters to one bucket, order-preserving", () => {
    expect(filterByBucket(entries, "sensitive").map((e) => e.id)).toEqual([1, 2]);
    expect(filterByBucket(entries, "unsafe").map((e) => e.id)).toEqual([3]);
  });

  it("returns the input unchanged when the bucket is null", () => {
    expect(filterByBucket(entries, null)).toBe(entries);
  });

  it("handles the all-pii_blocked degraded reality (today) — two empty buckets", () => {
    const only = [entry({ id: 1 }), entry({ id: 2 })];
    expect(bucketCounts(only)).toEqual({ sensitive: 2, unsafe: 0, integrity: 0 });
  });
});

describe("summarizeRefusals", () => {
  // Fixed "now" so the windows are deterministic: 2024-01-01T12:00:00Z.
  const NOW = Date.parse("2024-01-01T12:00:00Z");

  it("counts the trailing-hour, today, and floor windows from real timestamps", () => {
    const rows = [
      entry({ occurred_at: "2024-01-01T11:30:00Z", pii_categories: ["credential"] }), // 30m ago, floor
      entry({ occurred_at: "2024-01-01T11:55:00Z", pii_categories: ["contact"] }), // 5m ago, non-floor
      entry({ occurred_at: "2024-01-01T03:00:00Z", pii_categories: ["payment_card"] }), // earlier today, floor
      entry({ occurred_at: "2023-12-28T09:00:00Z", pii_categories: ["contact"] }), // days ago
    ];
    const s = summarizeRefusals(rows, NOW);
    expect(s.lastHour).toBe(2);
    expect(s.floorHits).toBe(2);
    // "today" excludes the Dec-28 row; the three Jan-01 rows count (TZ-robust
    // because all three are well clear of any local midnight boundary at noon).
    expect(s.today).toBe(3);
  });

  it("never counts a future (clock-skewed) timestamp toward last hour", () => {
    const rows = [entry({ occurred_at: "2024-01-01T12:30:00Z", pii_categories: ["contact"] })];
    expect(summarizeRefusals(rows, NOW).lastHour).toBe(0);
  });

  it("ignores unparseable timestamps without throwing", () => {
    const rows = [entry({ occurred_at: "not-a-date", pii_categories: ["credential"] })];
    const s = summarizeRefusals(rows, NOW);
    expect(s.lastHour).toBe(0);
    expect(s.today).toBe(0);
    // floor is timestamp-independent — it still counts.
    expect(s.floorHits).toBe(1);
  });

  it("returns all-zero windows for an empty feed", () => {
    expect(summarizeRefusals([], NOW)).toEqual({ lastHour: 0, today: 0, floorHits: 0 });
  });
});

describe("buildRecoveryHint", () => {
  it("derives a per-category hint from the blocked categories", () => {
    expect(buildRecoveryHint(["credential"])).toMatch(/password_hash/);
    expect(buildRecoveryHint(["payment_card"])).toMatch(/card_number/);
  });

  it("joins multiple categories' hints", () => {
    const hint = buildRecoveryHint(["credential", "contact"]);
    expect(hint).toMatch(/password_hash/);
    expect(hint).toMatch(/email/);
  });

  it("falls back to a generic line when no category is present", () => {
    expect(buildRecoveryHint([])).toMatch(/widen the policy/);
  });
});
