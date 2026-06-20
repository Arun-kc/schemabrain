/**
 * Responsive smoke.
 *
 * Locks in the mobile-nav fix and guards against horizontal-overflow
 * regressions. Before the fix the shared nav floored the page at ~490px wide,
 * so every route scrolled sideways at 320/375 and the GitHub / Get-started
 * actions (and all section links) were unreachable on a phone.
 *
 * Two guarantees:
 *  1. No route scrolls horizontally at common phone widths, in either theme.
 *  2. The hamburger reveals a panel that carries every link/action the desktop
 *     bar shows — and it actually navigates.
 */

import { expect, test, type Page } from "@playwright/test";

const PHONE_WIDTHS = [320, 375] as const;
const ROUTES = ["/", "/blog", "/blog/why-i-built-schemabrain"] as const;

async function horizontalOverflowPx(page: Page): Promise<number> {
  return page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
}

for (const width of PHONE_WIDTHS) {
  for (const route of ROUTES) {
    test(`no horizontal overflow at ${width}px on ${route}`, async ({ page }) => {
      await page.setViewportSize({ width, height: 800 });
      await page.goto(route);
      // ≤1px tolerance for sub-pixel rounding; anything more is a real sideways scroll.
      expect(await horizontalOverflowPx(page)).toBeLessThanOrEqual(1);
    });
  }
}

test("mobile hamburger opens the panel and navigates", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 800 });
  await page.goto("/");

  // The desktop section links are collapsed; the bar exposes a hamburger.
  const burger = page.getByRole("button", { name: /open menu/i });
  await expect(burger).toBeVisible();

  // Panel links are out of the a11y tree until opened.
  const nav = page.getByRole("navigation");
  await expect(nav.getByRole("link", { name: "Intelligence" })).toBeHidden();

  await burger.click();
  await expect(nav.getByRole("link", { name: "Intelligence" })).toBeVisible();

  // The panel carries the actions the desktop bar would show.
  await expect(nav.getByRole("link", { name: /get started/i })).toBeVisible();

  // ...and a panel link actually routes.
  await nav.getByRole("link", { name: "Blog" }).click();
  await expect(page).toHaveURL(/\/blog$/);
});

test("desktop keeps inline links and hides the hamburger", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.goto("/");

  const nav = page.getByRole("navigation");
  // Inline section links are present; the hamburger is not rendered for desktop.
  await expect(nav.getByRole("link", { name: "How it works" })).toBeVisible();
  await expect(page.getByRole("button", { name: /open menu/i })).toBeHidden();
});
