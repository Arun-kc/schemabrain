/**
 * Visual-regression baselines — every dashboard surface × the 4 standard
 * breakpoints × both themes (wsQA-visual-regression-baselines, PR-21b).
 *
 * Runs under playwright.visual.config.ts ONLY (the behavioural config
 * `testIgnore`s this file) and ONLY inside the pinned Playwright container so
 * the baselines stay environment-stable. See that config + web/tests/e2e/README.md.
 *
 * Determinism (the whole game for pixel baselines):
 *  - every `/api/*` response is a frozen fixture (tests/e2e/fixtures.ts), so no
 *    wall-clock timestamps or per-boot Merkle hashes leak into the render;
 *  - the browser clock is frozen (FROZEN_NOW_MS), so "Xm ago" relative times
 *    are constant;
 *  - prefers-reduced-motion is forced, so the graph force-sim never starts
 *    (it parks at its seed layout — see graph.spec.ts) and the indexing ring /
 *    sheet slide don't animate;
 *  - Playwright disables CSS animations + hides the caret, and screenshot.css
 *    hides scrollbars;
 *  - we await document.fonts.ready so a font swap never races the capture.
 *
 * 9 surfaces × 4 breakpoints × 2 theme projects = 72 baselines.
 */

import { expect, type Page, test } from "@playwright/test";
import { FROZEN_NOW_MS, routeAllApis } from "./fixtures";
import { pinTheme, themeForProject } from "./theme";
import { BREAKPOINT_NAMES, BREAKPOINTS } from "./viewports";

interface Surface {
  /** Route path under the dashboard. */
  path: string;
  /** Snapshot name stem (joined with the breakpoint). */
  name: string;
  /** Wait for the surface's load-bearing content before the capture. */
  ready: (page: Page) => Promise<void>;
}

const SURFACES: readonly Surface[] = [
  {
    path: "/overview",
    name: "overview",
    ready: async (page) => {
      await expect(
        page.getByRole("heading", { name: /Agents are reasoning over a protected graph/ }),
      ).toBeVisible();
    },
  },
  {
    path: "/graph",
    name: "graph",
    ready: async (page) => {
      const region = page.getByRole("region", { name: "Knowledge graph" });
      await expect(region).toBeVisible();
      await expect(page.locator(".react-flow__node").first()).toBeVisible();
      // The force sim is parked at its seed layout under reduced motion, so the
      // canvas is static and the capture is deterministic.
      await expect(region).toHaveAttribute("data-graph-loop", "parked");
    },
  },
  {
    path: "/entities",
    name: "entities",
    ready: async (page) => {
      await expect(page.getByRole("heading", { name: "Entities" })).toBeVisible();
    },
  },
  {
    path: "/dict",
    name: "dict",
    ready: async (page) => {
      await expect(page.getByRole("heading", { name: "Data dictionary", level: 1 })).toBeVisible();
    },
  },
  {
    path: "/pii",
    name: "pii",
    ready: async (page) => {
      await expect(page.getByRole("heading", { name: /PII matrix/i })).toBeVisible();
    },
  },
  {
    path: "/refusals",
    name: "refusals",
    ready: async (page) => {
      await expect(page.getByRole("heading", { name: /Refusals/i })).toBeVisible();
    },
  },
  {
    path: "/audit",
    name: "audit",
    ready: async (page) => {
      await expect(page.getByRole("heading", { name: /Audit/i })).toBeVisible();
      await expect(page.getByText(/merkle root/i)).toBeVisible();
    },
  },
  {
    path: "/policy",
    name: "policy",
    ready: async (page) => {
      await expect(page.getByRole("heading", { name: "Policy", exact: true })).toBeVisible();
      // The per-column grid groups tables collapsed-by-default (the radiogroups
      // live inside, hidden until expanded) — capture that honest default state,
      // so wait for the pane, not an expanded control.
      await expect(page.getByLabel("per-column enforcement")).toBeVisible();
    },
  },
  {
    path: "/drift",
    name: "drift",
    ready: async (page) => {
      await expect(page.getByRole("heading", { name: "Drift", exact: true })).toBeVisible();
    },
  },
];

test.describe("visual regression", () => {
  test.beforeEach(async ({ context, page }, testInfo) => {
    await pinTheme(context, themeForProject(testInfo));
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.clock.setFixedTime(FROZEN_NOW_MS);
    await routeAllApis(page);
  });

  for (const surface of SURFACES) {
    for (const bp of BREAKPOINT_NAMES) {
      test(`${surface.name} @ ${bp} (${BREAKPOINTS[bp].width}px)`, async ({ page }) => {
        await page.setViewportSize(BREAKPOINTS[bp]);
        await page.goto(surface.path);
        await surface.ready(page);
        // No font swap mid-capture.
        await page.evaluate(async () => {
          await document.fonts.ready;
        });
        await expect(page).toHaveScreenshot(`${surface.name}-${bp}.png`, { fullPage: true });
      });
    }
  }
});
