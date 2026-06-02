---
title: "ADR 0005 — Dashboard routing under static export"
description: "Keep Next.js App-Router multi-page routes behind a shared root-layout shell; address the entity drilldown by ?entity= query param so it survives static export without pre-rendering runtime entities."
---

# ADR 0005 — Dashboard client routing under static export

- **Status:** Accepted
- **Date:** 2026-06-02

## Context

The dashboard ships as a **Next.js App-Router static export** (`output: "export"`,
`web/next.config.mjs`). The build emits flat per-route HTML — `index.html`,
`pii.html`, `refusals.html`, `audit.html` — which is copied into
`schemabrain/dashboard/static/` at wheel-build time and served by the FastAPI
sidecar through `StaticFiles(html=True)`. **No Node runtime ever runs in
production; the sidecar serves bytes** and binds to `127.0.0.1` only.

Today each of the four surfaces is a standalone `app/<surface>/page.tsx` that
renders its own `HeaderStrip` (`web/components/dashboard/HeaderStrip.tsx`). There
is no shared chrome, no nav rail, and no router state beyond Next's own
file-based routes — the "MPA of headers" structure from the original
four-surface build.

The post-launch dashboard rebuild changes that shape materially:

1. A **shared app shell** — top bar + nav rail + source selector + theme
   toggle + ⌘K — must persist across every surface without remount or flash.
2. The route set grows from 4 to **9** (overview, graph, entities, dict, pii,
   refusals, audit, policy, drift).
3. An **app-level entity drilldown sheet** opens over *any* surface (from the
   graph canvas, the Entities index, or a PII row) and must be deep-linkable.

This forces a routing decision that touches every subsequent UI PR — it is a
**one-way door**. Two hard constraints scope it:

- **Static export, no server.** Dynamic route segments (`/entities/[name]`) under
  `output: "export"` require `generateStaticParams` to enumerate every path at
  *build* time. Entities are **runtime data** — they depend on whichever database
  the operator indexed — so there is no build-time list to pre-render.
- **Deep-link + refresh must work.** The sidecar serves bytes; a hard refresh on
  `/pii` or a shared link to a specific entity has to resolve without a Node
  router rewriting paths on the fly.

This ADR locks the routing model for the whole post-launch surface set.

## Decision

### 1. Keep Next.js App-Router multi-page routes — not a client-router SPA

Each surface stays its own `app/<surface>/page.tsx`. We do **not** collapse the
app into a single `index.html` driven by a client-side router (react-router or
equivalent).

Rationale:

- **Static export already gives us per-route HTML for free.** A client router
  would fight the export model — every deep link would resolve to the SPA shell
  and re-route on the client, and a hard refresh on a sub-route would 404 unless
  the sidecar rewrote everything to `index.html`.
- **Route-level code splitting is automatic.** The heavy `/graph` surface
  (reactflow) only ships its chunk on that route; the PII/audit surfaces never
  pay for it. A single-page router would have to re-implement this with manual
  lazy boundaries.
- **It is the idiomatic path** for a Next export and keeps `trailingSlash:
  false` + the existing `StaticFiles(html=True)` contract untouched.

### 2. Promote routing into a shared root-layout shell

The persistent shell lives in the App-Router **layout** tree, not in each page.
A shared layout (the app shell: top bar, nav rail, source selector, theme
toggle) wraps all nine routes; per-surface `HeaderStrip` is removed.

App-Router layouts do **not remount when navigating between sibling routes** —
React preserves the layout subtree across the navigation. That is exactly the
property the shell needs:

- Nav-rail active state, source selection, theme, and scroll **survive** route
  changes with no flash and no re-fetch.
- The shell mounts once; only the `{children}` slot swaps per route.

This is the concrete meaning of "MPA-routes-with-shared-layout": MPA on the
*wire* (one HTML file per route, served statically), SPA-grade on
*navigation* (Next's client-side soft navigation keeps the shell mounted and
swaps only the page).

### 3. Entity drilldown is a `?entity=<name>` query param, not a route segment

The app-level drilldown sheet is addressed by a **query parameter on the current
route** — `…/graph?entity=orders`, `…/pii?entity=customers` — read on the client
with `useSearchParams`. It is **not** a `/entities/[name]` dynamic segment.

Rationale:

