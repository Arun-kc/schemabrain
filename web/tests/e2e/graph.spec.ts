/**
 * Knowledge-graph surface E2E smoke (/graph) — PR-17a canvas + PR-17b chrome.
 *
 * The signature surface reproduced on reactflow: kit GraphNode/GraphEdge
 * primitives, a deterministic generated layout, and the restore-to-seed force
 * sim driven by a PARKED rAF loop (it runs only while a drag has energy, then
 * stops). Selection is URL-driven (`?entity=`) and opens the shell's global
 * drilldown sheet. PR-17b adds the floating chrome (search, the three
 * data-backed overlays, legend, path chip, MiniMap/Controls, tooltip), all
 * exercised in the second describe block below.
 *
 * Graph state is injected via `page.route` on /api/graph only; /api/meta stays
 * live so the shell resolves a source. Prerequisite: a dashboard sidecar on
 * http://127.0.0.1:7878 serving an export that includes the /graph route, with
 * at least one indexed source. See web/tests/e2e/README.md.
 *
 * Two PR-17a-specific guards live here (NOT deferred to the QA pass): the
 * parked-sim frame count proves the rAF loop runs AND stops, and the
 * reduced-motion case proves the loop never starts at all.
 */

import { expect, type Page, test } from "@playwright/test";
import { pinTheme, themeForProject } from "./theme";

test.beforeEach(async ({ context }, testInfo) => {
  await pinTheme(context, themeForProject(testInfo));
});

// A tiny but representative projection: a 3-entity canonical path
// (user → order → plan), one catastrophic node, two declared FK edges on the
// path, and one off-path log-mined edge (dashed, no cardinality).
const GRAPH = {
  source_connection_id: "graph-smoke",
  nodes: [
    {
      id: "user",
      label: "user",
      group: "identity",
      pii_level: "catastrophic",
      row_count: 1200,
      refusal_count: 2,
    },
    {
      id: "order",
      label: "order",
      group: "activity",
      pii_level: "none",
      row_count: 9000,
      refusal_count: 0,
    },
    {
      id: "plan",
      label: "plan",
      group: "billing",
      pii_level: "pii",
      row_count: 12,
      refusal_count: 0,
    },
  ],
  edges: [
    {
      id: "user_order",
      source: "user",
      target: "order",
      evidence: "declared",
      canonical_path_rank: 1,
      cardinality: "one_to_many",
    },
    {
      id: "order_plan",
      source: "order",
      target: "plan",
      evidence: "declared",
      canonical_path_rank: 1,
      cardinality: "many_to_one",
    },
    {
      id: "user_plan_mined",
      source: "user",
      target: "plan",
      evidence: "log_mined",
      canonical_path_rank: 0,
      cardinality: null,
    },
  ],
  canonical_path: {
    nodes: ["user", "order", "plan"],
    edges: ["user_order", "order_plan"],
    hops: 2,
  },
  unattributed_refusals: 1,
};

