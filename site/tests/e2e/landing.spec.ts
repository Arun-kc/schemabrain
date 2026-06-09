/**
 * Marketing landing smoke.
 *
 * Asserts the single-scroll page renders its load-bearing sections, the hero
 * carries the canonical tagline phrase, the theme toggle flips <html>, and no
 * runtime error fires. The page is static and backend-free — there is no
 * sidecar/API dependency.
 */

import { expect, test } from "@playwright/test";

test("landing renders all sections with no page errors", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));

  await page.goto("/");

  // Hero H1 carries the canonical positioning phrase (sourced from TAGLINE).
  await expect(page.getByRole("heading", { level: 1 })).toContainText(
    /trust and intelligence layer/i,
  );

  // The five content sections' load-bearing headings.
  await expect(
    page.getByRole("heading", { name: /It understands your database deeply/i }),
  ).toBeVisible();
  await expect(page.getByRole("heading", { name: /discover → describe → compute/i })).toBeVisible();
  await expect(
    page.getByRole("heading", { name: /Trust you can show your security team/i }),
  ).toBeVisible();
  await expect(page.getByRole("heading", { name: /Running in one command/i })).toBeVisible();

  // Firewall is present but framed as one proof-point of six, never the headline.
  await expect(page.getByText("SQL firewall")).toBeVisible();
  await expect(page.getByText(/the floor, not the headline/i)).toBeVisible();

  expect(errors, `console pageerror events: ${errors.join("; ")}`).toEqual([]);
});

test("theme toggle flips the document theme", async ({ page }) => {
  await page.goto("/");

  const html = page.locator("html");
  // Dark-first marketing site.
  await expect(html).toHaveAttribute("data-theme", "dark");

  await page.getByRole("button", { name: /toggle color theme/i }).click();
  await expect(html).not.toHaveAttribute("data-theme", "dark");

  await page.getByRole("button", { name: /toggle color theme/i }).click();
  await expect(html).toHaveAttribute("data-theme", "dark");
});
