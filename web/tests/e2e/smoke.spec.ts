/**
 * Dashboard end-to-end smoke.
 *
 * The smallest valuable browser-driven regression net for the core dashboard
 * surfaces (root→overview redirect + PII + Refusals + Audit). Each spec navigates,
 * asserts the surface's load-bearing landmark, then captures a screenshot
 * for visual review in `playwright-report/`.
 *
 * Prerequisite: a dashboard sidecar must already be running on
 * http://127.0.0.1:7878 against a store with:
 *   - at least one indexed source
 *   - at least one refused audit row (so the Refusals ledger renders
 *     a held row, not the empty state)
 *
 * See `web/tests/e2e/README.md` for the canonical setup flow.
 */

import { expect, test } from "@playwright/test";
import { pinTheme, themeForProject } from "./theme";

// Pin the theme via localStorage before the first Next.js hydration runs,
// derived from the active project (chromium-dark / chromium-light) so the
// same content assertions run under both themes without per-spec branching.
test.beforeEach(async ({ context }, testInfo) => {
  await pinTheme(context, themeForProject(testInfo));
});

test.describe("dashboard E2E smoke", () => {
  test("root redirects to the overview surface", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));

    // `/` is a redirect stub now — the marketing landing moved to the
    // separate `site/` app, and the dashboard home is /overview.
    await page.goto("/");

    await expect(page).toHaveURL(/\/overview\/?$/);
    await expect(
      page.getByRole("heading", { name: /Agents are reasoning over a protected graph/ }),
    ).toBeVisible();

    await page.screenshot({ path: "test-results/01-overview.png", fullPage: true });
    expect(errors, `console pageerror events: ${errors.join("; ")}`).toEqual([]);
  });

  test("PII matrix renders the confidence heatmap", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));

    await page.goto("/pii");

    // The surface title (a heading, so it doesn't collide with the nav link
    // of the same name).
    await expect(page.getByRole("heading", { name: /PII matrix/i })).toBeVisible();
    // The honest 3-level band legend is always rendered — the heatmap's
    // load-bearing "advisory, never gates" framing.
    await expect(page.getByText(/never gates enforcement/i)).toBeVisible();

    // The heatmap must list at least one classified column from the fixture.
    // Anchor on the qualified table name (`public.users`) inside the matrix
    // label so the assertion stays scoped to the grid; `.first()` because a
    // column-row layout repeats the table name per column.
    await expect(
      page.getByLabel("pii matrix").getByText("public.users").first(),
    ).toBeVisible();

    await page.screenshot({ path: "test-results/02-pii.png", fullPage: true });
    expect(errors, `console pageerror events: ${errors.join("; ")}`).toEqual([]);
  });

  test("refusals timeline renders the protective ledger with a held row", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));

    await page.goto("/refusals");

    // The surface title (a heading, so it doesn't collide with the nav link
    // of the same name) + the protective lede the ledger is framed around.
    await expect(page.getByRole("heading", { name: /Refusals/i })).toBeVisible();
    await expect(page.getByText(/held at the boundary/i)).toBeVisible();

    // A seeded refused row renders inside the ledger as a Sensitive-data
    // bucket row terminating in a green "held" status — proof the
    // row → API → render chain worked. Scoped to the ledger list so the
    // assertion stays anchored to a real row, not chrome.
    const ledger = page.getByLabel("refusals ledger");
    await expect(ledger.getByText("Sensitive-data").first()).toBeVisible();
    await expect(ledger.getByText("held", { exact: true }).first()).toBeVisible();

    await page.screenshot({ path: "test-results/03-refusals.png", fullPage: true });
    expect(errors, `console pageerror events: ${errors.join("; ")}`).toEqual([]);
  });

  test("audit ledger renders the merkle root + verifies inclusion proofs", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));

    await page.goto("/audit");

    // The surface title (a heading, so it doesn't collide with the nav link
    // of the same name) + the honest lede the surface is framed around.
    await expect(page.getByRole("heading", { name: /Audit/i })).toBeVisible();
    await expect(page.getByText(/commits to the hash of the row before it/i)).toBeVisible();
    // The derived Merkle root is the spine of the surface.
    await expect(page.getByText(/merkle root/i)).toBeVisible();

    // A seeded row renders inside the audit list (scoped to the list so the
    // assertion stays anchored to a real row, not chrome).
    const ledger = page.getByLabel("audit rows");
    await expect(ledger.getByText("list_entities()")).toBeVisible();

    // The real verification: clicking Verify recomputes each row's RFC-6962
    // inclusion proof in the browser (crypto.subtle) and reconciles it to the
    // one global root — proof the merkle route → fetch → verify chain worked.
    await page.getByRole("button", { name: /verify against root/i }).click();
    await expect(page.getByText(/verified · \d+\/\d+ intact/i).first()).toBeVisible({
      timeout: 15000,
    });

    await page.screenshot({ path: "test-results/04-audit.png", fullPage: true });
    expect(errors, `console pageerror events: ${errors.join("; ")}`).toEqual([]);
  });

  test("source id auto-resolves in the shell source selector", async ({ page }) => {
    // The dashboard pages never hardcode a source_connection_id; they
    // call /api/meta and read `default_source_connection_id`. With the
    // app shell, the resolved id surfaces in the top-bar source selector
    // (the per-surface HeaderStrip is retired) — its accessible name is
    // `active source: <seeded 'demo-source' or hashed production id>`.
    await page.goto("/pii");
    await expect(
      page.getByRole("button", { name: /active source: (demo-source|[0-9a-f]{16})/i }),
    ).toBeVisible();
  });
});

