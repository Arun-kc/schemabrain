/**
 * Policy editor E2E smoke (/policy).
 *
 * The editable policy surface (ADR 0007): a category block panel + a
 * per-column override list on the left, a server-rendered schemabrain.yaml
 * panel + staged diff on the right. The catastrophic floor is locked in
 * both. Apply is read-only (copy YAML + reveal command, ADR 0006).
 *
 * The category list is static (the 12-value enum), so "block contact" is
 * present regardless of which source is indexed — the interaction test
 * anchors on it for determinism.
 *
 * Prerequisite: a dashboard sidecar running on http://127.0.0.1:7878 with
 * at least one indexed source. See web/tests/e2e/README.md.
 */

import { expect, test } from "@playwright/test";
import { pinTheme, themeForProject } from "./theme";

test.beforeEach(async ({ context }, testInfo) => {
  await pinTheme(context, themeForProject(testInfo));
});

test.describe("Policy editor E2E smoke", () => {
  test("renders the two levers + yaml + diff panels", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));

    await page.goto("/policy");

    await expect(page.getByRole("heading", { name: "Policy", exact: true })).toBeVisible();
    await expect(page.getByLabel("category block set")).toBeVisible();
    await expect(page.getByLabel("column overrides")).toBeVisible();
    await expect(page.getByLabel("generated policy yaml")).toBeVisible();
    await expect(page.getByLabel("staged changes")).toBeVisible();

    // The server-rendered YAML always carries the version line.
    await expect(page.getByLabel("generated policy yaml").getByText("version: 1")).toBeVisible();

    // The catastrophic floor renders locked (alarm + lock affordance)
    // in the category panel — exposed to assistive tech as "locked
    // floor". Runs under both themes via the project matrix.
    await expect(page.getByLabel("locked floor").first()).toBeVisible();

    await page.screenshot({ path: "test-results/08-policy.png", fullPage: true });
    expect(errors, `console pageerror events: ${errors.join("; ")}`).toEqual([]);
  });

  test("staging a category block reveals the diff + read-only apply", async ({ page }) => {
    await page.goto("/policy");

    // At rest there are no staged changes.
    await expect(page.getByLabel("staged changes").getByText(/0 staged/)).toBeVisible();

    // Block a non-floor category (always present — static enum).
    await page.getByRole("button", { name: "block contact" }).click();

    // The diff pane updates and the read-only Apply control appears.
    await expect(page.getByLabel("staged changes").getByText(/1 staged/)).toBeVisible();
    await expect(page.getByRole("button", { name: "Apply policy" })).toBeVisible();

    // Discard reverts back to the clean baseline.
    await page.getByRole("button", { name: "Discard" }).click();
    await expect(page.getByLabel("staged changes").getByText(/0 staged/)).toBeVisible();
  });
});
