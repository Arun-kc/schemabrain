/**
 * Knowledge-graph surface E2E smoke (/graph) — PR-17a.
 *
 * The signature surface reproduced on reactflow: kit GraphNode/GraphEdge
 * primitives, a deterministic generated layout, and the restore-to-seed force
 * sim driven by a PARKED rAF loop (it runs only while a drag has energy, then
 * stops). Selection is URL-driven (`?entity=`) and opens the shell's global
 * drilldown sheet.
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
    { id: "user", label: "user", group: "identity", pii_level: "catastrophic", row_count: 1200 },
    { id: "order", label: "order", group: "activity", pii_level: "none", row_count: 9000 },
    { id: "plan", label: "plan", group: "billing", pii_level: "pii", row_count: 12 },
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

    // Every entity renders a node label; the catastrophic node is stamped.
    await expect(region.getByText("user", { exact: true })).toBeVisible();
    await expect(region.getByText("order", { exact: true })).toBeVisible();
    await expect(region.getByText("plan", { exact: true })).toBeVisible();
    await expect(region.getByText("CATASTROPHIC")).toBeVisible();

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
    await expect(page.getByText("user", { exact: true })).toBeVisible();
    expect(errors, `console pageerror events: ${errors.join("; ")}`).toEqual([]);
  });
});
