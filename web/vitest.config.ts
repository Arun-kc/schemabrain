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
        // Pure graph data layer (the reactflow canvas itself is DOM-only and
        // is covered by the Playwright graph spec, not this unit gate).
        "components/graph/graphAdapter.ts",
        "components/graph/graphLayout.ts",
        // The floating chrome panels pull no reactflow runtime, so they are
        // unit-gated here; GraphCanvas/Graph (which do) stay e2e-covered.
        "components/graph/panels/**/*.{ts,tsx}",
        "lib/useTheme.ts",
        "lib/useReducedMotion.ts",
        "lib/policy.ts",
        "lib/piiMatrix.ts",
        "lib/refusals.ts",
        // Pure entity-surface view logic (the EntityDrilldownBody / EntitiesIndex
        // components themselves fetch + render, so — like the /pii Matrix and
        // /refusals Timeline — they are covered by RTL regression tests +
        // the Playwright entities spec, not this unit gate).
        "lib/entities.ts",
        "lib/merkle.ts",
        "lib/auditVerdict.ts",
        "lib/relativeTime.ts",
        // Pure data-dictionary layer: the Markdown serialiser (byte-for-byte
        // parity with the CLI golden), its label maps, and the browse-view
        // presentation helpers. The DataDictionary component itself fetches +
        // renders, so it is covered by the Playwright dict spec (PR-21), not
        // this unit gate.
        "lib/dict/**/*.ts",
        // Pure Overview stat helpers (percentage / scaling / is-rated). The
        // Overview component itself fetches + renders, so it is covered by the
        // Playwright overview spec, not this unit gate.
        "lib/overview/**/*.ts",
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
