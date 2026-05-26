# `schemabrain/dashboard/static/`

This directory holds the pre-built Next.js static export served by the
FastAPI sidecar under `/`. It is **populated at wheel-build time**, not
checked into git (only `.gitkeep` + this README live here).

## At release time

`hatch build` runs `pnpm --dir web build && pnpm --dir web exec next export`
(via the CI `web-build` job) and copies the resulting `web/out/` into this
directory. The wheel then ships the static bytes; end users running
`pip install schemabrain[ui]` get them automatically.

## At contributor dev time

Contributors run `pnpm dev` from the `web/` directory against a live
`schemabrain serve` instance. The Next.js dev server proxies API requests
to the sidecar; the static directory stays empty.

## End users

End users never need Node. The wheel ships the built bytes; the sidecar
mounts this directory at `/` when it exists and has content.

See `docs/internal/v0.4_ui_rfc.md` §2.3 for the full build pipeline.
