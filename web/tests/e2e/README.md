# Dashboard E2E smoke

A small Playwright suite that drives a real Chromium against a running
dashboard sidecar and asserts the four M1 surfaces render with live
data.

## What it covers

| Surface | Spec | Assertion shape |
| --- | --- | --- |
| Landing (`/`) | renders the three surface cards | hero h1 + each card name + telemetry section |
| PII Ledger (`/pii`) | stat slab + entity matrix | "THE LEDGER" + slab label + at least one entity row |
| Refusal Experience (`/refusals`) | populated incident detail | feed header + "Refusal Event:" detail + `pii_blocked` envelope tag |
| Audit Viewer (`/audit`) | ledger chain + selected-block payload | "Ledger Chain Intact" + JSON payload pane |
| Source ID auto-resolution | header strip pill | a 16-char hex string appears (proves `useSourceId` works) |

Each spec also asserts **zero `pageerror` events** during navigation —
catches missing CSS, JS exceptions, hydration mismatches that would
otherwise render silently.

Screenshots land in `test-results/` and `playwright-report/` after
each run.

## Prerequisites

The smoke does not boot its own sidecar (that's a follow-up PR's job).
You need:

1. **A populated SchemaBrain store.** It must have at least one indexed
   source AND at least one refused audit row (so the Refusal spec
   doesn't hit the empty state).
2. **A dashboard sidecar serving that store on `http://127.0.0.1:7878`.**

The canonical bring-up flow lives in [`scripts/dashboard_demo.py`](../../../scripts/dashboard_demo.py). Run it once to get a fully seeded store + a sidecar on `127.0.0.1:7878`:

```bash
.venv/bin/python scripts/dashboard_demo.py
```

To force a refused audit row (so the Refusal Experience spec asserts
against populated data rather than the empty state), append a row via
the canonical seed helper:

```bash
.venv/bin/python scripts/seed_refused_audit_row.py \
  --store-path <store-path-from-demo-script> \
  --source-id <source-id-from-demo-script>
```

The store path + source id are printed by `dashboard_demo.py` on boot.

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

## What this does NOT cover

- Sidecar boot / API smoke — covered by `tests/dashboard/test_sidecar_routes.py`
- Accessibility audits — `@axe-core/playwright` is installed but not
  wired up here; follow-up PR
- Visual-regression baselines — screenshots are artifacts for review,
  not pixel-diff assertions. Pin pixel baselines once the design is
  fully locked
- CI execution — the suite runs reliably on a dev machine; a follow-up
  PR adds a CI job that brings up the stack before invoking `pnpm test:e2e`
