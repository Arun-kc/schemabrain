#!/usr/bin/env bash
#
# Regenerate the dashboard visual-regression baselines (PR-21b).
#
# Pixel baselines are environment-specific, so they are captured inside the
# SAME pinned Playwright container CI asserts against
# (mcr.microsoft.com/playwright:v1.60.0-noble) — never on the host. This script:
#   1. builds the dashboard static export on the host,
#   2. boots the demo sidecar on the host (serves the export + live /api/*),
#   3. runs the visual suite in the pinned container with --update-snapshots,
#      reaching the host sidecar via host.docker.internal,
#   4. tears the sidecar down.
#
# The container gets its own node_modules (anonymous volumes) so the host's
# macOS-native install is never clobbered with Linux binaries. Generated
# baselines land in web/tests/e2e/__screenshots__/ on the host mount.
#
# Usage:  scripts/update_visual_baselines.sh
# Verify (no update): scripts/update_visual_baselines.sh --check
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="mcr.microsoft.com/playwright:v1.60.0-noble"
PORT=7878
HEALTH="http://127.0.0.1:${PORT}/api/health"
PW_ARGS="--update-snapshots"
if [[ "${1:-}" == "--check" ]]; then PW_ARGS=""; fi

cd "$REPO"

echo "==> [1/4] Building the dashboard static export (host)…"
pnpm --dir web run export

echo "==> [2/4] Booting the demo sidecar on :${PORT} (host)…"
uv run --extra dev --extra ui python scripts/dashboard_demo.py --no-open &
SIDECAR_PID=$!
cleanup() {
  echo "==> Stopping sidecar (pid ${SIDECAR_PID})…"
  kill "${SIDECAR_PID}" 2>/dev/null || true
  wait "${SIDECAR_PID}" 2>/dev/null || true
}
trap cleanup EXIT

echo "    waiting for ${HEALTH} …"
for _ in $(seq 1 30); do
  if curl -sf "${HEALTH}" >/dev/null 2>&1; then echo "    sidecar up."; break; fi
  sleep 1
done
curl -sf "${HEALTH}" >/dev/null 2>&1 || { echo "ERROR: sidecar did not come up"; exit 1; }

echo "==> [3/4] Running the visual suite in ${IMAGE} (${PW_ARGS:-assert})…"
# Anonymous volumes shadow node_modules AND the pnpm store so the container's
# Linux install never clobbers the host's macOS install — and, critically, so
# the container's content-addressable store isn't written back onto the
# bind-mounted repo (a ~700MB .pnpm-store turd at the repo root otherwise).
docker run --rm \
  -v "${REPO}":/work \
  -v /work/node_modules \
  -v /work/web/node_modules \
  -v /work/.pnpm-store \
  -w /work/web \
  --add-host=host.docker.internal:host-gateway \
  -e SCHEMABRAIN_E2E_BASE_URL="http://host.docker.internal:${PORT}" \
  -e CI=1 \
  "${IMAGE}" \
  bash -lc "corepack enable && corepack prepare pnpm@9 --activate && pnpm config set store-dir /work/.pnpm-store && pnpm install --frozen-lockfile && pnpm exec playwright test -c playwright.visual.config.ts ${PW_ARGS}"

echo "==> [4/4] Done. Baselines under web/tests/e2e/__screenshots__/"
