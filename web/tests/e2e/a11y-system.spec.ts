/**
 * System responsive + accessibility pass (wsSHELL-responsive-a11y-system, PR-21).
 *
 * Cross-cutting shell guarantees that aren't tied to one surface: the skip link,
 * keyboard reachability of the nav rail + source selector, ≥44px nav targets, no
 * horizontal overflow across the four standard breakpoints, and the app-wide
 * reduced-motion contract (the per-surface axe checks live in each surface spec;
 * the graph's reduced-motion park lives in graph.spec.ts).
 *
 * Runs under both themes (chromium-dark / chromium-light). Prerequisite: a
 * dashboard sidecar on http://127.0.0.1:7878 with at least one indexed source.
 * See web/tests/e2e/README.md.
 */

import { expect, test } from "@playwright/test";
import { BREAKPOINTS, BREAKPOINT_NAMES } from "./viewports";
import { pinTheme, themeForProject } from "./theme";

/** WCAG-aligned minimum target size the nav rail is held to. */
const MIN_TARGET_PX = 44;

/** Computed transition-duration of `locator`, in milliseconds. */
async function transitionMs(page: import("@playwright/test").Page, selector: string): Promise<number> {
  return page.locator(selector).first().evaluate((el) => {
    const d = getComputedStyle(el).transitionDuration.trim();
    return d.endsWith("ms") ? parseFloat(d) : parseFloat(d) * 1000;
  });
}

test.beforeEach(async ({ context }, testInfo) => {
  await pinTheme(context, themeForProject(testInfo));
});

test.describe("System responsive + a11y", () => {
  test("the skip link is the first focusable, hidden until focused, and targets main", async ({ page }) => {
    await page.goto("/overview");

    const skip = page.getByRole("link", { name: /skip to content/i });
    // Present in the DOM but parked off-screen until focused.
    await expect(skip).not.toBeInViewport();

    // The very first Tab lands on it.
    await page.keyboard.press("Tab");
    await expect(skip).toBeFocused();
    await expect(skip).toBeInViewport();

    // Activating it jumps to the <main id="sb-main"> content landmark.
    await skip.press("Enter");
    await expect(page).toHaveURL(/#sb-main$/);
    await expect(page.locator("main#sb-main")).toBeVisible();
  });

  test("the nav rail and source selector are keyboard reachable", async ({ page }) => {
    await page.goto("/overview");

    // Scope to the rail — the overview hero also links to the graph ("Open
    // knowledge graph"), which would otherwise match by substring.
    const nav = page.getByRole("navigation", { name: "Dashboard sections" });
    const navGraph = nav.getByRole("link", { name: "Knowledge graph" });
    await navGraph.focus();
    await expect(navGraph).toBeFocused();

    const source = page.getByRole("button", { name: /active source:/i });
    await source.focus();
    await expect(source).toBeFocused();
  });

  test("every nav target is at least 44px tall", async ({ page }) => {
    await page.goto("/overview");

    const items = page.locator(".sb-nav-item");
    const count = await items.count();
    expect(count).toBeGreaterThan(0);

    for (let i = 0; i < count; i += 1) {
      const box = await items.nth(i).boundingBox();
      expect(box, `nav item ${i} has no bounding box`).not.toBeNull();
      expect(
        box!.height,
        `nav item ${i} height ${box!.height}px is below the ${MIN_TARGET_PX}px target`,
      ).toBeGreaterThanOrEqual(MIN_TARGET_PX);
    }
  });

  for (const name of BREAKPOINT_NAMES) {
    test(`no horizontal overflow at ${name} (${BREAKPOINTS[name].width}px)`, async ({ page }) => {
      await page.setViewportSize(BREAKPOINTS[name]);
      await page.goto("/overview");

      await expect(
        page.getByRole("heading", { name: /Agents are reasoning over a protected graph/ }),
      ).toBeVisible();

      // The document must not scroll horizontally (1px tolerance for sub-pixel
      // rounding). A wider scrollWidth than clientWidth means something overflowed.
      const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
      );
      expect(overflow, `horizontal overflow of ${overflow}px`).toBeLessThanOrEqual(1);
    });
  }

  test("reduced motion collapses transitions app-wide", async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto("/overview");

    // The global `prefers-reduced-motion` rule zeroes every transition (and the
    // drilldown sheet's slide with it), so motion snaps instead of animating.
    const navMs = await transitionMs(page, ".sb-nav-item");
    expect(navMs, `nav transition ${navMs}ms should be ~0 under reduced motion`).toBeLessThan(50);
  });
});
