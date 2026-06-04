import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Vitest config for the dashboard.
//
// Component unit tests live alongside the components they cover
// (e.g. `components/kit/kit.test.tsx`) and run in jsdom with React
// Testing Library. The Playwright spec under `tests/e2e/` is a
// different `test()` symbol and is excluded so vitest never imports it.
//
// Coverage is scoped to the new design-system kit + the shared theme
// hook. Scoping keeps the gate meaningful for the code this PR introduces
// without demanding tests for the legacy surfaces (which are covered when
// they are reskinned onto the kit).
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./", import.meta.url)),
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./vitest.setup.ts"],
    exclude: ["**/node_modules/**", "tests/e2e/**", "playwright-report/**"],
    coverage: {
      provider: "v8",
      include: [
        "components/kit/**/*.{ts,tsx}",
        "lib/useTheme.ts",
        "lib/policy.ts",
        "lib/piiMatrix.ts",
        "lib/relativeTime.ts",
      ],
      exclude: ["**/index.ts"],
      reporter: ["text", "html"],
      thresholds: {
        statements: 85,
        branches: 85,
        functions: 85,
        lines: 85,
      },
    },
  },
});
