/**
 * PolicyView E2E smoke.
 *
 * The Ledger (Matrix) and PolicyView share the /pii surface. This
 * spec covers the PolicyView pane only — the Matrix is exercised by
 * smoke.spec.ts's `PII ledger renders the stat slab + entity matrix`
 * test. Splitting the policy assertions out keeps each spec focused
 * on one component's render contract.
 *
 * Prerequisite: a dashboard sidecar running on http://127.0.0.1:7878
 * with at least one indexed source containing PII-tagged columns
 * (the same fixture the Matrix smoke depends on satisfies this).
 *
 * See web/tests/e2e/README.md for the canonical setup flow.
 */

import { expect, test } from "@playwright/test";

test.beforeEach(async ({ context }) => {
  await context.addInitScript(() => {
    try {
      window.localStorage.setItem("schemabrain.theme", "dark");
    } catch {
      // localStorage can throw in private-mode-style sandboxes; the
      // theme just falls back to the page default.
    }
  });
});

test.describe("PolicyView E2E smoke", () => {
  test("policy view renders active block + per-column verdict table", async ({
    page,
  }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));

    await page.goto("/pii");

    // The PolicyView title strip — distinct from the Matrix's
    // "THE LEDGER" so a substring match isolates this pane.
    await expect(page.getByText(/Active Policy/i)).toBeVisible();

    // The "active block" panel label always renders, regardless of
    // whether a YAML file is present or only the catastrophic floor
    // is active.
    await expect(page.getByText("active block", { exact: true })).toBeVisible();

    // Posture preview panel — the diff table label sits below the
    // active block. "posture preview" is unique to this surface.
    await expect(page.getByText("posture preview", { exact: true })).toBeVisible();

    // Per-column verdict heading — the table that lists each tagged
    // column with its computed enforcement verdict.
    await expect(page.getByText(/Per-column verdict/i)).toBeVisible();

    // Catastrophic floor chips always render with the three default
    // categories. `credential` is the most stable string to anchor
    // on (one word, no underscore-to-space transform).
    await expect(page.getByText(/credential/i).first()).toBeVisible();

    await page.screenshot({ path: "test-results/05-policy.png", fullPage: true });
    expect(errors, `console pageerror events: ${errors.join("; ")}`).toEqual([]);
  });

  test("policy view exposes verdict + origin filters", async ({ page }) => {
    await page.goto("/pii");

    // Both filter dropdowns must render; tests assert their presence
    // via accessible labels rather than depending on the order they
    // appear in the DOM.
    await expect(page.getByLabel("filter by verdict")).toBeVisible();
    await expect(page.getByLabel("filter by origin")).toBeVisible();
  });
});
