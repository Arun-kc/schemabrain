# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `schemabrain --version` / `-V` flag. Reads from installed package
  metadata via `importlib.metadata`, so `pyproject.toml` is the single
  source of truth for the version literal.
- `schemabrain fixture-path <name>` subcommand prints the absolute path
  to a bundled fixture (`ecommerce.sql`) or golden set
  (`ecommerce.json`). Designed for shell substitution in the README
  quickstart. Rejects path separators, `.`, `..`, and symlink escapes.
- `--url-env VARNAME` flag on `schemabrain index`, `serve`, and `eval`.
  Reads the connection URL from a named environment variable so the
  password never appears in argv (visible to `ps`, shell history, and
  journald). New scripts should prefer `--url-env` over the positional
  URL form. Closes the HIGH-severity finding from the 2026-05-11
  security audit.
- `SECURITY.md` at the repo root: vulnerability disclosure policy,
  acknowledgement SLA, scope, and coordinated-disclosure window.
  GitHub Private Vulnerability Reporting is the preferred channel; email
  is the fallback.
- `.github/dependabot.yml`: weekly minor+patch grouped updates for the
  pip and github-actions ecosystems, plus a separate security-updates
  group so CVE patches don't get held back by version-bump batching.
- PyYAML pinned as a direct dev dependency (used by the new dependabot
  config structural tests).
- `pip-audit` runs on every PR via a new `security` CI job. Strict
  mode fails the build on any known-CVE dep. One transient suppression:
  CVE-2025-71176 (pytest local-DoS, dev-only, fix in 9.0.3 — Dependabot
  will ship the bump as its own PR).
- `bandit` runs on every PR via the same `security` job (strictest
  `-ll` threshold). Two `# nosec B608` suppressions on the two
  identifier-only f-string SQL assemblies in `profiler/postgres.py`
  — both audit-verified safe (all interpolated values come from
  SQLAlchemy's `IdentifierPreparer.quote()`; no user input enters
  the SQL string). Configuration lives in `[tool.bandit]` in
  `pyproject.toml` and excludes tests/scripts where intentional
  `assert` and fake-URL usage would generate noise.
- `semgrep` runs on every PR via the same `security` job, using the
  community `p/python` + `p/security-audit` rulesets and `--error`
  so any unsuppressed finding fails the build. Run via `uvx` rather
  than pinned as a dev dep — the 46 MB binary is heavy and the rule
  registry updates independently of the binary version. Two
  `# nosemgrep:` suppressions on the two `text(sql)` calls that
  pair with the bandit suppressions (same underlying safety
  guarantee, different scanner).
- Source-database engines (`PostgresDataSource`, `PostgresProfiler`)
  now set `default_transaction_read_only=on` at the connection
  boundary. The profiler has always issued `SELECT` only, but the
  flag enforces the contract at the Postgres session level — any
  future regression that tries to INSERT/UPDATE/DELETE/DROP fails
  with a clear `read-only transaction` error rather than silently
  mutating a customer database. Backs the read-only claim in
  `SECURITY.md` with server-side enforcement instead of code-review
  convention. Three new integration tests pin the behaviour.

### Changed (CI)
- Workflow-level `permissions: contents: read` on `.github/workflows/ci.yml`.
  No job currently needs write access; if one ever does (e.g. publishing
  a wheel), it must declare elevated permissions explicitly. Hardens
  against a compromised CI step (third-party action, semgrep rule pack)
  ever exfiltrating or pushing.
- `security` job now `needs: lint-and-unit` so a typo in source doesn't
  burn a ~90 s `uv sync` + 3 scanner runs before `ruff check` rejects
  the same PR. Sequencing doesn't lengthen the critical path because
  `integration` (already after lint, ~3-4 min) dominates anyway.
- `semgrep` is now pinned to a specific version (`uvx semgrep@1.163.0`)
  in the security job. Previous unpinned `uvx semgrep` would have
  resolved to the latest PyPI release every run; a malicious or buggy
  semgrep release would have flipped the `--error` gate red on benign
  code with no warning. Rule packs (`p/python`, `p/security-audit`)
  still fetch from the registry at run time, so rule updates land
  automatically; the binary version is now under our control.
