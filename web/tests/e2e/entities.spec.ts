/**
 * Entities surface E2E smoke (/entities) — PR-18.
 *
 * The sortable table companion to the knowledge graph + the structured entity
 * drilldown body that mounts into the shell's `?entity=` slide-over. Selecting a
 * row opens the same shared sheet the graph drives; the sheet's footer deep-links
 * to the PII matrix with a `?focus=` highlight.
 *
 * `/api/entities`, `/api/entities/{name}/columns`, and `/api/entities/pii-matrix`
 * are injected via `page.route`; `/api/meta` stays live so the shell resolves a
 * source. Prerequisite: a dashboard sidecar on http://127.0.0.1:7878 serving an
 * export that includes the /entities + /pii routes, with at least one indexed
 * source. See web/tests/e2e/README.md.
 */

import { expect, type Page, test } from "@playwright/test";
import { pinTheme, themeForProject } from "./theme";

test.beforeEach(async ({ context }, testInfo) => {
  await pinTheme(context, themeForProject(testInfo));
});

const ENTITIES = {
  source_connection_id: "ent-smoke",
  count: 3,
  items: [
    { name: "user", description: "", qualified_table: "public.users", identity: "id", group: "identity", origin: "suggested", inference_method: "llm_suggested", validation_state: "applied", rows: 1_200_000, confidence: "high", pii_level: "catastrophic", metrics_count: 2, joins_count: 3 },
    { name: "plan", description: "", qualified_table: "public.plans", identity: "id", group: "billing", origin: "manual", inference_method: "manually_authored", validation_state: "confirmed", rows: 42, confidence: null, pii_level: "none", metrics_count: 0, joins_count: 1 },
    { name: "session", description: "", qualified_table: "public.sessions", identity: "id", group: "activity", origin: "suggested", inference_method: "observed_in_query_log", validation_state: "applied", rows: 94_000_000, confidence: "low", pii_level: "internal", metrics_count: 1, joins_count: 2 },
  ],
  summary: { entities: 3, metrics: 3, joins: 6, catastrophic_entities: 1 },
};

const USER_COLUMNS = {
  entity: { name: "user", description: "", qualified_table: "public.users", identity: "id", group: "identity", origin: "suggested", inference_method: "llm_suggested", validation_state: "applied", rows: 1_200_000, confidence: "high", rationale: "Bound from the primary key and inbound foreign keys." },
  columns: [
    { name: "id", data_type: "uuid", description: "surrogate key", sensitivity: "public", pii_categories: [], similar: [] },
    { name: "email", data_type: "text", description: "primary contact email", sensitivity: "pii", pii_categories: ["contact"], similar: [{ schema: "public", table: "invoice", column: "billing_email", score: 0.94 }] },
    { name: "password_hash", data_type: "text", description: "argon2id hash", sensitivity: "pii", pii_categories: ["credential"], similar: [] },
  ],
  metrics: [{ name: "active_users", description: "distinct active users", measure: { agg: "count_distinct", column: "id", expression: null }, time_grains: ["day"] }],
  joins: [{ name: "subscription_user", description: "a subscription belongs to a user", source_entity: "subscription", target_entity: "user", on_clause: '"subscription"."user_id" = "user"."id"', cardinality: "many_to_one", origin: "suggested", inference_method: "fk_constraint", is_log_mined: false }],
  referenced_by: { metrics: 1, joins: 1 },
};

const PII_MATRIX = {
  source_connection_id: "ent-smoke",
  entities: [{ name: "user", qualified_table: "public.users", identity: "id", origin: "suggested", inference_method: "llm_suggested", validation_state: "applied", counts: { contact: 1 }, catastrophic_column_count: 0, has_catastrophic: false }],
  columns: [{ entity: "user", qualified_table: "public.users", column_name: "email", sensitivity: "pii", categories: ["contact"], pii_confidence: "high" }],
  categories: ["contact", "financial", "payment_card", "health", "genetic", "biometric", "behavioral", "online_identifier", "credential", "government_id", "location", "demographic_protected"],
  catastrophic_categories: ["credential", "payment_card", "government_id"],
  totals: { entities: 1, columns: 1, catastrophic_columns: 0, pii_columns: 1, confidential_columns: 0, internal_or_public_columns: 0 },
};

