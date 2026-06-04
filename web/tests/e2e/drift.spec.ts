/**
 * Drift surface E2E smoke (/drift).
 *
 * Matches the design handoff (app/drift.jsx), honestly adapted: a
 * fresh/stale hero, a kind filter (Config / Enrichment only — the
 * store-only sidecar can't verify live schema/definition drift), and
 * risk-badged change cards. Every action copies a CLI command (ADR 0006),
 * so the sidecar stays GET-only.
 *
 * Drift state is injected via `page.route` on /api/drift only; /api/meta
 * stays live so the shell resolves a source. Prerequisite: a dashboard
 * sidecar on http://127.0.0.1:7878 with at least one indexed source.
 * See web/tests/e2e/README.md.
 */

import { expect, type Page, test } from "@playwright/test";
import { pinTheme, themeForProject } from "./theme";

test.beforeEach(async ({ context }, testInfo) => {
  await pinTheme(context, themeForProject(testInfo));
});

const FRESHNESS_FRESH = {
  fresh: true,
  change_count: 0,
  high_risk: 0,
  last_enriched: 1_700_000_000,
  last_indexed: 1_700_000_500,
  source_label: "drift-smoke",
  prompt_version: "2026-05-11-1",
};

const CONFIG_ITEM = {
  kind: "config",
  risk: "med",
  title: "Policy file changed since serve started",
  when: "2026-06-04T10:00:00+00:00",
  detail: "pii_policy.yaml was edited after `schemabrain serve` recorded it.",
  action: { label: "Copy restart command", command: "schemabrain serve" },
};

const ENRICHMENT_ITEM = {
  kind: "enrichment",
  risk: "med",
  title: "AI context built on an older prompt version",
  when: null,
  detail: "Some column descriptions predate the live prompt version (2026-05-11-1).",
  action: { label: "Copy re-enrich command", command: "schemabrain index <source> --enrich" },
};

const STALE = {
  source_connection_id: "drift-smoke",
  freshness: { ...FRESHNESS_FRESH, fresh: false, change_count: 2 },
  items: [CONFIG_ITEM, ENRICHMENT_ITEM],
};

const FRESH = {
  source_connection_id: "drift-smoke",
  freshness: FRESHNESS_FRESH,
  items: [],
};

async function routeDrift(page: Page, json: unknown): Promise<void> {
  await page.route(
    (url) => url.pathname === "/api/drift",
    (route) => route.fulfill({ json }),
  );
}

/** Collect any non-GET /api request — the read-only invariant guard. */
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

test.describe("Drift surface E2E smoke", () => {
  test("stale: hero + risk cards render, actions are copy-only (GET-only)", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));
    const writes = trackWrites(page);

    await routeDrift(page, STALE);
    await page.goto("/drift");

    await expect(page.getByRole("heading", { name: "Drift", exact: true })).toBeVisible();

    // Stale hero with the honest change counts.
    const hero = page.getByLabel("Drift status");
    await expect(hero.getByText("AI context may be stale")).toBeVisible();
    await expect(page.getByText("2 changes", { exact: true })).toBeVisible();

    // Both honest kinds render as cards.
    const list = page.getByLabel("Drift changes");
    await expect(list.getByText("Policy file changed since serve started")).toBeVisible();
    await expect(list.getByText("AI context built on an older prompt version")).toBeVisible();

    // Hero CTA copies the primary (re-enrich) remedy; card actions copy too —
    // none of them write.
    await hero.getByRole("button", { name: "Copy re-enrich command" }).click();
    await list.getByRole("button", { name: "Copy restart command" }).click();

    await page.screenshot({ path: "test-results/09-drift.png", fullPage: true });
    expect(writes, `unexpected non-GET /api calls: ${writes.join("; ")}`).toEqual([]);
    expect(errors, `console pageerror events: ${errors.join("; ")}`).toEqual([]);
  });

  test("fresh: green hero + designed empty state, no cards", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));

    await routeDrift(page, FRESH);
    await page.goto("/drift");

    await expect(page.getByLabel("Drift status").getByText("Context is fresh")).toBeVisible();
    await expect(page.getByRole("heading", { name: "No drift detected", level: 3 })).toBeVisible();
    // No change list when fresh.
    await expect(page.getByLabel("Drift changes")).toHaveCount(0);

    expect(errors, `console pageerror events: ${errors.join("; ")}`).toEqual([]);
  });

  test("kind filter narrows the list to one kind", async ({ page }) => {
    await routeDrift(page, STALE);
    await page.goto("/drift");

    const filter = page.getByRole("group", { name: "Filter drift by kind" });
    const list = page.getByLabel("Drift changes");

    await filter.getByRole("button", { name: "Config" }).click();
    await expect(list.getByText("Policy file changed since serve started")).toBeVisible();
    await expect(list.getByText("AI context built on an older prompt version")).toHaveCount(0);
    await expect(page.getByText("1 change", { exact: true })).toBeVisible();

    await filter.getByRole("button", { name: "Enrichment" }).click();
    await expect(list.getByText("AI context built on an older prompt version")).toBeVisible();
    await expect(list.getByText("Policy file changed since serve started")).toHaveCount(0);
  });

  test("renders without overflow at mobile width", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));

    await page.setViewportSize({ width: 375, height: 800 });
    await routeDrift(page, STALE);
    await page.goto("/drift");

    await expect(page.getByLabel("Drift status")).toBeVisible();
    await expect(
      page.getByLabel("Drift changes").getByText("Policy file changed since serve started"),
    ).toBeVisible();
    expect(errors, `console pageerror events: ${errors.join("; ")}`).toEqual([]);
  });
});
