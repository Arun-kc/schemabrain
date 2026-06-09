/**
 * Data dictionary surface E2E smoke (/dict) — PR-21 (wsQA-e2e-entities-dict-drift).
 *
 * The generated, browsable dictionary: a grouped + searchable left index drives
 * a right entry panel (description, PII chips, columns with meaning, semantic
 * joins with the server-rendered ON clause, and the metrics each entity anchors).
 * "Export Markdown" re-renders the WHOLE dictionary client-side and copies it to
 * the clipboard — byte-for-byte equal to the `schemabrain docs` CLI golden (that
 * parity is unit-tested in lib/dict/serialize.test.ts; here we assert the copy
 * fires and the surface stays read-only).
 *
 * `/api/dict` is injected via `page.route`; `/api/meta` stays live so the shell
 * resolves a source. Prerequisite: a dashboard sidecar on http://127.0.0.1:7878
 * serving an export that includes the /dict route, with at least one indexed
 * source. See web/tests/e2e/README.md.
 */

import { expect, type Page, test } from "@playwright/test";
import { expectNoSeriousA11yViolations } from "./a11y";
import { pinTheme, themeForProject } from "./theme";

test.beforeEach(async ({ context }, testInfo) => {
  await pinTheme(context, themeForProject(testInfo));
});

// Two entities across two groups (billing/order first → the default entry;
// identity/user second) so we can exercise group ordering, selection switching,
// and search. `order` carries a payment_card column (catastrophic floor) plus a
// semantic join and an anchored metric; `user` carries a credential column.
const DICT = {
  schema_version: "17",
  sources: [
    {
      source_connection_id: "dict-smoke",
      entities: [
        {
          name: "order",
          description: "A purchase by a user.",
          qualified_table: "public.orders",
          identity: "id",
          group: "billing",
          columns: [
            { name: "id", data_type: "integer", nullable: false, is_primary_key: false, is_identity: true, description: null, pii_sensitivity: "public", pii_categories: [] },
            { name: "card_pan", data_type: "text", nullable: true, is_primary_key: false, is_identity: false, description: "raw card number", pii_sensitivity: "pii", pii_categories: ["payment_card"] },
            { name: "amount_cents", data_type: "integer", nullable: false, is_primary_key: false, is_identity: false, description: null, pii_sensitivity: "public", pii_categories: [] },
          ],
          joins: [
            { name: "user_orders", description: "Links user accounts to their placed orders.", source_entity: "user", target_entity: "order", on_clause: '"user"."id" = "order"."user_id"', cardinality: "one_to_many", provenance: "Operator-authored" },
          ],
          metrics: [
            { name: "total_revenue", description: "Sum of order purchase values (stored in cents).", agg: "sum", measure: "amount_cents", measure_is_expression: false, time_dimension: null, time_grains: [] },
          ],
        },
        {
          name: "user",
          description: "A registered account.",
          qualified_table: "public.users",
          identity: "id",
          group: "identity",
          columns: [
            { name: "id", data_type: "integer", nullable: false, is_primary_key: false, is_identity: true, description: null, pii_sensitivity: "public", pii_categories: [] },
            { name: "email", data_type: "text", nullable: false, is_primary_key: false, is_identity: false, description: "primary contact email", pii_sensitivity: "pii", pii_categories: ["contact"] },
            { name: "password_hash", data_type: "text", nullable: false, is_primary_key: false, is_identity: false, description: null, pii_sensitivity: "pii", pii_categories: ["credential"] },
          ],
          joins: [],
          metrics: [],
        },
      ],
    },
  ],
};

async function routeDict(page: Page): Promise<void> {
  await page.route(
    (url) => url.pathname === "/api/dict",
    (route) => route.fulfill({ json: DICT }),
  );
}

function trackWrites(page: Page): string[] {
  const writes: string[] = [];
  page.on("request", (req) => {
    const url = req.url();
    if (url.includes("/api/") && !["GET", "HEAD", "OPTIONS"].includes(req.method())) {
      writes.push(`${req.method()} ${url}`);
    }
  });
  return writes;
}

