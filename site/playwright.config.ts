import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright config for the marketing site smoke.
 *
 * The landing is a static, backend-free page, so the webServer just runs
 * `next dev` on :3001 (the static export renders identically). A single
 * Chromium project keeps the smoke lean — the heavy multi-breakpoint ×
 * dual-theme matrix is the dashboard's concern, not the marketing page's.
 */
export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: process.env.CI ? "list" : "html",
  use: {
    baseURL: "http://localhost:3001",
    trace: "on-first-retry",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: "pnpm dev",
    url: "http://localhost:3001",
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
