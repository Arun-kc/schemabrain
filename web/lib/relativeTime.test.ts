import { describe, expect, it } from "vitest";
import { formatRelativeTime } from "./relativeTime";

// Fixed "now" so every case is deterministic (2024-01-01T00:00:00Z).
const NOW_MS = 1_704_067_200_000;
const NOW_S = NOW_MS / 1000;

describe("formatRelativeTime", () => {
  it("renders 'just now' for very recent timestamps", () => {
    expect(formatRelativeTime(NOW_S - 5, NOW_MS)).toBe("just now");
    expect(formatRelativeTime(NOW_S - 44, NOW_MS)).toBe("just now");
  });

  it("renders 'just now' for future timestamps (clock skew, never negative)", () => {
    expect(formatRelativeTime(NOW_S + 120, NOW_MS)).toBe("just now");
  });

  it("renders minutes", () => {
    expect(formatRelativeTime(NOW_S - 60, NOW_MS)).toBe("1m ago");
    expect(formatRelativeTime(NOW_S - 59 * 60, NOW_MS)).toBe("59m ago");
  });

  it("renders hours", () => {
    expect(formatRelativeTime(NOW_S - 6 * 3600, NOW_MS)).toBe("6h ago");
    expect(formatRelativeTime(NOW_S - 23 * 3600, NOW_MS)).toBe("23h ago");
  });

  it("renders days", () => {
    expect(formatRelativeTime(NOW_S - 3 * 86400, NOW_MS)).toBe("3d ago");
    expect(formatRelativeTime(NOW_S - 29 * 86400, NOW_MS)).toBe("29d ago");
  });

  it("renders months", () => {
    expect(formatRelativeTime(NOW_S - 60 * 86400, NOW_MS)).toBe("2mo ago");
  });

  it("renders years", () => {
    expect(formatRelativeTime(NOW_S - 400 * 86400, NOW_MS)).toBe("1y ago");
  });
});
