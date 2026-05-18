# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] - 2026-05-18

This release rounds out the v1 semantic-layer arc. Every layer the
Charter v1.1 envelope promises — entities, metrics, joins — now ships
with a `suggest` LLM authoring surface, a store-only `inspect`
browser, drift detection via `check`, and optional OpenTelemetry span
emission so existing observability stacks see every MCP tool call.
The one-command Docker demo stack lands alongside.

### Added
- `schemabrain metrics suggest` subcommand — LLM-driven metric
  suggestion to match the existing `entities suggest` and
  `joins suggest` surfaces. Reads the local entity store, sends the
  schema slice and bound tables to Claude (Anthropic), and emits one
  `Metric` per response candidate with confidence + rationale.
  Three apply modes: `--dry-run` (preview), `--out-dir DIR` (write
  YAML sidecars), `--apply` (write straight to the store with
  per-candidate confirmation). `--top-k N` caps the candidate set;
  `--max-cost-usd` plus `SCHEMABRAIN_MAX_LLM_COST_USD` enforce a
  spend ceiling. Refuses on empty entities, unknown anchor entity,
  empty stub response, out-dir conflicts, missing API key, malformed
  cost env var, and unwritable out-dir — each with a guided error and
  exit 2.
- `schemabrain inspect [<name>]` subcommand — store-only schema
  browser. Bare `inspect` renders a summary of every indexed table,
  entity, metric, and canonical join. `inspect <name>` drills into
  one entity with full columns (including PII sensitivity + category
  tags), related entities (bidirectional join traversal, with
  `outgoing` / `incoming` direction labels and cardinality), and
  anchored metrics. No LLM, no live source — pure local store
  reader. Exit codes: `0` rendered, `1` drilled name not found,
  `2` operational refusal. When no `--source` / `--url-env` is
  supplied, drilling walks every source the store knows about and
  renders every match — the common one-source case stays
  flag-free.
- `schemabrain check` subcommand — walks every persisted entity,
  metric, and canonical join and reports drift against the live source
  schema. Detects five drift kinds: `table_missing`,
  `identity_column_missing`, `measure_column_missing`,
  `time_dimension_column_missing`, and `join_column_missing`. Drift
  cascading is suppressed — when an entity's bound table is missing,
  downstream metric and join drifts on that table are NOT
  double-reported so the output stays focused on the root cause.
  Live-source introspection is cached per run so a 50-metric run
  against 10 tables hits the source 10 times, not 50. Exit codes:
  `0` (clean), `1` (drift), `2` (operational refusal). `--json`
  emits a parseable contract `{drifts, summary, exit_code}` to
  stdout for CI / monitoring scripts.
- `docker-compose.yml` at the repo root: one-command demo stack
  (Postgres 16 + bundled fixture loader + Schema Brain indexer). The
  stack reaches `Done` with a populated store on a named volume
  (`sb-data`) that survives `docker compose down`. README's "Run via
  Docker" section documents the `docker run`-based MCP host wiring
  on top of the demo volume.
- `schemabrain[otel]` optional extra — emits one OpenTelemetry span
  per MCP tool call when both the extra is installed AND
  `OTEL_EXPORTER_OTLP_ENDPOINT` is set. Spans carry `gen_ai.*`
  semantic-convention attributes plus schemabrain-specific result
  facets (matches, columns, paths, rows, fingerprint). Span name is
  `execute_tool`; status maps to OTel `OK` for the four success
  envelope variants and `ERROR` for `error` / `refused`. OTLP/HTTP
  exporter speaks to Langfuse, Phoenix, OpenLIT, otel-tui,
  otel-desktop-viewer, Datadog — see `docs/observability.md` for
  per-backend recipes. Off by default; zero overhead when the
  endpoint env var is unset. Without the extra, `pip install
  schemabrain` works identically.
- `entities list` subcommand — completes the symmetry with
  `joins list` and `metrics list`. Renders the alphabetised entity
  catalog for a given source.

