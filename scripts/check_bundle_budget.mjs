#!/usr/bin/env node
/**
 * Deterministic web performance budget gate (PR-21b, wsQA-perf-budget-landing).
 *
 * Lighthouse/CWV on CI is too noisy to BLOCK a merge, so this gate enforces the
 * deterministic half of the perf budget from the Next build output:
 *   - first-load JS (gzipped) ≤ --js KB
 *   - first-load CSS (gzipped) ≤ --css KB
 *   - a forbidden module is absent from every first-load chunk (--forbid-module),
 *     e.g. react-flow must stay dynamic-imported on /graph only, never in an
 *     eager bundle;
 *   - every @font-face uses font-display: swap (--fonts-swap).
 * LCP / CLS stay a manual Lighthouse check (documented in web/tests/e2e/README.md).
 *
 * Reads <app>/.next/app-build-manifest.json — each page entry is its full
 * first-load set (shared runtime + layout chain + page), so summing the target
 * route's files is the route's first-load. Run AFTER `next build`.
 *
 * Usage:
 *   node scripts/check_bundle_budget.mjs --app site --route /page --js 150 --css 30 \
 *        --forbid-module react-flow__ --fonts-swap
 *   node scripts/check_bundle_budget.mjs --app web  --route '/(app)/overview/page' \
 *        --js 300 --css 50 --forbid-module react-flow__ --fonts-swap
 */

import { gzipSync } from "node:zlib";
import { readFileSync } from "node:fs";
import { join } from "node:path";

function parseArgs(argv) {
  const args = { fontsSwap: false };
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (a === "--app") args.app = argv[++i];
    else if (a === "--route") args.route = argv[++i];
    else if (a === "--js") args.jsKb = Number(argv[++i]);
    else if (a === "--css") args.cssKb = Number(argv[++i]);
    else if (a === "--forbid-module") args.forbid = argv[++i];
    else if (a === "--fonts-swap") args.fontsSwap = true;
  }
  if (!args.app || !args.route) {
    console.error("usage: --app <dir> --route <key> [--js KB] [--css KB] [--forbid-module s] [--fonts-swap]");
    process.exit(2);
  }
  return args;
}

const KB = 1024;
const args = parseArgs(process.argv.slice(2));
const repoRoot = new URL("..", import.meta.url).pathname;
const nextDir = join(repoRoot, args.app, ".next");

const manifest = JSON.parse(readFileSync(join(nextDir, "app-build-manifest.json"), "utf8"));
const pages = manifest.pages ?? {};
if (!pages[args.route]) {
  console.error(`route "${args.route}" not in ${args.app}/.next/app-build-manifest.json`);
  console.error(`available routes:\n  ${Object.keys(pages).join("\n  ")}`);
  process.exit(2);
}

/** Segment list of a route key's directory, e.g. "/(app)/overview/page" → ["(app)","overview"]. */
const dirSegs = (key) =>
  key.replace(/\/(page|layout)$/, "").split("/").filter(Boolean);

/**
 * A route's true first-load = its own chunks UNIONED with every ancestor
 * layout's chunks (App Router loads the whole layout chain). The root `/layout`
 * is an ancestor of everything; `/(app)/layout` is an ancestor of `/(app)/*`.
 * Without this, CSS that lives on a layout (Next's usual placement) is missed.
 */
const routeSegs = dirSegs(args.route);
const isAncestorLayout = (key) => {
  if (!key.endsWith("/layout")) return false;
  const segs = dirSegs(key);
  return segs.every((s, i) => routeSegs[i] === s); // segment-prefix (root layout = vacuous true)
};
const firstLoadKeys = [args.route, ...Object.keys(pages).filter(isAncestorLayout)];
const files = [...new Set(firstLoadKeys.flatMap((k) => pages[k]))];

const gz = (rel) => gzipSync(readFileSync(join(nextDir, rel))).length;
const sum = (xs) => xs.reduce((a, b) => a + b, 0);

const jsFiles = files.filter((f) => f.endsWith(".js"));
const cssFiles = files.filter((f) => f.endsWith(".css"));
const jsBytes = sum(jsFiles.map(gz));
const cssBytes = sum(cssFiles.map(gz));

const failures = [];
const note = (ok, label) => `${ok ? "✓" : "✗"} ${label}`;
const kb = (b) => `${(b / KB).toFixed(1)}kb`;
const lines = [`[perf-budget] ${args.app} · ${args.route}`];

if (args.jsKb != null) {
  const ok = jsBytes <= args.jsKb * KB;
  lines.push(note(ok, `first-load JS  ${kb(jsBytes)} / ${args.jsKb}kb gz (${jsFiles.length} files)`));
  if (!ok) failures.push(`first-load JS ${kb(jsBytes)} exceeds ${args.jsKb}kb`);
}
if (args.cssKb != null) {
  const ok = cssBytes <= args.cssKb * KB;
  lines.push(note(ok, `first-load CSS ${kb(cssBytes)} / ${args.cssKb}kb gz (${cssFiles.length} files)`));
  if (!ok) failures.push(`first-load CSS ${kb(cssBytes)} exceeds ${args.cssKb}kb`);
}

if (args.forbid) {
  const culprits = jsFiles.filter((f) => readFileSync(join(nextDir, f), "utf8").includes(args.forbid));
  const ok = culprits.length === 0;
  lines.push(note(ok, `"${args.forbid}" absent from first-load`));
  if (!ok) failures.push(`"${args.forbid}" found in first-load chunk(s): ${culprits.join(", ")}`);
}

if (args.fontsSwap) {
  // Every @font-face that FETCHES a web font (src: url(…)) must declare
  // font-display: swap (no FOIT). next/font's metric-matched fallback faces
  // (`… Fallback`, src: local(…) + size-adjust) render instantly from a system
  // font and intentionally carry no font-display, so they're exempt. Scan every
  // CSS file Next emitted so a stray non-swap fetched face anywhere is caught.
  const allCss = [...new Set(Object.values(manifest.pages).flat().filter((f) => f.endsWith(".css")))];
  const bad = [];
  for (const f of allCss) {
    const css = readFileSync(join(nextDir, f), "utf8");
    for (const face of css.match(/@font-face\s*\{[^}]*\}/g) ?? []) {
      const fetchesFont = /src\s*:[^}]*url\(/.test(face);
      if (fetchesFont && !/font-display\s*:\s*swap/.test(face)) bad.push(f);
    }
  }
  const ok = bad.length === 0;
  lines.push(note(ok, `every fetched @font-face uses font-display: swap`));
  if (!ok) failures.push(`fetched @font-face without font-display:swap in: ${[...new Set(bad)].join(", ")}`);
}

console.log(lines.join("\n  "));
if (failures.length) {
  console.error(`\n[perf-budget] FAILED:\n  - ${failures.join("\n  - ")}`);
  process.exit(1);
}
console.log("[perf-budget] OK");
