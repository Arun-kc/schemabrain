import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

// jsdom ships no matchMedia. A default no-match stub keeps any component that
// reads a media query from crashing on mount; the useReducedMotion test
// installs its own controllable matchMedia over this default.
if (typeof window !== "undefined" && typeof window.matchMedia !== "function") {
  window.matchMedia = (query: string): MediaQueryList =>
    ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }) as unknown as MediaQueryList;
}

// jsdom ships no ResizeObserver — reactflow's store touches it when a node
// mounts inside a ReactFlowProvider. A no-op observer is enough for unit tests
// (the real layout/interaction is covered by the Playwright graph spec).
if (typeof globalThis.ResizeObserver === "undefined") {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
}

// Unmount React trees between tests so DOM assertions never leak across
// cases (RTL does not auto-clean under vitest's globals).
afterEach(() => {
  cleanup();
});
