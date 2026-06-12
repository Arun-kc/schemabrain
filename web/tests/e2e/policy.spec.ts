/**
 * Policy editor E2E smoke (/policy).
 *
 * Matches the design handoff scaled to a real multi-table schema: a
 * per-column block/redact/allow grid (ADR 0008) GROUPED into collapsible
 * tables, with a category summary strip + search/filter above it, beside a
 * server-rendered, syntax-highlighted schemabrain.yaml panel + staged diff.
 * The 3-way control is an ARIA radiogroup; floor columns render locked. Apply
 * is read-only (copy YAML + reveal command, ADR 0006) — the sidecar stays
 * GET-only.
 *
 * Groups are collapsed by default (the scaling win), so the interaction tests
 * expand them first. Selectors target ARIA roles/labels generically so they
 * don't depend on the seeded source's exact column names — except where a
 * specific seeded column (public.users.email) makes the assertion concrete.
 *
 * Prerequisite: a dashboard sidecar on http://127.0.0.1:7878 with at least
 * one indexed source (the demo seeds public.users + public.orders → 2 tables).
 * See web/tests/e2e/README.md.
 */

import { expect, type Page, test } from "@playwright/test";
import { expectNoSeriousA11yViolations } from "./a11y";
import { pinTheme, themeForProject } from "./theme";

test.beforeEach(async ({ context }, testInfo) => {
  await pinTheme(context, themeForProject(testInfo));
});

/** The per-column pane — scope group-header queries here so they don't catch
 * the app shell's own aria-expanded controls (e.g. the source selector). */
function gridPane(page: Page) {
  return page.getByLabel("per-column enforcement");
}

/** Expand every collapsed table group (group heads are the only buttons in the
 * pane that carry aria-expanded). Bounded loop — re-queries after each click. */
async function expandAllGroups(page: Page): Promise<void> {
  const pane = gridPane(page);
  // Wait for the pane (and thus the group heads) to render after the async
  // policy fetch — otherwise the loop sees 0 collapsed buttons and no-ops.
  await pane.waitFor({ state: "visible" });
  for (let i = 0; i < 50; i += 1) {
    const collapsed = pane.getByRole("button", { expanded: false });
    if ((await collapsed.count()) === 0) break;
    await collapsed.first().click();
  }
}

/** Collapse every expanded table group. */
async function collapseAllGroups(page: Page): Promise<void> {
  const pane = gridPane(page);
  for (let i = 0; i < 50; i += 1) {
    const open = pane.getByRole("button", { expanded: true });
    if ((await open.count()) === 0) break;
    await open.first().click();
  }
}

