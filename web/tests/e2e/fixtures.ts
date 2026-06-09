/**
 * Deterministic API fixtures for the visual-regression suite (visual.spec.ts).
 *
 * Visual baselines demand byte-stable renders. The dashboard's live `/api/*`
 * data is NOT stable: the demo seed stamps audit rows + `last_indexed_at` with
 * wall-clock time (so the Merkle root and every "Xm ago" change per boot). So
 * the visual suite never touches live data — it route-injects a frozen snapshot
 * of every read-only endpoint and freezes the browser clock.
 *
 * The snapshots under `./fixtures/*.json` were captured verbatim from a real
 * sidecar boot (`scripts/dashboard_demo.py`), so they match the live response
 * SHAPE exactly — the behavioural specs (smoke/overview/graph/…) still assert
 * against the live sidecar, so any contract drift fails there, not here. Only
 * `meta.store_path` was normalised (it leaked a machine-specific temp path);
 * the audit rows + Merkle root are kept as a consistent set so the surface's
 * in-browser root reconciliation still reads "intact".
 *
 * Regenerate after an API shape change: re-capture the snapshots from a live
 * sidecar (see web/tests/e2e/README.md → "Visual regression"), then refresh the
 * pixel baselines with scripts/update_visual_baselines.sh.
 */

import type { Page } from "@playwright/test";

import auditMerkleRoot from "./fixtures/audit-merkle-root.json";
import auditRefusals from "./fixtures/audit-refusals.json";
import auditRows from "./fixtures/audit-rows.json";
import auditVerify from "./fixtures/audit-verify.json";
import dict from "./fixtures/dict.json";
import drift from "./fixtures/drift.json";
import entities from "./fixtures/entities.json";
import entityUserColumns from "./fixtures/entity-user-columns.json";
import graph from "./fixtures/graph.json";
import meta from "./fixtures/meta.json";
import overview from "./fixtures/overview.json";
import piiMatrix from "./fixtures/pii-matrix.json";
import piiPolicy from "./fixtures/pii-policy.json";
import piiPolicyPreview from "./fixtures/pii-policy-preview.json";

/**
 * The instant the browser clock is frozen to. ~25 minutes after the seeded
 * timestamps in the fixtures, so relative-time renders read naturally
 * ("25m ago") and — critically — identically on every run. Epoch chosen to
 * match the captured `last_indexed_at` (1781012680s) + 1500s.
 */
export const FROZEN_NOW_MS = 1_781_014_180_000;

/** A minimal healthy response so the shell never paints a disconnected state. */
const HEALTH = { status: "ok", store: "ok", store_reason: null, uptime_s: 1 } as const;

/** Exact-pathname → fixture. Query strings (`?source_connection_id=…`,
 * `?limit=…`) are ignored — we match on the pathname only. */
const FIXTURE_BY_PATH: Record<string, unknown> = {
  "/api/health": HEALTH,
  "/api/meta": meta,
  "/api/overview": overview,
  "/api/entities": entities,
  "/api/entities/pii-matrix": piiMatrix,
  "/api/pii/policy": piiPolicy,
  "/api/pii/policy/preview": piiPolicyPreview,
  "/api/drift": drift,
  "/api/dict": dict,
  "/api/graph": graph,
  "/api/audit/rows": auditRows,
  "/api/audit/refusals": auditRefusals,
  "/api/audit/merkle/root": auditMerkleRoot,
  "/api/audit/verify": auditVerify,
};

/** Parametric routes (`/api/entities/{name}/columns`) the index/drilldown hit. */
function matchParametric(pathname: string): unknown {
  if (/^\/api\/entities\/[^/]+\/columns$/.test(pathname)) return entityUserColumns;
  return undefined;
}

/**
 * Intercept every `/api/*` request and answer from the frozen fixtures. Any
 * endpoint without a fixture returns a deterministic 404 rather than falling
 * through to the live sidecar — so an unmocked call shows up as a stable,
 * obvious empty/error render in the baseline instead of silent flakiness.
 */
export async function routeAllApis(page: Page): Promise<void> {
  await page.route("**/api/**", async (route) => {
    const { pathname } = new URL(route.request().url());
    const json = FIXTURE_BY_PATH[pathname] ?? matchParametric(pathname);
    if (json === undefined) {
      await route.fulfill({
        status: 404,
        contentType: "application/json",
        body: JSON.stringify({ detail: `no visual fixture for ${pathname}` }),
      });
      return;
    }
    await route.fulfill({ json });
  });
}