async function routeGraph(page: Page, json: unknown): Promise<void> {
  await page.route(
    (url) => url.pathname === "/api/graph",
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

/** Drag a node by its reactflow data-id, a fixed offset, in small steps. */
async function dragNode(page: Page, id: string, dx: number, dy: number): Promise<void> {
  const node = page.locator(`.react-flow__node[data-id="${id}"]`);
  await node.scrollIntoViewIfNeeded();
  const box = await node.boundingBox();
  if (!box) throw new Error(`node ${id} has no bounding box`);
  const cx = box.x + box.width / 2;
  const cy = box.y + box.height / 2;
  await page.mouse.move(cx, cy);
  await page.mouse.down();
  await page.mouse.move(cx + dx, cy + dy, { steps: 8 });
  await page.mouse.up();
}

test.describe("Knowledge graph surface E2E smoke", () => {
  test("renders entity nodes + relationship edges, read-only (GET-only)", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));
    const writes = trackWrites(page);

    await routeGraph(page, GRAPH);
    await page.goto("/graph");

    const region = page.getByRole("region", { name: "Knowledge graph" });
    await expect(region).toBeVisible();

    // Every entity renders a node. Scope to the reactflow node element — the
    // names also appear in the canonical-path chip, so an unscoped getByText
    // is ambiguous now that PR-17b draws the path hops.
    for (const id of ["user", "order", "plan"]) {
      await expect(page.locator(`.react-flow__node[data-id="${id}"]`)).toContainText(id);
    }
    // Exact match — the legend's "Catastrophic PII" row would otherwise
    // collide with the node's uppercase stamp under substring matching.
    await expect(region.getByText("CATASTROPHIC", { exact: true })).toBeVisible();

    // All three relationships render as edges.
    await expect(page.locator(".react-flow__edge")).toHaveCount(3);

    await page.screenshot({ path: "test-results/10-graph.png", fullPage: true });
    expect(writes, `unexpected non-GET /api calls: ${writes.join("; ")}`).toEqual([]);
    expect(errors, `console pageerror events: ${errors.join("; ")}`).toEqual([]);
  });

  test("clicking a node opens the entity drilldown via ?entity=", async ({ page }) => {
    const writes = trackWrites(page);
    await routeGraph(page, GRAPH);
    await page.goto("/graph");

    await page.locator('.react-flow__node[data-id="user"]').click();

    await expect(page).toHaveURL(/[?&]entity=user\b/);
    await expect(page.getByRole("dialog", { name: "Entity user" })).toBeVisible();

    // Opening the drilldown is a URL change, never a write.
    expect(writes, `unexpected non-GET /api calls: ${writes.join("; ")}`).toEqual([]);
  });

  test("parked sim: the rAF loop runs on drag, then stops at rest", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));

    await routeGraph(page, GRAPH);
    await page.goto("/graph");

    const region = page.getByRole("region", { name: "Knowledge graph" });
    await expect(region).toBeVisible();
    // Idle on load — the layout is pre-settled, so the loop hasn't run.
    await expect(region).toHaveAttribute("data-graph-loop", "parked");
    await expect(region).toHaveAttribute("data-graph-frames", "0");

    await dragNode(page, "order", 90, 50);

    // The loop ran (frames > 0) and parked again (the spring restored to seed).
    await expect(region).toHaveAttribute("data-graph-loop", "parked");
    const settled = Number(await region.getAttribute("data-graph-frames"));
    expect(settled).toBeGreaterThan(0);

    // And it STAYS parked — no perpetual rAF burning the main thread.
    await page.waitForTimeout(400);
    const after = Number(await region.getAttribute("data-graph-frames"));
    expect(after).toBe(settled);

    expect(errors, `console pageerror events: ${errors.join("; ")}`).toEqual([]);
  });

  test("reduced motion: the force loop never starts", async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await routeGraph(page, GRAPH);
    await page.goto("/graph");

    const region = page.getByRole("region", { name: "Knowledge graph" });
    await expect(region).toBeVisible();
    await expect(region).toHaveAttribute("data-reduced-motion", "true");

    // Even a drag must not kick the rAF loop under reduced motion.
    await dragNode(page, "order", 90, 50);
    await page.waitForTimeout(300);
    await expect(region).toHaveAttribute("data-graph-frames", "0");
    await expect(region).toHaveAttribute("data-graph-loop", "parked");
  });

  test("renders without overflow at mobile width", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));

    await page.setViewportSize({ width: 375, height: 800 });
    await routeGraph(page, GRAPH);
    await page.goto("/graph");

    await expect(page.getByRole("region", { name: "Knowledge graph" })).toBeVisible();
    // Scope to the node element — `user` also appears in the path chip.
    await expect(page.locator('.react-flow__node[data-id="user"]')).toBeVisible();
    expect(errors, `console pageerror events: ${errors.join("; ")}`).toEqual([]);
  });
});

test.describe("Knowledge graph panels + overlays (PR-17b)", () => {
  test("renders the floating chrome: search, overlays, legend, path chip", async ({ page }) => {
    await routeGraph(page, GRAPH);
    await page.goto("/graph");

    await expect(page.getByRole("searchbox", { name: /search entities/i })).toBeVisible();
    await expect(page.getByRole("button", { name: "PII heat" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Refusal hotspots" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Log-mined joins" })).toBeVisible();
    await expect(page.getByRole("button", { name: /copy re-index command/i })).toBeVisible();
    // Path chip renders the hop count straight off the wire (2 hops).
    await expect(page.getByText("Canonical path · 2 hops")).toBeVisible();
    // reactflow MiniMap + Controls present.
    await expect(page.locator(".react-flow__minimap")).toBeVisible();
    await expect(page.locator(".react-flow__controls")).toBeVisible();
  });

  test("the refusal overlay lights the attributed hotspot + reconciles the unattributed remainder", async ({
    page,
  }) => {
    const writes = trackWrites(page);
    await routeGraph(page, GRAPH);
    await page.goto("/graph");

    // Off by default — no badge.
    await expect(page.locator('[data-refusal-badge="true"]')).toHaveCount(0);

    await page.getByRole("button", { name: "Refusal hotspots" }).click();

    // `user` carries 2 attributed refusals → its badge shows the real count.
    const badge = page.locator('.react-flow__node[data-id="user"] [data-refusal-badge="true"]');
    await expect(badge).toBeVisible();
    await expect(badge).toHaveText("2");
    // The honest remainder is surfaced so badges + figure reconcile.
    await expect(page.getByText(/1 refusal not attributed to a visible entity/i)).toBeVisible();

    // Toggling an overlay is pure client state — never a write.
    expect(writes, `unexpected non-GET /api calls: ${writes.join("; ")}`).toEqual([]);
  });

  test("the PII-heat overlay halos PII-bearing entities only", async ({ page }) => {
    await routeGraph(page, GRAPH);
    await page.goto("/graph");

    await expect(page.locator('[data-pii-halo="true"]')).toHaveCount(0);
    await page.getByRole("button", { name: "PII heat" }).click();
    // user (catastrophic) + plan (pii) light; order (none) does not.
    await expect(page.locator('[data-pii-halo="true"]')).toHaveCount(2);
  });

  test("search dims the non-matching nodes", async ({ page }) => {
    await routeGraph(page, GRAPH);
    await page.goto("/graph");

    await page.getByRole("searchbox", { name: /search entities/i }).fill("plan");
    // The matched node stays full opacity; a miss fades back.
    await expect(page.locator('.react-flow__node[data-id="plan"]')).toHaveCSS("opacity", "1");
    await expect(page.locator('.react-flow__node[data-id="user"]')).toHaveCSS("opacity", "0.32");
  });

  test("hovering a node shows its tooltip with row count + PII summary", async ({ page }) => {
    await routeGraph(page, GRAPH);
    await page.goto("/graph");

    await page.locator('.react-flow__node[data-id="order"]').hover();
    const tip = page.getByRole("tooltip");
    await expect(tip).toBeVisible();
    await expect(tip).toContainText("order");
    await expect(tip).toContainText("9,000 rows · no PII");
  });

  test("a source reporting 'indexing' shows the genuine state-driven overlay (no crash)", async ({
    page,
  }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));

    // Override /api/meta to report a mid-index source — the SourceState the
    // sidecar reserves but does not emit yet. Proves the overlay is genuinely
    // state-driven (not a fabricated animation) and that the indexing branch
    // renders without a Rules-of-Hooks crash.
    await page.route(
      (url) => url.pathname === "/api/meta",
      (route) =>
        route.fulfill({
          json: {
            charter_version: "1.2",
            dashboard_schema_version: "1.8",
            fingerprint_version: "fp-v1",
            store_path: "/tmp/x",
            default_source_connection_id: "idx-src",
            source_connection_ids: ["idx-src"],
            sources: [
              {
                source_id: "idx-src",
                engine: "postgres",
                state: "indexing",
                last_indexed_at: null,
                tables: 3,
                entities: 0,
              },
            ],
          },
        }),
    );
    await routeGraph(page, GRAPH);
    await page.goto("/graph");

    // Scope to the indexing overlay's own status region — the shell mounts a
    // global clipboard toast that also carries role="status".
    const overlay = page.getByRole("status").filter({ hasText: /Indexing/i });
    await expect(overlay).toBeVisible();
    await expect(overlay).toContainText(/Indexing/i);
    // No fabricated percentage in the honest indexing copy.
    await expect(overlay).not.toContainText("%");
    expect(errors, `console pageerror events: ${errors.join("; ")}`).toEqual([]);
  });
});