async function routeEntities(page: Page): Promise<void> {
  await page.route((url) => url.pathname === "/api/entities", (route) => route.fulfill({ json: ENTITIES }));
  await page.route((url) => url.pathname === "/api/entities/user/columns", (route) => route.fulfill({ json: USER_COLUMNS }));
  await page.route((url) => url.pathname === "/api/entities/pii-matrix", (route) => route.fulfill({ json: PII_MATRIX }));
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

test.describe("Entities surface E2E smoke", () => {
  test("renders the table, stats, and sortable headers, read-only (GET-only)", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));
    const writes = trackWrites(page);

    await routeEntities(page);
    await page.goto("/entities");

    await expect(page.getByRole("heading", { name: "Entities" })).toBeVisible();
    await expect(page.getByText("1 on the catastrophic floor")).toBeVisible();
    // A row per entity; default sort is rows-descending (largest first).
    const firstRow = page.locator("table tbody tr").first();
    await expect(firstRow).toContainText("session");
    await expect(page.getByRole("columnheader", { name: /Rows/ })).toHaveAttribute("aria-sort", "descending");

    await page.screenshot({ path: "test-results/18-entities.png", fullPage: true });
    expect(writes, `unexpected non-GET /api calls: ${writes.join("; ")}`).toEqual([]);
    expect(errors, `console pageerror events: ${errors.join("; ")}`).toEqual([]);
  });

  test("clicking a sortable header re-sorts and updates aria-sort", async ({ page }) => {
    await routeEntities(page);
    await page.goto("/entities");

    await page.getByRole("button", { name: "Entity" }).click();
    await expect(page.getByRole("columnheader", { name: /Entity/ })).toHaveAttribute("aria-sort", "descending");
    // Name descending → user first.
    await expect(page.locator("table tbody tr").first()).toContainText("user");
  });

  test("selecting a row opens the entity drilldown with physical + semantic detail", async ({ page }) => {
    const writes = trackWrites(page);
    await routeEntities(page);
    await page.goto("/entities");

    await page.getByRole("button", { name: "user" }).click();
    await expect(page).toHaveURL(/[?&]entity=user\b/);

    const sheet = page.getByRole("dialog", { name: "Entity user" });
    await expect(sheet).toBeVisible();
    // Rich header: categorical confidence pill (never a numeric %), rationale.
    await expect(sheet.getByText("High", { exact: true })).toBeVisible();
    await expect(sheet).not.toContainText("%");
    await expect(sheet.getByText(/primary key and inbound foreign keys/)).toBeVisible();
    // Physical pane: the catastrophic floor column carries the explicit suffix.
    await expect(sheet.getByText("CREDENTIAL · CATASTROPHIC")).toBeVisible();
    // Semantic pane: the server-rendered ON clause + the similar neighbour.
    await expect(sheet.getByText('ON "subscription"."user_id" = "user"."id"')).toBeVisible();
    await expect(sheet.getByText("public.invoice.billing_email")).toBeVisible();

    // Opening + drilling is URL/clipboard only — never a write.
    expect(writes, `unexpected non-GET /api calls: ${writes.join("; ")}`).toEqual([]);
  });

  test("the footer deep-links to the PII matrix and focuses the entity row", async ({ page }) => {
    await routeEntities(page);
    await page.goto("/entities?entity=user");

    const sheet = page.getByRole("dialog", { name: "Entity user" });
    await expect(sheet).toBeVisible();

    await sheet.getByRole("button", { name: /view in pii matrix/i }).click();
    await expect(page).toHaveURL(/\/pii\?focus=user\b/);
    // The matrix renders and the focused entity's column row is present.
    await expect(page.getByRole("table", { name: /pii matrix/i })).toBeVisible();
    await expect(page.getByText("email")).toBeVisible();
  });

  test("renders without overflow at mobile width", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));

    await page.setViewportSize({ width: 375, height: 800 });
    await routeEntities(page);
    await page.goto("/entities");

    await expect(page.getByRole("heading", { name: "Entities" })).toBeVisible();
    await expect(page.locator("table tbody tr").first()).toBeVisible();
    expect(errors, `console pageerror events: ${errors.join("; ")}`).toEqual([]);
  });
});
