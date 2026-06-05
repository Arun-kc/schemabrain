import { afterEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { useReducedMotion } from "./useReducedMotion";

/** A controllable matchMedia stub: flip `matches` then fire `change`. */
function installMatchMedia(initialMatches: boolean) {
  const listeners = new Set<() => void>();
  const mql = {
    matches: initialMatches,
    media: "(prefers-reduced-motion: reduce)",
    onchange: null,
    addEventListener: (_: string, cb: () => void) => listeners.add(cb),
    removeEventListener: (_: string, cb: () => void) => listeners.delete(cb),
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  };
  const matchMedia = vi.fn().mockReturnValue(mql);
  vi.stubGlobal("matchMedia", matchMedia);
  return {
    matchMedia,
    setMatches(next: boolean) {
      mql.matches = next;
      for (const cb of listeners) cb();
    },
    listenerCount: () => listeners.size,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("useReducedMotion", () => {
  it("reports the current preference after mount", () => {
    installMatchMedia(true);
    const { result } = renderHook(() => useReducedMotion());
    expect(result.current).toBe(true);
  });

  it("returns false when motion is allowed", () => {
    installMatchMedia(false);
    const { result } = renderHook(() => useReducedMotion());
    expect(result.current).toBe(false);
  });

  it("reacts to a preference change while mounted", () => {
    const mm = installMatchMedia(false);
    const { result } = renderHook(() => useReducedMotion());
    expect(result.current).toBe(false);

    act(() => mm.setMatches(true));
    expect(result.current).toBe(true);
  });

  it("unsubscribes the change listener on unmount", () => {
    const mm = installMatchMedia(false);
    const { unmount } = renderHook(() => useReducedMotion());
    expect(mm.listenerCount()).toBe(1);

    unmount();
    expect(mm.listenerCount()).toBe(0);
  });

  it("stays false when the environment has no matchMedia", () => {
    vi.stubGlobal("matchMedia", undefined);
    const { result } = renderHook(() => useReducedMotion());
    expect(result.current).toBe(false);
  });
});