test.describe("Policy editor E2E smoke", () => {
  test("has no serious or critical accessibility violations", async ({ page }) => {
    await page.goto("/policy");

    await expect(page.getByRole("heading", { name: "Policy", exact: true })).toBeVisible();
    // Expand the groups so the per-column radiogroup controls are mounted and
    // included in the scan (the richest interactive DOM on this surface).
    await expandAllGroups(page);
    await expect(page.getByRole("radiogroup").first()).toBeVisible();
    await expectNoSeriousA11yViolations(page);
  });

  test("renders the grouped grid + category strip + yaml + diff panels", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));

    await page.goto("/policy");

    await expect(page.getByRole("heading", { name: "Policy", exact: true })).toBeVisible();
    await expect(page.getByLabel("per-column enforcement")).toBeVisible();
    await expect(page.getByLabel("category overview")).toBeVisible();
    await expect(page.getByLabel("generated policy yaml")).toBeVisible();
    await expect(page.getByLabel("staged changes")).toBeVisible();

    // The server-rendered YAML carries the canonical version key.
    await expect(page.getByLabel("generated policy yaml").getByText(/version/).first()).toBeVisible();

    // The always-on floor is disclosed on the panel (it isn't listed in block:).
    await expect(
      page.getByLabel("generated policy yaml").getByText(/always-on floor/),
    ).toBeVisible();

    // Collapse-by-default: with multiple tables, no row control is mounted yet.
    await expect(page.getByRole("radiogroup")).toHaveCount(0);
    // At least one collapsible table group header is present.
    expect(await gridPane(page).getByRole("button", { expanded: false }).count()).toBeGreaterThan(
      0,
    );

    // Expanding reveals the rows, including the catastrophic floor lock.
    await expandAllGroups(page);
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
    await expandAllGroups(page);
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
    await expandAllGroups(page);
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
    await expandAllGroups(page);
    const grid = page.getByLabel("per-column enforcement");

    const blockRadio = grid.getByRole("radio", { name: /^block / }).first();
    const colName = (await blockRadio.getAttribute("aria-label"))!.replace(/^block /, "");
    await blockRadio.click();
    await expect(blockRadio).toBeFocused();

    // ArrowRight selects the middle verb (shown as `open`) and focus follows it.
    await page.keyboard.press("ArrowRight");
    const openRadio = grid.getByRole("radio", { name: `open ${colName}` });
    await expect(openRadio).toHaveAttribute("aria-checked", "true");
    await expect(openRadio).toBeFocused();
  });

  test("collapse-by-default; expanding mounts rows, collapsing unmounts them", async ({ page }) => {
    await page.goto("/policy");
    await expect(page.getByLabel("per-column enforcement")).toBeVisible();

    // Nothing mounted while collapsed → light initial render at scale.
    await expect(page.getByRole("radiogroup")).toHaveCount(0);

    await expandAllGroups(page);
    expect(await page.getByRole("radiogroup").count()).toBeGreaterThan(0);

    await collapseAllGroups(page);
    await expect(page.getByRole("radiogroup")).toHaveCount(0);
  });

  test("search narrows to matches and auto-expands the matching group", async ({ page }) => {
    await page.goto("/policy");
    await page.getByLabel("search columns").fill("email");

    // The matching column's group auto-expands; its block radio is visible.
    await expect(page.getByRole("radio", { name: "block public.users.email" })).toBeVisible();
    // The non-matching table group is filtered out entirely.
    await expect(page.getByRole("button", { name: /public\.orders/ })).toHaveCount(0);
    // The live region announces the result count.
    await expect(page.getByText(/1 column · 1 table/)).toBeVisible();

    // Clearing the filter brings the other table back (collapsed again).
    await page.getByRole("button", { name: /clear/ }).click();
    expect(await page.getByRole("button", { name: /public\.orders/ }).count()).toBeGreaterThan(0);
  });

  test("category strip blocks a whole category in one click, GET-only", async ({ page }) => {
    const writes: string[] = [];
    page.on("request", (req) => {
      const url = req.url();
      if (url.includes("/api/") && !["GET", "HEAD", "OPTIONS"].includes(req.method())) {
        writes.push(`${req.method()} ${url}`);
      }
    });

    await page.goto("/policy");
    const strip = page.getByLabel("category overview");
    const staged = page.getByLabel("staged changes");
    await expect(staged.getByText("0 staged")).toBeVisible();

    // public.users.email is `contact` (non-floor) → the strip offers a toggle.
    const contactToggle = strip.getByRole("button", { name: "block all contact columns" });
    await contactToggle.click();
    await expect(contactToggle).toHaveAttribute("aria-pressed", "true");
    await expect(staged.getByText("1 staged")).toBeVisible();

    expect(writes, `unexpected non-GET /api calls: ${writes.join("; ")}`).toEqual([]);
  });

  test("hides non-PII columns by default; 'show all' reveals them", async ({ page }) => {
    const col = (qt: string, name: string, categories: string[]) => ({
      qualified_table: qt,
      column_name: name,
      qualified_column: `${qt}.${name}`,
      sensitivity: categories.length ? "pii" : "internal",
      categories,
      origin: "heuristic",
      effective_enforcement: "allowed",
    });
    const resp = {
      source_connection_id: "pii-default-smoke",
      policy_path: "./schemabrain/pii_policy.yaml",
      block_source: "default",
      active_block: [],
      catastrophic_floor: ["credential", "payment_card", "government_id"],
      effective_block_for_describe: ["credential", "payment_card", "government_id"],
      category_rollup: [],
      per_column: [
        col("public.users", "email", ["contact"]),
        col("public.users", "full_name", ["contact"]),
        col("public.users", "id", []),
        col("public.users", "created_at", []),
        col("public.users", "role", []),
        col("public.invoices", "id", []),
        col("public.invoices", "status", []),
        col("public.invoices", "total_cents", []),
      ],
      diff_preview: {
        current_blocked: 0,
        if_all_blocked: 0,
        if_catastrophic_only: 0,
        if_none_blocked: 0,
      },
      yaml_parse_error: null,
      policy_drift: { detected: false, recorded_at: null, current_mtime: null },
    };
    await page.route(
      (url) => url.pathname === "/api/pii/policy",
      (route) => route.fulfill({ json: resp }),
    );

    await page.goto("/policy");
    const pane = gridPane(page);
    const search = page.getByRole("search");

    // Default: only the PII-bearing table (users) shows; the all-non-PII table
    // (invoices) is hidden entirely.
    await expect(pane.getByRole("button", { name: /public\.users/ })).toBeVisible();
    await expect(pane.getByRole("button", { name: /public\.invoices/ })).toHaveCount(0);
    // The toggle advertises the hidden non-PII columns (6 of them).
    const showAll = search.getByRole("button", { name: /non-PII/ });
    await expect(showAll).toBeVisible();
    await expect(showAll).toHaveAttribute("aria-pressed", "false");

    // Reveal them — the non-PII table now appears.
    await showAll.click();
    await expect(pane.getByRole("button", { name: /public\.invoices/ })).toBeVisible();
    await expect(search.getByRole("button", { name: "PII only" })).toBeVisible();
  });

  test("scales to many tables: collapse keeps the initial render light", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));

    // Inject a large schema via the policy GET only (meta + preview stay live).
    const TABLES = 30;
    const COLS = 10;
    const cats = [
      "contact",
      "financial",
      "behavioral",
      "online_identifier",
      "location",
      "demographic_protected",
    ];
    const perColumn = [];
    for (let t = 0; t < TABLES; t += 1) {
      for (let c = 0; c < COLS; c += 1) {
        const qt = `public.t${t}`;
        const col = `c${c}`;
        perColumn.push({
          qualified_table: qt,
          column_name: col,
          qualified_column: `${qt}.${col}`,
          sensitivity: "pii",
          categories: [cats[(t + c) % cats.length]],
          origin: "heuristic",
          effective_enforcement: "allowed",
        });
      }
    }
    const big = {
      source_connection_id: "scale-smoke",
      policy_path: "./schemabrain/pii_policy.yaml",
      block_source: "default",
      active_block: [],
      catastrophic_floor: ["credential", "payment_card", "government_id"],
      effective_block_for_describe: ["credential", "payment_card", "government_id"],
      category_rollup: [],
      per_column: perColumn,
      diff_preview: {
        current_blocked: 0,
        if_all_blocked: 0,
        if_catastrophic_only: 0,
        if_none_blocked: 0,
      },
      yaml_parse_error: null,
      policy_drift: { detected: false, recorded_at: null, current_mtime: null },
    };

    await page.route(
      (url) => url.pathname === "/api/pii/policy",
      (route) => route.fulfill({ json: big }),
    );

    await page.goto("/policy");
    await expect(page.getByLabel("per-column enforcement")).toBeVisible();

    // 30 collapsed table headers, ZERO rows mounted → render stays cheap.
    await expect(gridPane(page).getByRole("button", { expanded: false })).toHaveCount(TABLES);
    await expect(page.getByRole("radiogroup")).toHaveCount(0);

    // Expanding one table mounts only that table's rows.
    await gridPane(page).getByRole("button", { expanded: false }).first().click();
    expect(await page.getByRole("radiogroup").count()).toBeGreaterThan(0);
    expect(await page.getByRole("radiogroup").count()).toBeLessThanOrEqual(COLS);

    expect(errors, `console pageerror events: ${errors.join("; ")}`).toEqual([]);
  });
});
