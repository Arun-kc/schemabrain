/**
 * Policy editor E2E smoke (/policy).
 *
 * Matches the design handoff: a single per-column block/redact/allow grid
 * (ADR 0008) beside a server-rendered, syntax-highlighted schemabrain.yaml
 * panel + staged diff. The 3-way control is an ARIA radiogroup; floor
 * columns render locked. Apply is read-only (copy YAML + reveal command,
 * ADR 0006) — the sidecar stays GET-only.
 *
 * The interaction tests target the first non-floor row's segments
 * generically (aria-label `<verb> <qualified_column>`), so they don't
 * depend on the seeded source's exact column names.
 *
 * Prerequisite: a dashboard sidecar on http://127.0.0.1:7878 with at least
 * one indexed source. See web/tests/e2e/README.md.
 */

import { expect, test } from "@playwright/test";
import { pinTheme, themeForProject } from "./theme";

test.beforeEach(async ({ context }, testInfo) => {
  await pinTheme(context, themeForProject(testInfo));
});

test.describe("Policy editor E2E smoke", () => {
  test("renders the per-column grid + yaml + diff panels", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));

    await page.goto("/policy");

    await expect(page.getByRole("heading", { name: "Policy", exact: true })).toBeVisible();
    await expect(page.getByLabel("per-column enforcement")).toBeVisible();
    await expect(page.getByLabel("generated policy yaml")).toBeVisible();
    await expect(page.getByLabel("staged changes")).toBeVisible();

    // The server-rendered YAML carries the canonical version key.
    await expect(page.getByLabel("generated policy yaml").getByText(/version/).first()).toBeVisible();

    // The catastrophic floor renders locked, exposed once as "locked floor".
    await expect(page.getByLabel("locked floor").first()).toBeVisible();

    await page.screenshot({ path: "test-results/08-policy.png", fullPage: true });
    expect(errors, `console pageerror events: ${errors.join("; ")}`).toEqual([]);
  });

  test("staging via the radiogroup reveals the diff + read-only apply, GET-only", async ({
    page,
  }) => {
    // Guard the read-only invariant: NO non-GET request to /api during the
    // whole interaction (Apply must copy + reveal a command, never write).
    const writes: string[] = [];
    page.on("request", (req) => {
      const url = req.url();
      if (url.includes("/api/") && !["GET", "HEAD", "OPTIONS"].includes(req.method())) {
        writes.push(`${req.method()} ${url}`);
      }
    });

    await page.goto("/policy");
    const staged = page.getByLabel("staged changes");
    const grid = page.getByLabel("per-column enforcement");
    await expect(staged.getByText("0 staged")).toBeVisible();

    // Block the first non-floor column (segments are role=radio).
    const blockRadio = grid.getByRole("radio", { name: /^block / }).first();
    await blockRadio.click();
    await expect(blockRadio).toHaveAttribute("aria-checked", "true");
    await expect(staged.getByText("1 staged")).toBeVisible();

    // Apply is read-only: reveals the CLI command, never writes.
    const apply = page.getByRole("button", { name: /^Apply policy/ });
    await expect(apply).toBeVisible();
    await apply.click();
    await expect(page.getByText("Run to apply")).toBeVisible();
    await expect(page.getByText(/schemabrain policy apply/)).toBeVisible();

    // Discard reverts to the clean baseline.
    await page.getByRole("button", { name: "Discard" }).click();
    await expect(staged.getByText("0 staged")).toBeVisible();

    expect(writes, `unexpected non-GET /api calls: ${writes.join("; ")}`).toEqual([]);
  });

  test("allow is mutually exclusive with block in the radiogroup", async ({ page }) => {
    await page.goto("/policy");
    const grid = page.getByLabel("per-column enforcement");

    const allowRadio = grid.getByRole("radio", { name: /^allow / }).first();
    await allowRadio.click();
    await expect(allowRadio).toHaveAttribute("aria-checked", "true");
    // selecting allow unchecks block on the same row
    await expect(grid.getByRole("radio", { name: /^block / }).first()).toHaveAttribute(
      "aria-checked",
      "false",
    );
  });

  test("arrow keys move selection AND focus (roving tabindex)", async ({ page }) => {
    await page.goto("/policy");
    const grid = page.getByLabel("per-column enforcement");

    const blockRadio = grid.getByRole("radio", { name: /^block / }).first();
    const colName = (await blockRadio.getAttribute("aria-label"))!.replace(/^block /, "");
    await blockRadio.click();
    await expect(blockRadio).toBeFocused();

    // ArrowRight selects redact and focus follows it.
    await page.keyboard.press("ArrowRight");
    const redactRadio = grid.getByRole("radio", { name: `redact ${colName}` });
    await expect(redactRadio).toHaveAttribute("aria-checked", "true");
    await expect(redactRadio).toBeFocused();
  });
});
