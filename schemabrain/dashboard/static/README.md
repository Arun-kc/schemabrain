# `schemabrain/dashboard/static/`

This directory holds the pre-built Next.js static export served by the
FastAPI sidecar under `/`. It is **populated at wheel-build time**, not
checked into git (only `.gitkeep` + this README live here).

## At release time

The release workflow (`.github/workflows/publish.yml`, the `build` job)
runs `pnpm run export` from `web/` — which is `next build` followed by a
copy of `web/out/` into this directory — BEFORE building the wheel with
`uv build --wheel`. (Next.js dropped the standalone `next export`
command, so the copy is wired into the `export` script in
`web/package.json`. Hatchling is the build backend, configured via the
`artifacts` glob in `pyproject.toml`; there is no `hatch build` hook.)
The wheel then ships the static bytes; end users running
`pip install schemabrain[ui]` get them automatically.

## At contributor dev time

Contributors run `pnpm dev` from the `web/` directory against a live
`schemabrain serve` instance. The Next.js dev server proxies API requests
to the sidecar; the static directory stays empty.

## End users

End users never need Node. The wheel ships the built bytes; the sidecar
mounts this directory at `/` when it exists and has content.

See `.github/workflows/publish.yml` (the `build` job) for the full
release build pipeline.
