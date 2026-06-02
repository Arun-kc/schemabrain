/**
 * App-shell E2E smoke.
 *
 * Exercises the shared shell that wraps every app surface: nav rail
 * (active state), top-bar source selector, theme toggle (no remount),
 * the ⌘K command-palette stub, and the app-level entity drilldown sheet
 * keyed by `?entity=`.
 *
 * Prerequisite: a dashboard sidecar on http://127.0.0.1:7878 against a
 * store with at least one indexed source (the same fixture the other
 * specs use). See web/tests/e2e/README.md.
 */

import { expect, test } from "@playwright/test";
import { pinTheme, themeForProject } from "./theme";

test.beforeEach(async ({ context }, testInfo) => {
  await pinTheme(context, themeForProject(testInfo));
});

test.describe("app shell", () => {
  test("nav rail marks the active surface and the brand is present", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));

    await page.goto("/pii");

    await expect(page.getByRole("link", { name: "schemabrain home" })).toBeVisible();
    const piiLink = page.getByRole("link", { name: /PII matrix/ });
    await expect(piiLink).toHaveAttribute("aria-current", "page");

    // Unbuilt surfaces render disabled (no dead 404 link) until their PR.
    await expect(page.getByRole("link", { name: /Knowledge graph/ })).toHaveCount(0);

    expect(errors, `console pageerror events: ${errors.join("; ")}`).toEqual([]);
  });

  test("theme toggle flips the document theme in place", async ({ page }) => {
    await page.goto("/pii");
    const before = await page.evaluate(
      () => document.documentElement.getAttribute("data-theme") ?? "light",
    );
    await page.getByRole("button", { name: /switch to (dark|light) theme/i }).click();
    await expect
      .poll(() =>
        page.evaluate(() => document.documentElement.getAttribute("data-theme") ?? "light"),
      )
      .not.toBe(before);
  });

  test("source selector resolves the active source", async ({ page }) => {
    await page.goto("/audit");
    await expect(
      page.getByRole("button", { name: /active source: (demo-source|[0-9a-f]{16})/i }),
    ).toBeVisible();
  });

  test("command palette opens with ⌘K and closes with Escape", async ({ page }) => {
    await page.goto("/pii");
    await page.keyboard.press("Meta+k");
    const palette = page.getByRole("dialog", { name: /command palette/i });
    await expect(palette).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(palette).toHaveCount(0);
  });

  test("entity drilldown opens via ?entity= and closes", async ({ page }) => {
    await page.goto("/pii?entity=customers");
    const sheet = page.getByRole("dialog", { name: /entity customers/i });
    await expect(sheet).toBeVisible();
    await expect(sheet.getByRole("heading", { name: "customers" })).toBeVisible();

    await page.getByRole("button", { name: /close entity detail/i }).click();
    await expect(page).toHaveURL(/\/pii$/);
  });
});
