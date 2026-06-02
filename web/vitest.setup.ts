import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

// Unmount React trees between tests so DOM assertions never leak across
// cases (RTL does not auto-clean under vitest's globals).
afterEach(() => {
  cleanup();
});