test.describe("Data dictionary surface E2E smoke", () => {
  test("renders the index + default entry with columns, join ON clause, and metric (read-only)", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));
    const writes = trackWrites(page);

    await routeDict(page);
    await page.goto("/dict");

    await expect(page.getByRole("heading", { name: "Data dictionary", level: 1 })).toBeVisible();

    // Left index lists both entities (grouped + searchable).
    const index = page.getByLabel("Entity index");
    await expect(index.getByRole("button", { name: "order" })).toBeVisible();
    await expect(index.getByRole("button", { name: "user" })).toBeVisible();

    // The default entry is the first entity overall (order).
    const entry = page.getByLabel("order dictionary entry");
    await expect(entry.getByRole("heading", { name: "order", level: 2 })).toBeVisible();
    await expect(entry.getByText("public.orders · identity id")).toBeVisible();
    // The catastrophic-floor PII column surfaces its category chip.
    await expect(entry.getByText("Payment Card").first()).toBeVisible();
    // The semantic join renders the server-rendered ON clause verbatim.
    await expect(entry.getByText('"user"."id" = "order"."user_id"')).toBeVisible();
    // The anchored metric.
    await expect(entry.getByText("total_revenue")).toBeVisible();

    await page.screenshot({ path: "test-results/19-dict.png", fullPage: true });
    expect(writes, `unexpected non-GET /api calls: ${writes.join("; ")}`).toEqual([]);
    expect(errors, `console pageerror events: ${errors.join("; ")}`).toEqual([]);
  });

  test("selecting an entity in the index switches the entry", async ({ page }) => {
    await routeDict(page);
    await page.goto("/dict");

    await page.getByLabel("Entity index").getByRole("button", { name: "user" }).click();

    const entry = page.getByLabel("user dictionary entry");
    await expect(entry.getByRole("heading", { name: "user", level: 2 })).toBeVisible();
    await expect(entry.getByText("public.users · identity id")).toBeVisible();
    // user's credential column flag.
    await expect(entry.getByText("Credential").first()).toBeVisible();
  });

  test("search filters the index to matching entities", async ({ page }) => {
    await routeDict(page);
    await page.goto("/dict");

    const index = page.getByLabel("Entity index");
    await index.getByRole("searchbox", { name: "Filter entities by name" }).fill("ord");
    await expect(index.getByRole("button", { name: "order" })).toBeVisible();
    await expect(index.getByRole("button", { name: "user" })).toHaveCount(0);
  });

  test("Export Markdown copies the whole dictionary, read-only", async ({ page }) => {
    const writes = trackWrites(page);

    // Headless Chromium can't reach the real clipboard deterministically, so we
    // stub navigator.clipboard.writeText to record the copied payload on window.
    await page.addInitScript(() => {
      const copied: string[] = [];
      (window as unknown as { __copied: string[] }).__copied = copied;
      // navigator.clipboard is a read-only accessor, so Object.assign can't
      // override it — defineProperty shadows it on the instance.
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: {
          writeText: (text: string) => {
            copied.push(text);
            return Promise.resolve();
          },
        },
      });
    });

    await routeDict(page);
    await page.goto("/dict");

    await page.getByRole("button", { name: /export markdown/i }).click();
    await expect(page.getByRole("button", { name: /copied markdown/i })).toBeVisible();

    const copied = await page.evaluate(
      () => (window as unknown as { __copied?: string[] }).__copied?.[0] ?? "",
    );
    expect(copied).toContain("# Data dictionary");
    expect(copied).toContain("order");

    expect(writes, `unexpected non-GET /api calls: ${writes.join("; ")}`).toEqual([]);
  });

  test("has no serious or critical accessibility violations", async ({ page }) => {
    await routeDict(page);
    await page.goto("/dict");

    await expect(page.getByRole("heading", { name: "Data dictionary", level: 1 })).toBeVisible();
    await expectNoSeriousA11yViolations(page);
  });

  test("renders without overflow at mobile width", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));

    await page.setViewportSize({ width: 375, height: 800 });
    await routeDict(page);
    await page.goto("/dict");

    await expect(page.getByRole("heading", { name: "Data dictionary", level: 1 })).toBeVisible();
    expect(errors, `console pageerror events: ${errors.join("; ")}`).toEqual([]);
  });
});
