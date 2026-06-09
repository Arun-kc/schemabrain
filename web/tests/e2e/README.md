# Dashboard E2E smoke

A small Playwright suite that drives a real Chromium against a running
dashboard sidecar and asserts the four M1 surfaces render with live
data.

## What it covers

| Surface | Spec | Assertion shape |
| --- | --- | --- |
| Landing (`/`) | renders the three surface cards | hero h1 + each card name + telemetry section |
| PII matrix (`/pii`) | confidence heatmap | "PII matrix" heading + band legend + at least one classified column |
| Refusals (`/refusals`) | protective ledger with a held row | "Refusals" heading + protective lede + a `Sensitive-data` row terminating in a green "held" status |
| Audit Viewer (`/audit`) | ledger chain + selected-block payload | "Ledger Chain Intact" + JSON payload pane |
| Source ID auto-resolution | header strip pill | a 16-char hex string appears (proves `useSourceId` works) |

Each spec also asserts **zero `pageerror` events** during navigation —
catches missing CSS, JS exceptions, hydration mismatches that would
otherwise render silently.

**Accessibility & responsive.** Every surface spec runs an
`@axe-core/playwright` audit (WCAG 2.0/2.1 A+AA, failing only on
serious/critical impacts) under both themes via the shared `a11y.ts`
helper. `a11y-system.spec.ts` adds the cross-cutting shell guarantees:
the skip link, keyboard reachability of the nav rail + source selector,
≥44px nav targets, no horizontal overflow at 375/768/1024/1440, and the
app-wide reduced-motion contract. Contrast is enforced as a side effect —
the suite drove the AA-safe `--*-strong` text shades in `app/sb-theme.css`.

Screenshots land in `test-results/` and `playwright-report/` after
each run.

## Prerequisites

The smoke needs a running sidecar pointed at a populated store. Two ways
to get there:

### Option A — one command (recommended)

```bash
.venv/bin/python scripts/dashboard_demo.py
```

Seeds a throwaway SQLite store under `/tmp/` with the entities, PII
tags, and 3 audit rows (1 success + 2 refusals) the suite expects, then
boots the sidecar on `http://127.0.0.1:7878`.

For a headless boot (no browser open — matches how CI runs it):

```bash
.venv/bin/python scripts/dashboard_demo.py --no-open
```

### Option B — point at your own store

If you already have a SchemaBrain store with **at least one indexed
source AND at least one refused audit row** (so the Refusal spec doesn't
hit the empty state), boot the sidecar against it directly:

```bash
.venv/bin/schemabrain dashboard --store-path /path/to/store.db --no-open
```

To seed a refused audit row into an existing store:

```bash
.venv/bin/python scripts/seed_refused_audit_row.py \
  --store-path /path/to/store.db \
  --source-id <source-id-from-/api/meta>
```

## Run

From the repo root:

```bash
pnpm --filter web exec playwright install chromium  # first time only
pnpm --filter web test:e2e
```

The HTML report opens with `pnpm --filter web exec playwright show-report`.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `SCHEMABRAIN_E2E_BASE_URL` | `http://127.0.0.1:7878` | Override when the sidecar binds a non-default port. |

## CI

The `dashboard-e2e` job in [`.github/workflows/ci.yml`](../../../.github/workflows/ci.yml)
runs this suite on every PR that touches the dashboard surface (`web/**`,
`schemabrain/dashboard/**`, `scripts/dashboard_demo.py`, or the workflow
file). The job boots `dashboard_demo.py --no-open` in the background,
waits for `/api/health`, then runs `pnpm test:e2e`. On failure the HTML
report + per-test screenshots upload as a `playwright-report` artifact
(7-day retention).

Paths-filtered so docs-only and core-Python-only PRs don't pay the
~3-4 min cold-cache cost.

## What this does NOT cover

- Sidecar boot / API smoke — covered by `tests/dashboard/test_sidecar_routes.py`
- Visual-regression baselines — screenshots are artifacts for review,
  not pixel-diff assertions. Pin Linux-captured pixel baselines in a
  follow-up QA pass