- **Survives static export with zero pre-rendering.** A query param adds no new
  build-time path; the same nine HTML files serve every entity. A dynamic
  segment would need `generateStaticParams`, which we cannot populate from
  runtime data.
- **Opens over any surface.** The sheet is app-level; `?entity=` lets the
  graph, the Entities index, and a PII row all drive the *same* sheet without
  each surface owning a container.
- **Shareable + deep-linkable + back-button-correct.** The param is real URL
  state: copy the link, reopen the sheet; press Back, the param drops and the
  sheet closes. A single global selected-entity store syncs to and from this
  param so there is one source of truth.

The `/entities` route is the sortable *index* of entities; the sheet is an
overlay keyed by `?entity=`. They are distinct: the index is a pre-rendered
page, the sheet is pure client state over whatever route you are on.

### 4. URL as the home for shareable state; everything else is client state

The convention for the whole app:

- **Route** = which surface (`/pii`, `/graph`, …).
- **`?entity=`** = which drilldown sheet is open (or none).
- **Active source** = resolved by the existing `useSourceId` hook /
  source-selector store; it may surface as `?source=` when shareable selection
  is wanted, but the hook stays the single resolver — pages never hardcode a
  source id.

Transient UI (hover, sort direction, ⌘K open) stays in component/client state,
not the URL.

### 5. Static export retained; the sidecar serving contract is unchanged

`output: "export"` + `trailingSlash: false` stay as-is. The five new routes
emit five new flat HTML files (`graph.html`, `entities.html`, `dict.html`,
`policy.html`, `drift.html`) served exactly like the current four via
`StaticFiles(html=True)`; unknown paths still fall back to `index.html`. Because
the drilldown is a query param, it introduces **no new paths** — the sidecar
fallback behaviour is untouched. The dev-only `/api/*` rewrite to the sidecar at
`127.0.0.1:7878` is likewise unaffected.

A hard CI merge-gate asserts every one of the nine route HTML files is present
in the shipped wheel, so a missing export fails publish rather than shipping a
blank surface.

## Consequences

**What becomes possible:**

- A single app shell renders on all nine routes with persistent nav/source/theme
  and no per-surface header duplication.
- The entity sheet opens over any surface from any trigger (graph node, Entities
  row, PII row) and is shareable by URL.
- `/graph`'s reactflow bundle is isolated to that route by automatic route-level
  code splitting — the landing/perf budget is reachable without manual
  lazy-loading gymnastics.
- The sidecar stays a dumb byte server; nothing about the localhost-only,
  no-Node-in-prod posture changes.

**What constrains future evolution:**

- **No server-rendered, per-entity URLs.** Entity drilldowns are always
  `?entity=` over a static page, never `/entities/<name>` SSR pages. If a future
  build wants true per-entity pre-rendered pages, it must either leave static
  export (adding a Node runtime + a new threat model) or generate entity paths
  at index time, which is out of scope.
- **Soft navigation depends on Next's client router.** Deep links and refresh
  resolve to static HTML, but in-app navigation that keeps the shell mounted
  relies on Next's `<Link>`/router. Surfaces must use it (not raw `<a>` full-page
  loads) or they remount the shell and lose the persistence this ADR buys.
- **One shell owner.** The shell is authored once in the layout tree; surfaces
  render into the slot and must not re-introduce per-page chrome.

**What remains deferred:**

- A `?source=` shareable selection (vs. the current resolver-hook default) —
  adopt only if operators ask to share source-scoped links.
- True dynamic/SSR routes of any kind — explicitly out of the static-export
  model for v1; revisit only alongside any future hosted-transport work.
- ⌘K command-palette routing affordances beyond open/close — the palette ships
  as a stub in the shell; richer command routing is a later surface.

## References

- `web/next.config.mjs` — `output: "export"`, `trailingSlash: false`, dev
  `/api/*` rewrite.
- `web/app/layout.tsx` — the root layout that becomes the shell host.
- `web/components/dashboard/HeaderStrip.tsx` — the per-surface header this ADR
  retires in favour of the shared shell.
- `schemabrain/dashboard/` — the FastAPI sidecar that serves the export via
  `StaticFiles(html=True)` on `127.0.0.1`.
- ADR 0004 — observability event bus (the SSE stream the shell's live badges
  consume).
