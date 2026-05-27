import { defineConfig } from "vitest/config";

// Vitest config kept intentionally tiny — its only job today is to
// prevent vitest from picking up the Playwright spec under
// `tests/e2e/`. Playwright's `test()` and vitest's `test()` are
// different symbols; vitest would otherwise crash on the e2e file
// with an import error.
//
// Component-level unit tests (when we add them) live alongside the
// components they cover (e.g. `web/components/.../Foo.test.tsx`) and
// get picked up by vitest's default discovery.

export default defineConfig({
  test: {
    exclude: ["**/node_modules/**", "tests/e2e/**", "playwright-report/**"],
  },
});
