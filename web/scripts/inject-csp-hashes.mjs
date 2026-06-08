// Post-export CSP hash extractor (issue #185).
//
// Runs as part of `pnpm run export`, AFTER `next build` and BEFORE the
// `cp out/. ../schemabrain/dashboard/static/` step, so the manifest it
// writes rides into the wheel's static dir.
//
// What it does:
//   1. scans every `*.html` under ./out (the static export),
//   2. extracts each INLINE `<script>` block (no `src=` attribute),
//   3. computes its CSP source hash, `sha256-<base64>`, over the EXACT
//      bytes of the script's text content,
//   4. writes the deduped, sorted union to ./out/csp-script-hashes.json.
//
// The FastAPI sidecar (schemabrain/dashboard/sidecar.py) reads that
// manifest and assembles `script-src 'self' '<hash>'…`, dropping
// 'unsafe-inline'. A pinned `generateBuildId` (next.config.mjs) keeps the
// inline RSC-flight payload byte-stable, so the same source produces the
// same hashes across builds.
//
// Why a byte-exact capture is correct: `<script>` is an HTML "raw text
// element" — the parser takes its content verbatim (no entity decoding;
// only `</script` terminates it). So the bytes between `>` and
// `</script>` ARE the text the browser hashes for CSP. We must NOT trim
// or decode them.
//
// Fail-loud posture: a wrong/empty manifest silently BREAKS the
// dashboard (every inline script refused), so this script exits non-zero
// rather than shipping a manifest that doesn't match the bytes.

import { createHash } from "node:crypto";
import { readdir, readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

// Resolve ./out relative to THIS file (web/scripts/ → web/out), so the
// script is correct regardless of the caller's working directory.
const OUT_DIR = fileURLToPath(new URL("../out", import.meta.url));
const MANIFEST_NAME = "csp-script-hashes.json";

// Opening <script ...> + verbatim content + closing </script>. The
// attribute span `[^>]*` is safe here: Next's emitted <script> open tags
// carry only simple attributes (src, async, id, type) with no `>` in any
// value. `[\s\S]*?` is non-greedy, stopping at the FIRST `</script>` — and
// that is exactly correct, not a bug: <script> is an HTML "raw text
// element", so the browser's tokenizer ALSO terminates at the first
// `</script` regardless of JS string/comment context (a literal
// `</script>` inside a script body is impossible — Next escapes it as
// `</script`). So this regex computes the hash over the same bytes
// the browser does. A JS-aware parser that "skipped" a quoted `</script>`
// would DISAGREE with the browser and produce a wrong, rejected hash.
const SCRIPT_RE = /<script\b([^>]*)>([\s\S]*?)<\/script>/gi;
// Detect an EXTERNAL src attribute (covered by 'self', so not hashed).
// Require whitespace immediately before `src` so hyphenated attributes
// like `data-src` / `nomodule-src` are NOT misread as `src` (a bare
// `\bsrc` matches the `src` in `data-src` via the `-`→`s` word boundary,
// which would wrongly skip — and thus omit the hash of — an inline script
// that merely carried a `data-src` attribute).
const HAS_SRC_RE = /\ssrc\s*=/i;

async function htmlFilesUnder(dir) {
  const out = [];
  const entries = await readdir(dir, { withFileTypes: true });
  for (const entry of entries) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      out.push(...(await htmlFilesUnder(full)));
    } else if (entry.isFile() && entry.name.endsWith(".html")) {
      out.push(full);
    }
  }
  return out;
}

function inlineScriptHashes(html) {
  const hashes = [];
  for (const match of html.matchAll(SCRIPT_RE)) {
    const [, attrs, content] = match;
    if (HAS_SRC_RE.test(attrs)) continue; // external chunk — covered by 'self'
    const digest = createHash("sha256").update(content, "utf8").digest("base64");
    hashes.push(`sha256-${digest}`);
  }
  return hashes;
}

async function main() {
  const htmlFiles = await htmlFilesUnder(OUT_DIR);
  if (htmlFiles.length === 0) {
    console.error(
      `[inject-csp-hashes] no *.html under ${OUT_DIR} — did 'next build' run first?`,
    );
    process.exit(1);
  }

  const seen = new Set();
  for (const file of htmlFiles) {
    const html = await readFile(file, "utf8");
    for (const hash of inlineScriptHashes(html)) seen.add(hash);
  }

  if (seen.size === 0) {
    console.error(
      "[inject-csp-hashes] extracted ZERO inline-script hashes — the export " +
        "emits inline <script> blocks, so the extractor almost certainly " +
        "failed to match them. Refusing to ship a manifest that would " +
        "produce a script-src that breaks the dashboard.",
    );
    process.exit(1);
  }

  const hashes = [...seen].sort();
  const manifestPath = join(OUT_DIR, MANIFEST_NAME);
  await writeFile(manifestPath, `${JSON.stringify(hashes, null, 2)}\n`, "utf8");
  console.log(
    `[inject-csp-hashes] ${hashes.length} distinct inline-script hash(es) ` +
      `from ${htmlFiles.length} HTML file(s) → out/${MANIFEST_NAME}`,
  );
}

await main();
