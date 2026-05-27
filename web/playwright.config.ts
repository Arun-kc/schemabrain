import { defineConfig, devices } from "@playwright/test";

// Playwright config for the dashboard E2E smoke.
//
// Scope: tiny. Assumes the sidecar is already running on
// http://127.0.0.1:7878 against a store with at least one indexed
// source AND at least one refused audit row. The README walks an
// operator through the prerequisite boot.
//
// CI integration is intentionally OUT OF SCOPE for this PR — once
// the spec is stable locally, a follow-up adds a workflow job that
// brings up Postgres + indexes + boots the sidecar before invoking
// `pnpm test:e2e`. Until then, this config runs reliably on a dev
// machine and produces screenshots for visual review.

const BASE_URL = process.env.SCHEMABRAIN_E2E_BASE_URL ?? "http://127.0.0.1:7878";

export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: false,
  // The sidecar binds 127.0.0.1 only and the spec is read-only against
  // a shared store; running tests in parallel would just race the
  // theme-toggle localStorage init across workers without any speed
  // win on a 4-spec suite.
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: 0,
  reporter: [["list"], ["html", { open: "never", outputFolder: "playwright-report" }]],
  outputDir: "test-results",
  timeout: 20_000,
  expect: { timeout: 5_000 },
  use: {
    baseURL: BASE_URL,
    trace: "on-first-retry",
    screenshot: "only-on-failure",
    video: "off",
    actionTimeout: 5_000,
    navigationTimeout: 10_000,
  },
  projects: [
    {
      name: "chromium-dark",
      use: { ...devices["Desktop Chrome"], colorScheme: "dark" },
    },
  ],
});