### Changed
- Version bump to `0.3.0` — the v1 semantic-layer arc end-state.
  Per the locked versioning policy in
  `project_versioning_policy.md`, `1.0.0` waits until the MCP / CLI /
  Store-Protocol surface has been used in anger by external users
  without a forced break. The roadmap milestone "v1" and the semver
  number `1.0.0` are deliberately decoupled.
- `Development Status` PyPI classifier bumped `2 - Pre-Alpha` →
  `3 - Alpha`.
- `audit list --limit` now rejects negative values at parse time. SQLite
  treats `LIMIT -1` as "unlimited", which silently returned the entire
  audit history when a user typo'd a positive number. Argparse converter
  blocks any value below zero with a clear message.
- `audit list` and `audit verify` read paths now warn on schema-version
  drift. The read paths open the store with raw `sqlite3.connect` to
  bypass SQLiteStore's strict `SchemaVersionMismatchError`; a future-
  version store or tampered meta row was rendering with no signal that
  drift had occurred. New `_warn_on_schema_drift` helper distinguishes
  three cases: meta-table missing (silent — pre-v11 store), meta-row
  missing (warn), and version mismatch (warn with capped echo).
- `UnknownMetricError.metric_name` echo is now capped at 200 chars.
  Closes a 100 KB context-window-exhaustion vector where an agent
  passing a hostile long metric name would have flooded the error
  message into the response envelope.

### Fixed
- `schemabrain index` no longer crashes on Postgres column types
  without a built-in equality operator (`xml`, `tsvector`, `point`,
  `line`, `lseg`, `box`, `path`, `polygon`, `circle`, plus any type
  SQLAlchemy reflects to `NullType`). The batched profile query
  previously emitted `COUNT(DISTINCT col)` for every column and
  raised `psycopg.errors.UndefinedFunction` on first encounter with
  any such type. AdventureWorks-for-Postgres has 7 xml columns in
  base tables; the pre-tag smoke against it surfaced the bug. For
  affected columns the profiler now substitutes `NULL::bigint` for
  the DISTINCT slot and falls back to `distinct_count = non_null` (a
  max-cardinality hedge that's more useful to the downstream LLM
  cardinality prompt than a misleading zero). The connector's
  `inspector.get_columns()` call also gains a narrow SAWarning
  filter so the "Did not recognize type X" warning doesn't crash
  the test suite under `filterwarnings = error`.
- Self-join refusal in `CanonicalJoin.__post_init__` now surfaces an
  actionable workaround in the error message — "model each side as a
  separate entity (e.g. `manager` and `direct_report`) and define
  the canonical join on the FK column from one side" — replacing
  the stale "deferred to v1 wk-15" roadmap reference. 10 internal
  `wk-15` comment references cleaned up across `schemabrain/core/
  join.py`, `schemabrain/core/store.py`, `schemabrain/core/store_
  protocol.py`, `schemabrain/joins/suggest.py`, `schemabrain/joins/
  yaml_grammar.py`, and three test files.

### Documentation
- `docs/observability.md` expanded with OTel integration section:
  span shape, attribute map, status mapping, per-backend recipes
  (Langfuse / Phoenix / OpenLIT / otel-tui), and limits (orphan
  spans, no args on spans, semantic-conventions migration risk).
- `docs/adr/` expanded from one to four ADRs:
  - `0002-store-protocol-seam.md` — Store as the universal write
    substrate seam across v1 (SQLite local) and v3 (hosted Postgres).
  - `0003-versioning-policy.md` — strict-semver interpretation;
    `1.0.0` waits for battle-tested API.
  - `0004-observability-event-bus.md` — the JSONL event bus as
    three-consumer substrate (tail / audit / OTel), with the OTel
    emission decisions locked.
- `docs/internal/manual_smoke_2026_05_18.md` captures the pre-tag
  manual production-DB smoke that surfaced the two `### Fixed` items
  above. Walks the v0.3.0 wheel against Pagila + Northwind +
  AdventureWorks + synthetic PII mockup + reserved-keyword synthetic
  through the full new-user journey. Reference for future smoke runs.

