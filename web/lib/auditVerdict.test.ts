import { describe, expect, it } from "vitest";
import { decideVerifyPhase } from "./auditVerdict";

describe("decideVerifyPhase", () => {
  it("is done when every visible row is proven and nothing is edited", () => {
    expect(
      decideVerifyPhase({ editedTotal: 0, proofFailed: 0, advanced: 0, proven: 50, attempted: 50 }),
    ).toBe("done");
  });

  it("is broken when an edited row is OUTSIDE the visible window (the HIGH regression)", () => {
    // The chain walk found 1 edited row across the whole log; all 50 visible
    // rows proved clean. The verdict must still be broken, not a false-green.
    expect(
      decideVerifyPhase({ editedTotal: 1, proofFailed: 0, advanced: 0, proven: 50, attempted: 50 }),
    ).toBe("broken");
  });

  it("is broken when a visible proof fails to reconcile", () => {
    expect(
      decideVerifyPhase({ editedTotal: 0, proofFailed: 1, advanced: 0, proven: 49, attempted: 50 }),
    ).toBe("broken");
  });

  it("prefers broken over advanced and partial", () => {
    expect(
      decideVerifyPhase({ editedTotal: 2, proofFailed: 0, advanced: 3, proven: 40, attempted: 50 }),
    ).toBe("broken");
  });

  it("is advanced when the log grew but nothing is edited or failed", () => {
    expect(
      decideVerifyPhase({ editedTotal: 0, proofFailed: 0, advanced: 2, proven: 48, attempted: 50 }),
    ).toBe("advanced");
  });

  it("is partial when some visible rows could not be proved (no edits, no drift)", () => {
    expect(
      decideVerifyPhase({ editedTotal: 0, proofFailed: 0, advanced: 0, proven: 48, attempted: 50 }),
    ).toBe("partial");
  });

  it("treats a single proven single-row tree as done", () => {
    expect(
      decideVerifyPhase({ editedTotal: 0, proofFailed: 0, advanced: 0, proven: 1, attempted: 1 }),
    ).toBe("done");
  });
});
