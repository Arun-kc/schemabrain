/**
 * Overview surface E2E smoke (/overview) — the dashboard home.
 *
 * Matches the design handoff (app/overview.jsx), honestly adapted: a
 * system-status hero + a 6-card bento where every card links into its
 * detail surface. Every figure is read from GET /api/overview; this spec
 * injects that one route via `page.route` (keeping /api/meta live so the
 * shell resolves a source) and asserts the hero + each card's link target
 * and headline number. Prerequisite: a dashboard sidecar on
 * http://127.0.0.1:7878 with at least one indexed source.
 */

import { expect, test } from "@playwright/test";
import { pinTheme, themeForProject } from "./theme";

test.beforeEach(async ({ context }, testInfo) => {
  await pinTheme(context, themeForProject(testInfo));
});

const OVERVIEW = {
  source_connection_id: "ov-smoke",
  source: { label: "ov-smoke", tables: 12, last_indexed_at: 1_700_000_500 },
  entities: {
    count: 7,
    catastrophic_count: 3,
    by_group: { identity: 3, billing: 2, activity: 2, other: 0 },
    confidence: { high: 4, medium: 1, low: 0, unrated: 2 },
  },
  protection: { catastrophic: 3, policy_blocked: 5, allowed: 20, columns: 28 },
  freshness: {
    fresh: false,
    change_count: 2,
    risk: { high: 1, med: 1, low: 0 },
    last_enriched: 1_700_000_000,
  },
  refusals: {
    count_24h: 4,
    recent: [
      { tool: "get_metric", category: "contact", occurred_at: 1_700_000_400 },
      { tool: "query_rows", category: "government_id", occurred_at: 1_700_000_300 },
    ],
  },
  audit: {
    total: 1234,
    recent: [
      { tool: "query_rows", seq: 1234, refused: true, summary: "pii_blocked", occurred_at: 1_700_000_400 },
      { tool: "describe_entity", seq: 1233, refused: false, summary: "ok", occurred_at: 1_700_000_300 },
    ],
  },
  semantic: {
    metrics_count: 6,
    joins_count: 9,
    mined_count: 2,
    metric_names: ["active_users", "mrr_cents"],
  },
};

test("overview renders the hero and the 6 bento cards with live links", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));

  await page.route("**/api/overview*", (route) =>
    route.fulfill({ json: OVERVIEW }),
  );

  await page.goto("/overview");

  // hero
  await expect(
    page.getByRole("heading", { name: /Agents are reasoning over a protected graph/ }),
  ).toBeVisible();
  await expect(page.getByRole("link", { name: /Open knowledge graph/ })).toHaveAttribute(
    "href",
    "/graph",
  );

  // each card is a link into its surface (scope text to the card link)
  const protection = page.getByRole("link", { name: /Protection posture/ });
  await expect(protection).toHaveAttribute("href", "/pii");
  await expect(protection.getByText(/catastrophic columns hard-blocked/)).toBeVisible();
  await expect(protection.getByText(/Catastrophic/)).toBeVisible();

  const freshness = page.getByRole("link", { name: /Freshness/ });
  await expect(freshness).toHaveAttribute("href", "/drift");
  // honest pill reflects real drift state (fresh:false → Stale)
  await expect(freshness.getByText("Stale", { exact: true })).toBeVisible();

  // fuller name disambiguates the card from the nav-rail "Refusals" link
  const refusals = page.getByRole("link", { name: /Refusals · last 24h/ });
  await expect(refusals).toHaveAttribute("href", "/refusals");
  await expect(refusals.getByText(/agent attempts blocked today/)).toBeVisible();

  const audit = page.getByRole("link", { name: /Audit trail/ });
  await expect(audit).toHaveAttribute("href", "/audit");
  await expect(audit.getByText("Hash chain intact")).toBeVisible();
  await expect(audit.getByText(/1,234/)).toBeVisible();

  const entities = page.getByRole("link", { name: /Bound entities/ });
  await expect(entities).toHaveAttribute("href", "/entities");
  await expect(entities.getByText(/entities across/)).toBeVisible();

  const semantic = page.getByRole("link", { name: /Semantic layer/ });
  await expect(semantic).toHaveAttribute("href", "/graph");
  await expect(semantic.getByText("active_users")).toBeVisible();

  expect(errors, `console pageerror events: ${errors.join("; ")}`).toEqual([]);
});

test("overview shows honest fallbacks when values are absent", async ({ page }) => {
  await page.route("**/api/overview*", (route) =>
    route.fulfill({
      json: {
        ...OVERVIEW,
        source: { label: "ov-smoke", tables: null, last_indexed_at: null },
        freshness: { fresh: true, change_count: 0, risk: { high: 0, med: 0, low: 0 }, last_enriched: null },
        entities: { ...OVERVIEW.entities, confidence: { high: 0, medium: 0, low: 0, unrated: 7 } },
        semantic: { ...OVERVIEW.semantic, mined_count: 0 },
      },
    }),
  );

  await page.goto("/overview");

  // null last_enriched → "—", never a fabricated 0/epoch
  await expect(page.getByText(/last enriched · —/)).toBeVisible();
  // all-unrated confidence → honest note, not a fake meter
  await expect(page.getByText(/no model rating/)).toBeVisible();
  // real fresh state (scope to the freshness card; "Fresh" alone matches
  // "Freshness" + "Context is fresh" too)
  const freshness = page.getByRole("link", { name: /Freshness/ });
  await expect(freshness.getByText("Fresh", { exact: true })).toBeVisible();
  await expect(freshness.getByText(/Context is fresh/)).toBeVisible();
  // 0 mined joins → honest "all declared" copy, not a fabricated count
  await expect(page.getByText(/Every join is a declared foreign key/)).toBeVisible();
});