## [0.2.0a1] - 2026-05-15

### Added
- PEP 561 `py.typed` marker shipped in the wheel. Schema Brain's
  source carries full type annotations on every public function
  signature; the marker tells downstream type checkers (mypy,
  pyright, pyrefly) to use them. Without it, the checkers silently
  treated imports as untyped.
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
- `pytest` lower bound bumped to `>=9.0.3`, dropping the
  `--ignore-vuln CVE-2025-71176` suppression from the `pip-audit` step.
  pytest 9.0.3 fixes the local-DoS in `/tmp/pytest-of-{user}` directory
  naming. `pip-audit --strict` now runs with no suppressions; any
  future CVE in any dep will fail the gate.

### Changed (docs)
- `SECURITY.md` SLA language softened from "We commit to" to "we aim
  to" / "we target", with a note that any slip will be published on
  the advisory thread. Solo, part-time maintenance makes hard
  commitments risky to put in public policy. The day-counts are
  unchanged.
- `SECURITY.md` "In Scope" no longer claims "fingerprint integrity
  guarantees" — that's a v2 feature not shipped at `0.1.0a1`. Now
  reads "PII redaction in sample values written to the local SQLite
  store", which is what actually ships today.
- `SECURITY.md` "Out of Scope" adds an explicit clause excluding
  build-time-only dependency compromise that doesn't materially
  affect the published PyPI wheel. Closes a wording gap that could
  have been read as inviting reports on every Dependabot-quiet
  week.

### Changed
- `schemabrain mine-queries` now filters Schema Brain's own profiler
  SELECT statements out of the mined `example_queries` set.
  Previously, running `mine-queries` against a Postgres that was also
  indexed surfaced Schema Brain's own profiling chatter (positional-
  alias counts queries and `::text AS v` value samplers) alongside
  real user workload, polluting what `get_example_queries` returned
  to agents. The filter is narrow — joint signatures only, so
  realistic user shapes like
  `SELECT DISTINCT status::text AS v FROM orders LIMIT 100` are
  preserved. Skipped statements emit a DEBUG breadcrumb so
  contributors can trace missing rows.
- sqlglot's WARNING-level "Falling back to parsing as a 'Command'."
  notices are silenced inside the mining module.
  `pg_stat_statements` surfaces non-DML statements (`SHOW`,
  `CREATE EXTENSION`, `SET`) from any session's connection setup;
  the pipeline already drops non-DML via type filtering, so the
  warnings were pure noise. Real parse failures still surface as
  exceptions.
- `suggest_joins` default `max_hops` raised from 4 to 6. The bundled
  e-commerce fixture's longest reachable pair (`users → orders →
  order_items → products → product_categories → categories`) is 5
  hops, so the previous default reported it as unreachable —
  surprising for users walking the documented demo. 6 covers M:N
  junction-table chains common in normalised OLTP schemas with one
  hop of headroom while staying below the threshold where BFS
  exploration becomes expensive on wide FK graphs.
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

### Fixed
- `IndexResult.summary()` no longer prints
  `LLM: 0 descriptions ($0.XXXX)` on a cache-hit re-index. The
  enrichment pipeline initialises `spent_usd` from the persistent
  ledger so the in-memory cost-cap check covers historical spend,
  then accumulates new spend on top; the end-of-run value carrying
  the cumulative total leaked into the per-run summary clause,
  making a zero-cost re-index look like it spent money. The
  clause now scopes to runs that actually generated descriptions.
  A separate branch surfaces the rare anomaly path (LLM calls
  billed but no descriptions landed) with a "check logs" hint so
  unaccounted spend remains visible.

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

[Unreleased]: https://github.com/Arun-kc/schemabrain/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/Arun-kc/schemabrain/compare/v0.2.0a1...v0.3.0
[0.2.0a1]: https://github.com/Arun-kc/schemabrain/releases/tag/v0.2.0a1
[0.1.0a1]: https://github.com/Arun-kc/schemabrain/releases/tag/v0.1.0a1
