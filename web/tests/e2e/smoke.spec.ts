/**
 * Dashboard end-to-end smoke.
 *
 * The smallest valuable browser-driven regression net for the 4 dashboard
 * surfaces (landing + PII + Refusals + Audit). Each spec navigates,
 * asserts the surface's load-bearing landmark, then captures a screenshot
 * for visual review in `playwright-report/`.
 *
 * Prerequisite: a dashboard sidecar must already be running on
 * http://127.0.0.1:7878 against a store with:
 *   - at least one indexed source
 *   - at least one refused audit row (so the Refusal surface renders
 *     a populated incident card, not the empty state)
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
  test("landing page renders the three surface cards", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));

    await page.goto("/");

    await expect(
      page.getByRole("heading", { name: /What the SQL firewall sees/i }),
    ).toBeVisible();

    // Three surface cards — each is the entry point to one M1 page.
    await expect(page.getByText("PII Visualization", { exact: false })).toBeVisible();
    await expect(page.getByText("Refusal Experience", { exact: false })).toBeVisible();
    await expect(page.getByText("Audit Viewer", { exact: false })).toBeVisible();

    // Active-pipeline telemetry section is the hero composition the
    // landing page is built around; missing it means the layout
    // collapsed.
    await expect(page.getByText(/Active Pipeline Telemetry/i)).toBeVisible();

    await page.screenshot({ path: "test-results/01-landing.png", fullPage: true });
    expect(errors, `console pageerror events: ${errors.join("; ")}`).toEqual([]);
  });

  test("PII ledger renders the stat slab + entity matrix", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));

    await page.goto("/pii");

    await expect(page.getByText(/THE LEDGER/i)).toBeVisible();
    // Slab label is exact-case "catastrophic exposure" (lowercase).
    // The title-meta string "no catastrophic exposure" appears on a
    // safe store and would shadow a case-insensitive match; an
    // exact-case match isolates the slab block reliably either way.
    await expect(page.getByText("catastrophic exposure", { exact: true })).toBeVisible();

    // The matrix must list at least one entity from the fixture.
    // Anchor on the qualified table name (`public.users`) inside the
    // matrix label so the assertion stays scoped to the Ledger pane
    // even after PolicyView (which also renders the qualified-column
    // strings in its rollup samples + per-column rows) lands on the
    // same surface. Without the scope, strict-mode fires.
    await expect(
      page.getByLabel("pii ledger matrix").getByText("public.users"),
    ).toBeVisible();

    await page.screenshot({ path: "test-results/02-pii.png", fullPage: true });
    expect(errors, `console pageerror events: ${errors.join("; ")}`).toEqual([]);
  });

  test("refusal experience renders the populated incident detail", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));

    await page.goto("/refusals");

    await expect(page.getByText(/Active Threat Telemetry Feed/i)).toBeVisible();
    // With a seeded refused row, the detail pane shows the tool name.
    await expect(page.getByText(/Refusal Event:/i)).toBeVisible();
    // pii_blocked is the refusal_reason every catastrophic-PII refusal
    // carries — its presence in the envelope properties is the
    // crispest signal that the row → API → render chain worked.
    await expect(page.getByText(/pii_blocked/i).first()).toBeVisible();

    await page.screenshot({ path: "test-results/03-refusals.png", fullPage: true });
    expect(errors, `console pageerror events: ${errors.join("; ")}`).toEqual([]);
  });

  test("audit viewer renders the ledger chain + block detail", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));

    await page.goto("/audit");

    await expect(page.getByText(/Ledger Chain Intact/i)).toBeVisible();
    // The block-detail right pane shows when a row is selected; the
    // first row should be auto-selected on load. "BLOCK PAYLOAD"
    // headers the JSON pane that is the audit chain's tamper-evident
    // payload.
    await expect(page.getByText(/BLOCK PAYLOAD/i)).toBeVisible();

    await page.screenshot({ path: "test-results/04-audit.png", fullPage: true });
    expect(errors, `console pageerror events: ${errors.join("; ")}`).toEqual([]);
  });

  test("source id auto-resolves in the header strip", async ({ page }) => {
    // The /api/meta fix from PR #126 means the dashboard pages never
    // hardcode a source_connection_id; they call /api/meta and read
    // `default_source_connection_id`. The header strip surfaces the
    // resolved id.
    await page.goto("/pii");
    // The header strip's source-id pill displays either the seeded 'demo-source' or the hashed production ID.
    await expect(page.getByText(/demo-source|[0-9a-f]{16}/i).first()).toBeVisible();
  });
});

