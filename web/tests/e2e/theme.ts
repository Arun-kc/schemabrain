import type { BrowserContext, TestInfo } from "@playwright/test";

/**
 * Theme harness for the dashboard E2E.
 *
 * The app reads its theme from `localStorage["schemabrain.theme"]` before the
 * first hydration (app/layout.tsx). To exercise a theme deterministically we
 * seed that key with an init script, derived from the Playwright project name
 * (e.g. `chromium-dark` → dark, `chromium-light` → light) so the same specs
 * run under both themes without per-spec branching.
 */
export type E2ETheme = "light" | "dark";

/** Intended theme for the running project (defaults to dark for legacy names). */
export function themeForProject(testInfo: TestInfo): E2ETheme {
  return testInfo.project.name.endsWith("light") ? "light" : "dark";
}

/** Seed the persisted theme before the first navigation/hydration. */
export async function pinTheme(context: BrowserContext, theme: E2ETheme): Promise<void> {
  await context.addInitScript((value) => {
    try {
      window.localStorage.setItem("schemabrain.theme", value);
    } catch {
      // localStorage can throw in private-mode-style sandboxes; the theme
      // just falls back to the page default (Atlas/light).
    }
  }, theme);
}