test.describe("Knowledge graph polish (PR-17c)", () => {
  test("draws compact cardinality labels on the canonical-path edges (G1)", async ({ page }) => {
    await routeGraph(page, GRAPH);
    await page.goto("/graph");

    // The two path edges (rank 1) are highlighted on load → labelled with their
    // declared cardinality. user→order is 1:N, order→plan is N:1.
    const edges = page.locator(".react-flow__edges");
    await expect(edges).toContainText("1:N");
    await expect(edges).toContainText("N:1");
  });

  test("labels a log-mined edge 'mined' under the overlay, never a fake cardinality (G1)", async ({
    page,
  }) => {
    await routeGraph(page, GRAPH);
    await page.goto("/graph");

    const edges = page.locator(".react-flow__edges");
    await expect(edges).not.toContainText("mined");
    await page.getByRole("button", { name: "Log-mined joins" }).click();
    // The single off-path mined edge (cardinality null) reads "mined".
    await expect(edges).toContainText("mined");
  });

  test("renders directional arrowhead markers on the edges (G2)", async ({ page }) => {
    await routeGraph(page, GRAPH);
    await page.goto("/graph");

    await expect(page.getByRole("region", { name: "Knowledge graph" })).toBeVisible();
    // reactflow injects one <marker class="react-flow__arrowhead"> per distinct
    // arrowhead config; our edges request green (path) + ink (rest).
    expect(await page.locator("marker.react-flow__arrowhead").count()).toBeGreaterThan(0);
  });

  test("brightens edges incident to the selected node (G3)", async ({ page }) => {
    await routeGraph(page, GRAPH);
    await page.goto("/graph");

    // The off-path mined edge rests at 0.5 opacity. reactflow tags the edge
    // group with `data-testid` (not `data-id`, which only nodes carry).
    const minedPath = page.locator(
      '[data-testid="rf__edge-user_plan_mined"] .react-flow__edge-path',
    );
    await expect(minedPath).toHaveCSS("opacity", "0.5");

    // Selecting `plan` makes user→plan incident → brightened to 0.95.
    await page.locator('.react-flow__node[data-id="plan"]').click();
    await expect(page).toHaveURL(/[?&]entity=plan\b/);
    await expect(minedPath).toHaveCSS("opacity", "0.95");
  });

  test("shows the refusal pulse-ring on the attributed hotspot (G4)", async ({ page }) => {
    await routeGraph(page, GRAPH);
    await page.goto("/graph");

    const pulse = page.locator('.react-flow__node[data-id="user"] [data-refusal-pulse="true"]');
    await expect(pulse).toHaveCount(0);

    await page.getByRole("button", { name: "Refusal hotspots" }).click();
    // `user` carries 2 attributed refusals → the pulse ring accompanies its badge.
    await expect(pulse).toBeVisible();
  });
});