- All three CI jobs now declare `timeout-minutes` (10 for `lint-and-unit`
  and `security`, 15 for `integration`). Previously GitHub's 360-minute
  default would have allowed a hung dep / container to burn 6 hours of
  CI minutes per run.

### Changed
- `schemabrain.__version__` is now read dynamically from package
  metadata. The literal was previously hardcoded in `__init__.py`.
- README quickstart uses `$(schemabrain fixture-path ecommerce.sql)`
  for the seed step instead of an inline Python path-resolution
  expression.
- README, `docs/setup.md`, and the Claude Desktop / Cursor example
  configs now lead with `--url-env DATABASE_URL` and an `env:` block
  for the URL. The legacy positional / `--source <url>` form still
  works for backwards compatibility but emits a one-line stderr
  deprecation warning when the URL contains a password.
- `schemabrain index` no longer requires the positional URL at the
  argparse layer (`nargs="?"`). Either `--url-env` or the positional
  URL is now required, and the missing-both case renders a guided
  error instead of an argparse usage dump.
- `schemabrain serve --source` and `schemabrain eval --source` are no
  longer marked `required=True` at the argparse layer. Either flag
  (`--source` or `--url-env`) satisfies the requirement; passing both
  is a guided error.

## [0.1.0a1] - 2026-05-11

First public preview. Live on PyPI as `schemabrain==0.1.0a1`.

### Added
- Postgres + SQLite source support (psycopg v3 scheme:
  `postgresql+psycopg://`).
- LLM enrichment via Anthropic SDK. Default model is Claude Haiku 4.5;
  `--enable-sonnet` routes cryptic column names (heavy abbreviations)
  to Claude Sonnet 4.6.
- Local embeddings via fastembed (`BAAI/bge-small-en-v1.5`, 384-dim).
- Cost-capped enrichment via `--max-cost` (default $10/run) with clean
  mid-run abort and pristine cache on cap-trip.
- Cache-aware re-indexing: a fingerprint hash over schema + sample
  values skips unchanged tables on every subsequent run, producing
  zero LLM calls and zero embedding calls on a no-op rerun.
- 4 MCP tools exposed over stdio:
  - `find_relevant_tables(query, limit)` — cosine retrieval over
    column embeddings, per-table max score.
  - `describe_table(qualified_name)` — full structural + semantic
    detail with foreign keys.
  - `describe_column(qualified_name)` — column detail including
    outgoing and incoming foreign-key references.
  - `suggest_joins(tables, max_hops)` — undirected BFS shortest-path
    over the FK graph; handles composite and self-referential FKs.
- CLI subcommands: `index`, `eval`, `serve`.
- Eval harness: typed `GoldenSet` loader (Pydantic-validated),
  `KeywordRetriever` baseline, `EmbeddingRetriever` default,
  macro `recall@1`/`@3`/`@10` reporting.
- Partition-child filter on Postgres — declarative partition tables
  are skipped during indexing (saved ~34% cost / 50% time on Pagila).
- M:N junction table detection — descriptions and the enrichment
  prompt acknowledge junction nature so agents emit double-counting
  caveats when joining through them.
- Versioned prompts baked into the fingerprint hash so prompt changes
  cleanly invalidate the cache.
- Bundled e-commerce SQL fixture (`fixtures/ecommerce.sql`) and golden
  set (`golden_sets/ecommerce.json`) as a starter example. The harness
  itself is domain-agnostic.
- `examples/claude_desktop_config.example.json` for one-line Claude
  Desktop wiring; `examples/anthropic_demo.py` for headless agent
  loops over the same MCP transport.
- MIT license; SSH-signed commits; CI on Python 3.11 + 3.12 (Linux
  unit) plus Docker Postgres integration with `--cov-fail-under=99`.

[Unreleased]: https://github.com/Arun-kc/schemabrain/compare/v0.1.0a1...HEAD
[0.1.0a1]: https://github.com/Arun-kc/schemabrain/releases/tag/v0.1.0a1
