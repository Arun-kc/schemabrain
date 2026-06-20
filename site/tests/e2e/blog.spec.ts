/**
 * Blog smoke.
 *
 * Covers the two new routes (/blog index + /blog/[slug] article) and the
 * landing → blog link. The article assertions double as a regression guard for
 * the PR #256 honesty corrections: the corrected "cosine" + "directly measured"
 * language must be present, and the removed "BM25" / "no data egress" overclaims
 * must never reappear in published copy.
 */

import { expect, test } from "@playwright/test";

test("blog index lists the founder post", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));

  await page.goto("/blog");

  await expect(page.getByRole("heading", { level: 1 })).toContainText(
    /Notes from the trust boundary/i,
  );
  await expect(page.getByRole("heading", { name: "Why I built SchemaBrain" })).toBeVisible();

  expect(errors, `console pageerror events: ${errors.join("; ")}`).toEqual([]);
});

test("founder post renders with the corrected, honest claims", async ({ page }) => {
  await page.goto("/blog/why-i-built-schemabrain");

  await expect(page.getByRole("heading", { level: 1 })).toContainText(/Why I built SchemaBrain/i);

  const body = page.locator(".bl-prose");
  // Corrected retrieval + cost language is present...
  await expect(body).toContainText(/semantic cosine retrieval/i);
  await expect(body).toContainText(/directly measured reference/i);
  // ...and the overclaims PR #256 removed never reappear in published copy.
  await expect(body).not.toContainText(/BM25/i);
  await expect(body).not.toContainText(/no data egress/i);
});

test("blog is reachable from the landing nav", async ({ page }) => {
  await page.goto("/");

  // The desktop nav exposes a Blog link (footer carries a second one for mobile,
  // so scope to the nav to keep the locator unambiguous).
  await page.getByRole("navigation").getByRole("link", { name: "Blog" }).click();

  await expect(page).toHaveURL(/\/blog$/);
  await expect(page.getByRole("heading", { name: "Why I built SchemaBrain" })).toBeVisible();
});
