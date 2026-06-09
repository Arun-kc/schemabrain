import { defineConfig, devices } from "@playwright/test";

// Visual-regression config — DECOUPLED from the behavioural E2E config
// (playwright.config.ts), which `testIgnore`s visual.spec.ts.
//
// Why a separate config + a pinned container in CI: pixel baselines are
// environment-specific (fonts, anti-aliasing, GPU). The behavioural job runs on
// a bare `ubuntu-latest` runner whose image GitHub bumps over time; comparing
// pixels there would re-churn every baseline on each image bump. So the visual
// suite runs ONLY inside the pinned `mcr.microsoft.com/playwright:v1.60.0-noble`
// image — the SAME image used to generate the baselines locally
// (scripts/update_visual_baselines.sh) — so generation and assertion render in
// byte-identical environments. The `-linux` suffix Playwright stamps on each
// snapshot matches that container.
//
// Like the behavioural config, this assumes the sidecar is already serving the
// built dashboard export on http://127.0.0.1:7878; the suite route-injects all
// `/api/*` data (tests/e2e/fixtures.ts), so only the static HTML/JS/CSS/fonts
// are served live.

const BASE_URL = process.env.SCHEMABRAIN_E2E_BASE_URL ?? "http://127.0.0.1:7878";

export default defineConfig({
  testDir: "./tests/e2e",
  testMatch: ["visual.spec.ts"],
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: 0,
  reporter: [["list"], ["html", { open: "never", outputFolder: "playwright-report" }]],
  outputDir: "test-results",
  timeout: 45_000,
  expect: {
    timeout: 10_000,
    toHaveScreenshot: {
      // Generation + assertion share one pinned image, so renders are near
      // identical; a small ratio absorbs the rare anti-aliasing pixel without
      // letting a real regression through.
      maxDiffPixelRatio: 0.01,
      stylePath: "./tests/e2e/screenshot.css",
      animations: "disabled",
      caret: "hide",
    },
  },
  // Baselines live next to the suite, namespaced by project + platform so the
  // dark/light × linux matrix never collides:
  //   tests/e2e/__screenshots__/visual-dark/overview-mobile-linux.png
  snapshotPathTemplate: "{testDir}/__screenshots__/{projectName}/{arg}-{platform}{ext}",
  use: {
    baseURL: BASE_URL,
    trace: "on-first-retry",
    video: "off",
    actionTimeout: 10_000,
    navigationTimeout: 15_000,
  },
  // Theme is derived from the project-name suffix by tests/e2e/theme.ts
  // (themeForProject → endsWith("light")), matching the behavioural projects.
  projects: [
    {
      name: "visual-dark",
      use: { ...devices["Desktop Chrome"], colorScheme: "dark" },
    },
    {
      name: "visual-light",
      use: { ...devices["Desktop Chrome"], colorScheme: "light" },
    },
  ],
});
