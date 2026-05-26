# `web/` — SchemaBrain dashboard (v0.4 M1)

Next.js 15 static-export app that ships inside the Python wheel under
`schemabrain/dashboard/static/`. Served by the FastAPI sidecar at
`127.0.0.1` when the user runs `schemabrain dashboard`.

## Stack

- Next.js 15 (App Router, `output: "export"`)
- React 19
- Tailwind 4 (CSS-tokens-first, see [`app/globals.css`](app/globals.css))
- shadcn/ui patterns, vendored in [`components/ui/`](components/ui/)
- Zustand for client state
- TanStack Query for server cache
- TanStack Virtual for the audit table
- React Flow vendored for v0.5 Entity Browser readiness

## Contributor workflow

```bash
cd web/
pnpm install
pnpm dev          # http://localhost:3000 (proxies /api/* to 127.0.0.1:7878)

# in another terminal:
cd ..
schemabrain serve --source $DATABASE_URL --store-path ./schemabrain.db
schemabrain dashboard --port 7878 --no-open
```

## Release build (CI only)

```bash
pnpm install
pnpm build
pnpm export        # copies out/ → ../schemabrain/dashboard/static/
```

The Python wheel build (`hatch build`) then bundles `schemabrain/dashboard/static/**/*`
into the wheel via the `artifacts` glob in `pyproject.toml`.

## Three M1 surfaces

| Surface | Route | Spec |
|---|---|---|
| PII Visualization | `/pii` | `docs/internal/v0.4_ui_rfc.md` §5.1 |
| Refusal Experience UI | `/refusals` | §5.2 |
| Audit Viewer | `/audit` | §5.3 |

## Design tokens

All colour / type / spacing decisions are tokens declared in
[`app/globals.css`](app/globals.css). Tailwind references them via the
extensions in [`tailwind.config.ts`](tailwind.config.ts). Surface
components consume tokens directly — they do not hard-code hex values
or px sizes.

See [`~/.claude/rules/web/design-quality.md`](https://github.com/Arun-kc/.claude/) for
the anti-template policy this dashboard must satisfy at launch (R-4 in
master plan §5).
