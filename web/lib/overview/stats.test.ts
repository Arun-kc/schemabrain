import { describe, expect, it } from "vitest";
import type { OverviewConfidence } from "@/lib/types";
import { confidenceRated, maxCount, toPercent } from "./stats";

describe("toPercent", () => {
  it("computes a normal percentage", () => {
    expect(toPercent(25, 100)).toBe(25);
    expect(toPercent(6, 84)).toBeCloseTo(7.142, 2);
  });

  it("returns 0 when total is 0 (empty source → flat bar, never NaN)", () => {
    expect(toPercent(0, 0)).toBe(0);
    expect(toPercent(5, 0)).toBe(0);
  });

  it("returns 0 for a non-positive total", () => {
    expect(toPercent(5, -10)).toBe(0);
  });

  it("clamps to [0, 100]", () => {
    expect(toPercent(150, 100)).toBe(100);
    expect(toPercent(-5, 100)).toBe(0);
    expect(toPercent(84, 84)).toBe(100);
  });
});

describe("maxCount", () => {
  it("returns the largest value", () => {
    expect(maxCount([4, 5, 1])).toBe(5);
    expect(maxCount([0, 9, 3])).toBe(9);
  });

  it("returns 0 for an empty list", () => {
    expect(maxCount([])).toBe(0);
  });

  it("returns 0 when every value is 0", () => {
    expect(maxCount([0, 0, 0])).toBe(0);
  });
});

describe("confidenceRated", () => {
  const base: OverviewConfidence = { high: 0, medium: 0, low: 0, unrated: 0 };

  it("is false when all entities are unrated (hand-authored)", () => {
    expect(confidenceRated({ ...base, unrated: 10 })).toBe(false);
  });

  it("is true when any band is present", () => {
    expect(confidenceRated({ ...base, high: 1 })).toBe(true);
    expect(confidenceRated({ ...base, medium: 2, unrated: 3 })).toBe(true);
    expect(confidenceRated({ ...base, low: 1 })).toBe(true);
  });
});
