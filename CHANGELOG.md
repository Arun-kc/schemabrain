# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.6.0] - 2026-06-13

**Highlights** — the marketed launch. The dashboard grows from 4 surfaces into a **graph-led, 9-surface** experience: a signature **Knowledge Graph**, an **Overview** home, an **Entities** index, a **Data Dictionary**, an editable **Policy** editor, and a **Drift** view join the PII / Refusals / Audit trio. Audit logs are now **browser-verifiable** via a derived Merkle root, the marketing **landing moves to a standalone site**, and the product is repositioned from "SQL firewall" to the **trust + intelligence layer**. A zero-setup `schemabrain demo` command tells the whole story offline in seconds, the PII firewall now refuses **grouping by** a PII column as row-level disclosure, and `import dbt` imports `relationships` tests as canonical joins.

> **Upgrade note** — this release migrates the store schema (`SCHEMA_VERSION` 14 → 15) to persist the graph projection; it applies automatically and crash-atomically on first open (chaining v13 → v14 → v15), no manual step. The project is now **Apache-2.0** licensed. Install the dashboard with `pip install schemabrain[ui]`; `schemabrain dashboard` still binds to `127.0.0.1` only.

### Added
- **Knowledge Graph surface** (`/graph`) — a persisted, read-only graph projection of entities and their joins with edge cardinality, three-state PII node levels, refusal-hotspot attribution, and a floating-panel canvas; backed by `GET /api/graph`. ([#202], [#203], [#204], [#205], [#208])
- **Overview home surface** + `GET /api/overview` aggregate — the dashboard's new landing surface. ([#218])
- **Entities surface** — a sortable index plus a drilldown sheet showing columns, PII, metrics, and canonical joins. ([#206])
- **Data Dictionary** — a `/dict` dashboard surface + `GET /api/dict` with byte-for-byte Markdown export parity, and a `schemabrain docs` generator CLI that dogfoods the same model. ([#192], [#207])
- **Editable Policy editor** (`/policy`) — the handoff-exact 3-way block / redact / allow grid, scaled to multi-table schemas (collapsible table groups, search/filter, PII-only default) with always-on PII-floor disclosure. ([#195], [#196])
- **Drift surface** (`/drift`) + `GET /api/drift` — surfaces config and enrichment drift the store can verify, with copy-the-CLI actions. ([#197])
- **Browser-verifiable audit** — a derived Merkle root + per-row inclusion proofs (RFC-6962); "Verify" runs both a whole-log chain walk and per-row proofs. ([#201])
- **Column × category PII-confidence heatmap** + an index-time PII-confidence band (advisory, never gates). ([#198], [#199])
- **Refusals timeline ledger** — a protective-framed view of what was refused and why. ([#200])
- **Standalone marketing site** — the landing page moves out of the wheel into a separate Vercel app; the shipped dashboard roots at `/overview`. ([#219])
- **Dashboard design system** — dual-theme (light/dark) oklch tokens, self-hosted fonts, a shared component kit, and an app shell. ([#183], [#184], [#186])
- `schemabrain init` now leads with the knowledge-graph payoff, and `--help` is uvx-first. ([#226])
- **`schemabrain demo`** — a zero-setup command (no Docker, API key, or Postgres) that builds an offline SaaS store, knowledge graph, and seeded audit chain, then offers a dashboard, terminal showcase, or host-wiring payoff via a guided menu. ([#233])
- **dbt `relationships` → canonical joins** — `import dbt` now turns generic `relationships` schema tests into `dbt_import`-origin canonical joins (single-column, idempotent, FK-safe — both endpoint entities must be imported), completing the dbt-import path beyond entities. ([#237])
- **Type- and nullability-aware drift** — `schemabrain check` adds `type_mismatch` and `nullability_change` drift kinds compared against the indexed column snapshot, closing the existence-only silent-correctness gap; additive (reports drift without cascade-suppressing dependents). ([#236])
- **`init --enable-sonnet`** — the opt-in two-tier router (Sonnet 4.6 for cryptic column names, Haiku 4.5 otherwise) is now reachable from the onboarding wizard, with the same off-by-default semantics as `index --enable-sonnet`. ([#235])
- `schemabrain init`'s closing "next steps" now points to `--emit-yaml-dir` when no editable YAML was written, so an operator can find the `pii_policy.yaml` + entity/metric/join YAML to edit. ([#240])

### Changed
- **Repositioned from "SQL firewall" to the trust + intelligence layer**, with the firewall demoted to one of six proof-points; a canonical positioning source (`positioning.py`) + sweep engine keep the public surface consistent. ([#189], [#190], [#191], [#217], [#221])
- **License: MIT → Apache-2.0.** Added a `NOTICE` file and switched contribution sign-off to the [Developer Certificate of Origin](https://developercertificate.org/) (DCO; `git commit -s`). Apache-2.0 adds an explicit patent grant and patent-retaliation clause; the permissive terms are otherwise unchanged. ([#179])
- **Store schema v15** — persisted graph-projection tables, reserved semantic/PII columns, `entities.group`, plus entity confidence/rationale and table row-count writers. ([#181], [#194])
- Sidecar entity routes enriched (drilldown detail + list rollup). ([#193])

### Security
- Dashboard CSP hash-pins `script-src` and drops `'unsafe-inline'`. ([#216])
- `--enrich` withholds sample values and value-shape hints for PII-classified columns (gated on the name-based classifier). ([#214])
- `pyjwt` bumped to 2.13.0 (4 CVEs). ([#187])
- `sqlglot` pinned `<27` — 30.x renamed an AST arg key and silently emptied join-mining; added a version regression tripwire and a lock-free install smoke. ([#224], [#225])
- `get_metric` refuses grouping **by** any PII-tagged column as row-level disclosure, independent of policy — mirrors the existing MIN/MAX raw-value guard; aggregating over or filtering by a contact column still answers. MCP server instructions tightened: raw-row access is out of scope by design, and the server never emits or offers SQL for refused or absent data. ([#233])

### Fixed
- The Policy editor's middle verb is labelled "open", not "redact". ([#220])
- Confidence is derived (not asserted) for query-log + introspection tools, with explicit provenance. ([#224])
- The `init` wizard no longer misclassifies a user's own Postgres as the bundled demo — demo detection probes the SaaS-specific `workspaces` table instead of suffix-matching the demo URL tail. ([#234])
- `import dbt` on a never-indexed store now fails fast with a guided "run `schemabrain index` first" pre-flight instead of a per-model foreign-key error. ([#234])
- Quieted the Hugging Face download progress bar that printed even when the embedding model was already cached. ([#233])
- `schemabrain index` preserves operator PII overrides (`origin='operator'`) across a re-index — it now re-classifies only the columns the operator has not asserted on, instead of silently discarding hand-applied false-positive fixes when a schema change triggers a re-index. ([#240])

### Documentation
- **Code of Conduct** (Contributor Covenant 2.1) wired into README / ROADMAP / CONTRIBUTING. ([#226])
- ADRs 0005–0011 (dashboard routing, read-only Apply, drift actions, policy control model, trust data contract, graph projection). ([#180], [#196], [#197], [#198], [#202])
- README hero recast around the pain hook; pre-launch community on-ramp + surface-count accuracy. ([#215], [#217])
- Claim-truth sweep from the full-scope E2E audit: SQLite framed as a roadmap **source** connector (the local store is SQLite; no SQLite source connector ships yet), per-column enrichment cost `~$0.0004`, `--max-cost` default `$1`, audit framed best-effort, two-tier routing opt-in, ROADMAP at 9 surfaces / v0.6.x; 68 broken `mechanism/*` doc links fixed and ADRs 0006–0012 registered in `docs.json`. ([#234])
- ADR 0012 — group-by-PII row-level disclosure — plus threat-model, PII-taxonomy, security, and semantic-layer updates. ([#233])
- New **Your project** guide (the files `init` creates, the editable YAML tree behind `--emit-yaml-dir`, the edit → apply → restart loop, the `.env` model, and the least-privilege DB-role note), wired into Get started and linked from the README quickstart. Fixed the semantic-layer worked example (added the missing `customer` entity), reconciled the YAML layout to `./schemabrain/…` across docs, added a dashboard section to `operations.md`, and corrected the env-var / `--url-env` framing in the CLI reference. ([#240])
- **Demo recordings** — three reproducible `vhs` tapes/GIFs (firewall showcase + live and curated CLI tours); the curated CLI tour is embedded in the README. ([#241])
- **Dashboard screenshots** refreshed to the current dark-theme UI across all nine surfaces; the PII surface renamed **PII Ledger → PII matrix** to match the UI (old docs URL redirected); a code-grounded accuracy pass corrected stale dashboard claims (graph cardinality, audit verify ribbon / in-browser proofs / SSE, refusal recovery field). ([#242])
- Pre-release docs polish — a dedicated **Knowledge Graph** dashboard page (`/dashboard/graph`), an `examples/` index, a normalized PII-capability label, and a CONTRIBUTING note on the two-app (`web/` vs `site/`) frontend layout. ([#243])

### Internal
- Visual-regression baselines in a pinned Playwright container + web performance budgets; a 9-surface axe a11y sweep + AA-contrast fixes; dependency bumps. ([#209], [#210], [#212], [#213], [#222], [#223])

## [0.5.0] - 2026-06-01

**Highlights** — the launch release: a read-only **dashboard** (`[ui]` extra), an **editable PII enforcement policy**, a substantially **hardened SQL firewall**, a zero-config **SaaS demo pack**, and a full **Mintlify docs site**. The publish pipeline is fixed so the wheel actually ships the dashboard.

> **Upgrade note** — no store migration (`SCHEMA_VERSION` stays `14`). Install the dashboard with `pip install schemabrain[ui]`; `schemabrain dashboard` binds to `127.0.0.1` only.

### Added
- **Read-only dashboard** (`[ui]` extra) — local FastAPI sidecar + static Next.js UI via `schemabrain dashboard` (`127.0.0.1` only): schema/entity browser, PII Ledger, Refusal UI, Audit Viewer, Boardroom Brief; entity drilldown shows metrics + canonical joins. ([#125], [#126], [#127], [#129], [#130], [#132])
- **Editable PII policy** — `schemabrain policy {show, apply, tag}` + a `pii_policy.yaml` overlay + a read-only dashboard view; the catastrophic-leak floor is always-on and can't be overridden away. ([#155])
- **SaaS demo pack** (new bundled default) — 12 tables / 84 columns / 12 entities / 5 metrics / 8 joins covering all three catastrophic-PII legs; `init` applies it for $0 with no API key. Bundled packs are now a named registry (e-commerce stays as fallback). ([#143], [#164], [#167])
- `schemabrain doctor --verify` — no-API-key mock-agent MCP smoke + environment preflight. ([#116])
- `schemabrain init` host selection (Claude Desktop / Code / Cursor / Windsurf) with detection; `--host manual` / `--print-only` prints the snippet without writing. ([#115], [#146])
- `serve` query guardrails — `--statement-timeout-ms` (30s) and `--max-rows-per-result` (10000); `0` opts out. ([#116], [#151])
- Store ↔ YAML round-trip — `entities`/`metrics`/`joins` `export[-all]`, `schemabrain apply`, `schemabrain diff` (CI exit codes), `init --emit-yaml-dir`, and public `*_to_yaml` serialisers. ([#113])
- `audit verify --since <spec>` (hex-prefix / duration / ISO cursor) and an `audit list` status + cost-class footer. ([#112])
- `doctor` probes `pg_stat_statements` (advisory). ([#145])

### Changed
- Agent steering moved into the MCP `initialize` `instructions` field (no user-pasted snippet); interactive `--pii-block` default aligned with `--yes` + docs. ([#142])
- `get_metric` validates `limit` in-body (typed `malformed_name` envelope) and reports a `truncated` flag; the metric executor uses a `NullPool` engine. ([#117], [#165])

### Security
- Catastrophic-leak floor (`credential`, `payment_card`, `government_id`) enforced at every read path including the `get_metric` aggregate path; operator overrides can't strip it. ([#154], [#156], [#157], [#162])
- Catastrophic column **names** no longer disclosed via `redacted_columns` or the unknown-column hint. ([#174])
- PII classifier hardened — auth-secret + internationalised + concatenated/abbreviated shapes; `RULE_COUNT` 46 → 60. ([#152], [#158], [#161])
- `serve` rejects control chars in quoted identifiers, refuses `MIN`/`MAX` over PII, fails closed on untagged columns; redaction centralised. ([#150], [#153], [#154])
- Safe-by-default `--pii-block` across `serve` / `init` / `build_server` / `WizardConfig` (catastrophic-leak set; explicit `''` to disable). ([#110], [#162])
- Pinned the Hugging Face Hub model revision (B615 / CWE-494); added a 19-file firewall-bypass regression corpus. ([#147], [#149])

### Fixed
- `get_metric` refusal envelope surfaces only `blocked_categories` (no probe oracle); `describe_entity` always redacts catastrophic column descriptions. ([#110])
- PII verdicts labelled by attribution (`floor_blocked` vs operator policy). ([#160])
- Publish pipeline builds the dashboard export with `uv build --wheel`, so the wheel ships it and advertises `[ui]`. ([#163])
- Deterministic dashboard PII-category ordering; closed 7 launch-blockers via firewall hardening + `fastembed` reliability. ([#132], [#147])

### Documentation
- Full **Mintlify site** — mechanism explainers, per-client setup (Claude Desktop / Code / Cursor / Windsurf / Zed / Codex), comparisons, Works-with + security posture, threat model, First 5 Queries, dashboard guide, CLI reference. ([#118], [#120], [#121], [#122], [#123], [#124], [#133], [#135], [#136], [#140], [#144], [#145])
- Docs recast onto the SaaS demo; store-path default corrected to `./schemabrain.db`; README + substrate fact-check and link repair. ([#137], [#138], [#141], [#166], [#172], [#173])

### Internal
- Bundled-pack registry refactor; stale-comment / attribution hygiene; dependency bumps (`dorny/paths-filter` 3 → 4, `opentelemetry-sdk`). ([#104], [#106], [#111], [#119], [#148], [#164])

## [0.4.0] - 2026-05-23

**Highlights** — 0.4.0 lands the charter v1.2 2D trust signal,
composite-expression measures, junction-table bridges, column-granular
PII redaction, partition-parent FK union (Pagila pattern), a dedicated
`metrics show` CLI drill, and the README repositioning around the
SQL-firewall framing. Install substrate hardened so the wizard launches
the same code that ran it regardless of how schemabrain was installed
(PyPI / wheel / editable / VCS).

### Added
- `schemabrain metrics show <name>` — namespaced drill into one metric. Renders the same `MetricDetail` view that `inspect <name>` does, but skips the entity → metric → join priority cascade so an operator who knows they want a metric is not shadowed by an entity / join sharing the same name. Cross-source posture matches `inspect`: without `--source` walks every source the store knows about; missing name exits 1 with a next-step hint. ([#101])
- Charter v1.2: 2D trust signal (`Provenance.inference_method` × `Provenance.validation_state`) replaces hardcoded `confidence="HIGH"` on every entity / metric / join producer; surfaces on Pydantic summaries, the `inspect` drill, and `entities` / `joins` / `metrics list`. ([#95])
- Composite-expression measures via strict whitelist grammar — `MetricMeasure.expression` parses through `ast.parse` with a node-type whitelist; SQL injection surface closed by construction. Bundled `total_revenue_real.yaml` fixture. ([#91])
- `get_metric` accepts a `time_dimension` arg to disambiguate inheritance; resolver BFSes the canonical-join graph for reachable timestamp columns over non-fan-out edges. ([#95], [#96])
- Junction-table bridge synthesis on read — `list_joins` / `inspect` surface M:N bridges through junction entities. ([#95])
- Column-granular PII redaction in `describe_entity` (was whole-entity); init wizard prompts the operator to choose PII-block categories (default `contact`). ([#95])
- FK-inferred cardinality at `joins suggest` time + direction-aware effective cardinality on `ResolvedJoin` (reverse-walk hops flip cardinality correctly). ([#95])
- 5 new PII-classifier rules for medical (`encounter_id`, `npi`, …) and blockchain (`wallet_address`, `private_key`, …) schemas; `RULE_COUNT` 41 → 46. ([#94])
- `schemabrain inspect <name>` resolves entity → metric → join in priority and drills through metrics + joins. ([#95])
- MCP server `icons` (32 / 64 / 512 PNG) + `website_url` in the `initialize` response. ([#95])
- Compile-time `unknown_measure_column` envelope. ([#91])
- Store schema bumps: 12 → 13 (composite-expression column with XOR CHECK) and 13 → 14 (`inference_method` + `validation_state`; first in-place migration). ([#91], [#95])

### Changed
- README sample session rewritten against the bundled fixture (refusal + pivot, not the fictional `product_categories` junction). ([#100])
- README cost anchor corrected `~$0.01 → ~$0.03` (Haiku + Sonnet split named); firewall property #4 narrowed to the `get_metric` boundary; hero tagline gains "against your database" qualifier. ([#100])
- README leads with SQL-firewall positioning and a 2×3 firewall property grid; `examples/anthropic_demo.py` promoted as the 5th firewall property. ([#90], [#97])
- README lead paragraph tightened; `docs/mcp-tools.md` tool count synced `10 → 12`. ([#98])
- Charter doc bumped 1.1.0 → 1.2.0; MCP `CHARTER_VERSION` 1.1 → 1.2 (wire-compatible with v1.1 / v1.0 clients). ([#95], [#99])
- YAML grammar: `measure.expression` is a valid alternative to `measure.column` (XOR). MCP `MetricSummary.measure_column` becomes `str | None`; new `measure_expression: str | None` field. ([#91])
- PII propagation walks every column the measure expression touches (was only `measure.column` — composite expressions previously bypassed `--pii-block` on tagged-but-unwalked operands). ([#91])
- `get_metric` auto-fills `ORDER BY` with the group columns when caller omits `order_by`; `missing_order_by_with_limit` degradation removed from emit path. ([#96])
- Domain-agnostic LLM system prompts (placeholder column / table names, cross-domain `pii_hints` examples). No behavioural change for e-commerce schemas; reduces consumer-data prompt bias for medical / financial / blockchain. ([#94])
- Wizard `_resolve_runner` detects non-PyPI installs via PEP 610 `direct_url.json` and falls back to the absolute-path runner. ([#100])
- `MetricResult.fingerprint` docstring documents ADR-0001 privacy-by-construction (repeat-prefix is by design, not a hash collision). ([#96])

### Fixed
- Postgres partition parents whose FKs sit on the partition children (the Pagila pattern) now surface those FKs on the parent's `Table.foreign_keys`. New `_get_partition_child_fks` queries `pg_constraint` directly (bypassing SQLAlchemy's `referred_schema=None` ambiguity for cross-schema FKs into the default search_path), unions with the parent's own FKs, and de-dupes by identity tuple so cleanly-built schemas don't double-count. ([#102])
- Reverse-traversal cardinality flip missed same-name-FK joins (e.g. `rental.customer_id ↔ customer.customer_id`), silently over-counting on grouped aggregates — Pagila re-test reported 182 customers where ground truth was 158. Replaced heuristic with explicit `is_reverse_traversal` flag on `_ChainEdge`. ([#95])
- Wizard tagged LLM-suggested entities + metrics as `manually_authored`, collapsing the 2D trust signal to flat HIGH on the wizard happy path. ([#96])
- MCP refusal envelopes left `recovery.suggested_args` null even when the message named a structured arg — now populated for `ambiguous_time_dimension` and `pii_blocked`. ([#96])
- `serverInfo.version` leaked the `mcp` SDK package version (`1.27.1`) instead of `schemabrain.__version__`. ([#96])
- `schemabrain audit list` empty-state was ambiguous (empty audit log vs filtered-out rows); empty branch now says so explicitly with a `next:` hint. ([#96])
- Bridge join names truncated with `…` on narrow terminals in `inspect <entity>`; `On` column now wraps via `overflow="fold"`. ([#96])
- Measure-expression parser rejects non-finite float literals (`1e500 → inf` / `nan`) and literal-only expressions (`100`, `1 + 2`) at parse time. ([#92])
- `MetricMeasure.column is None` now handled at every consumer site (`check`, `inspect`, `cli metrics list` / `audit` / `suggest --apply`). ([#92])
- LLM-suggest path accepts composite-expression candidates; `MalformedMetricRowError` preserves the metric name through the MCP envelope (was reduced to bare `internal_error`). ([#92])
- `examples/anthropic_demo.py` spawned the MCP server via `uv run schemabrain serve` despite documented `pip install` path — changed to `command="schemabrain"` directly. ([#100])
- `schemabrain metrics list` empty-state hint mirrors `list_metrics` (next-command hint, not dead-end). ([#91])

## [0.3.0] - 2026-05-20

**Highlights** — SchemaBrain v0.3.0 is the first release where the
"pluggable semantic+SQL firewall for agents" positioning is honest
end-to-end. Ships: validated-SQL `get_metric` (the agent never sees
or writes SQL); tamper-evident `mcp_audit` append-only chain with
`audit verify`; 12-category PII classifier (GDPR / CCPA / HIPAA /
PCI DSS / ISO 27018) with `--pii-block` refusal at the MCP boundary;
10 MCP tools (5 physical-schema + 5 semantic-layer); 7-stage
`schemabrain init` activation wizard with optional auto-Docker
demo path; `schemabrain check` drift detection; dbt import path
making dbt the source of truth when present; Docker + multi-
platform image; OpenTelemetry export via `schemabrain[otel]` extra;
design-system CLI with Rich brand-line + glyph vocabulary across
every operator surface.

Sub-sections below preserve the development order of the two
landing phases (the post-2026-05-18 polish bundle on top, the
original v1 semantic-layer arc that landed PRs #48-#52 below).
Future releases will consolidate to a single Added/Changed/Fixed
trio per release.

### Changed
- **`inspect` + `index --dry-run` polished onto the design's
  brand-line + panel vocabulary** — final design-shape PR in the
  CLI design-system migration arc (PR #7; PRs #71/#72/#73 +
  #4/#5/#6 prior). Three surfaces upgraded in one bundle:

  - **`schemabrain inspect`** summary replaces the `SchemaBrain
    inspect` plain header with `◆ store · <path>` brand line.
    Below the existing Definitions Tree, a balanced
    3-Panel grid (`entities · N` / `metrics · N` / `joins · N`)
    always renders — empty categories show `(none yet)` body so
    the grid teaches operators what they don't have yet rather
    than collapsing to a confusing solo panel.
  - **`schemabrain inspect <entity>`** drill replaces
    `Entity: <name>` + dashed rule with brand line
    `◆ <qualified_table> · entity:<name> · binding <identity>`,
    surfacing the entity's identity essentials on one scan line.
    Non-manual origins (`suggested`, `dbt_import`) render an
    additional `· origin <kind>` segment so provenance stays
    visible.
  - **`schemabrain index --dry-run`** replaces
    `console.rule("Dry-run: <url>")` with brand line
    `◆ plan · [--since X ·] N tables`. The 6-line k/v grid wraps
    into a Rich `Panel`. The panel title adapts to the run mode:
    `✓ plan summary` for cost-free dry-runs (so the title doesn't
    mislead readers into expecting $ rows), `✓ cost estimate ·
    haiku` for `--enrich` runs where LLM cost is the load-
    bearing signal. The freshness audit line now leads with the
    `→` arrow glyph so it reads as a next-action breadcrumb.

  New shared helper `schemabrain._ui.short_path(p)` collapses
  `$HOME` prefixes to `~/` in display paths — consistent across
  the doctor brand line (PR #4) and the new `inspect` store
  brand line + dry-run `store` row, so terminal recordings + CI
  logs + support screenshots no longer leak the operator's OS
  username. New `_compose_dry_run_panel_body` helper extracts
  the cost-estimate grid construction into an independently-
  testable pure function (`schemabrain/cli.py`).

  The `--quiet` legacy pipe-delimited format for `index
  --dry-run` is **unchanged** — CI scripts grepping `"Would
  index"` / `"Stale since"` / `"| source="` substrings stay
  working. Only the Rich-rendered (non-quiet) path takes the
  design shape.

  Tests in `tests/test_inspect_render.py` (+199 LOC, 14 new
  layout pins covering brand lines + 3-panel grid + empty-state
  body + `(none yet)` body + non-manual origin), new
  `tests/test_dry_run_panel.py` (9 tests on the
  `_compose_dry_run_panel_body` helper covering row suppression
  + singular/plural grammar), `tests/test_ui_primitives.py` (+5
  tests pinning the `short_path` `$HOME` collapse). 11 existing
  test assertions flipped from old shape to design vocabulary
  (`SchemaBrain inspect` / `Entity: customer` / `Dry-run:` /
  `Est. cost:` / `Stale since` (Rich path only) → `◆ store` /
  `◆ public.users · entity:customer` / `◆ plan` / `est. cost`
  (lowercased) / `→ freshness audit` (Rich path only)).

- **`schemabrain init --help` re-rendered onto the design's
  grouped help surface** — fourth operator-visible win from the
  design-system migration arc (PR #6; PRs #71/#72/#73 + #4 + #5
  prior). Replaces argparse's plain-text 84-line dump with the
  design's structured shape (handoff bundle
  `cli/operator.jsx:InitHelp`):

  - A cyan brand line `◆ schemabrain init — N-stage activation wizard`
    where `N` is derived from `setup.wizard.DEFAULT_STAGES`.
  - A three-line preamble (`usage` / `stages` / `runtime`). The
    stage chain reads live from `DEFAULT_STAGES` so a wizard
    reshape auto-propagates into the help screen.
  - Five argument-group blocks (`SOURCE` / `STAGES` / `HOST` /
    `COST` / `BEHAVIOR`), each with a dim label + one-line
    purpose and a dashed rule separating it from the flag
    table. Flag rows render via `Table.grid` so long help
    text soft-wraps under the help column with the right
    indent.
  - An `examples` block listing three representative
    invocations.
  - A `→ see also: schemabrain --help · schemabrain doctor`
    breadcrumb at the bottom.

  Implementation reorganises the 14 `init` flags into
  `argparse.add_argument_group` calls keyed on the design's
  grouping; argparse's `Namespace` shape is unchanged
  (argument groups are display-only). A new
  `_GroupedInitHelpAction` (subclass of `argparse.Action` with
  `**kwargs` forwarding for stdlib forward-compat) replaces
  argparse's default `-h/--help` action and routes both flags
  through the design renderer. Flag-help strings tightened per
  UX feedback so each fits a single terminal line at the
  design's 88-col flag-description budget.

  New module `schemabrain/init_help_render.py` (107 LOC, 100%
  line + branch coverage) hosts the renderer. A new shared
  `_ui.console_render_width` helper consolidates the
  detected-width-with-soft-cap logic both this module and
  `setup/doctor_render.py` use (`_grid_width` removed from
  doctor_render in favour of the shared helper). The renderer
  emits a `UserWarning` at render time when `cli.py` registers
  a titled argument group without a `description=` kwarg, so a
  contract violation that would silently hide flags from the
  help surface surfaces in development.

  Tests in `tests/test_init_help_render.py` (47 tests) pin
  every flag's group membership, the brand-line/preamble/group
  block layout contract, the help-action wire-up (`-h` and
  `--help` both invoke the new renderer), and the dropped-
  group warning. The `parametrize` on
  `test_flag_lives_in_expected_group` ensures any future flag
  group churn fails loudly rather than appearing on the help
  screen wrong.

- **Two error surfaces re-rendered onto the design's panel
  vocabulary** — third operator-visible win from the design-
  system migration arc (PR #5; PRs #71/#72/#73 + #4 prior). The
  third design shape (LLM 529 advisory) is deferred to a follow-
  up PR — it requires new exception-catching plumbing inside the
  wizard / `entities suggest` flow beyond a visual upgrade.

  - **Shape A — bad input** (handoff bundle
    `cli/errors.jsx:ErrBadInput`): the `--since wednesday`-style
    parse-error path now renders a caret-pointer surface
    reproducing the user's command line with a `^^^` underline
    under the failing token and a `└─ <reason>` leader. A
    "did you mean" sub-block lists two corrected commands. The
    caret leader reflects the actual `parse_since` failure mode
    — duration-vs-date confusion renders `not a duration · not
    a date`; an ISO 8601 timestamp without a timezone renders
    `ISO 8601 needs a timezone (e.g. trailing Z)`. Previously
    rendered as a plain `error: --since: ...` print to stderr.

  - **Shape B — missing secret** (handoff bundle
    `cli/errors.jsx:ErrMissingSecret`): the `--url-env` unset
    AND empty paths now render the design's three-panel block
    (lookup failure named at the panel title — `env var X is
    not exported` / `env var X is set but empty` rather than
    the misleading "missing connection string" the old surface
    used — recommended `--url-env` form with security rationale,
    shell-level diagnostics, and a trailing `→ next:` breadcrumb
    pointing at `docs/setup.md`). The two states render distinct
    titles + panel headers so operators can tell at a glance
    which case fired. Previously rendered as the plain `error /
    why / fix / next` `GuidedError` block.

  New module `schemabrain/errors_render.py` (313 LOC) hosts both
  shapes; `schemabrain/errors.py` is unchanged (DTO + translators
  stay). Tests in `tests/test_errors_render.py` (32 tests, 100%
  line + branch coverage on the new module) pin the layout
  contract. Two existing assertions in `tests/test_cli.py`
  flipped from the old `"fix:"` substring to the new design
  vocabulary; no other test churn.

- **`schemabrain doctor` re-rendered onto the design's numbered
  checklist surface** — second operator-visible win from the
  design-system migration arc (PR #4 of #71/#72/#73/#4/...).
  Output reshape: a cyan brand line
  (``◆ environment · {cwd} · {host} · {os}    N / M healthy``)
  replaces the old ``SchemaBrain doctor — N pass, N warn, N fail``
  header. A progress rule above the grid surfaces total elapsed
  time (``  N checks  ────────  {elapsed} ms``). Per-check rows
  render in a Table.grid with columns ``ordinal · glyph · name ·
  detail``; the zero-padded ordinal anchors the eye for vertical
  scanning, and remediation lines render under the detail with the
  design's ``→ fix:`` prefix when ``suggested_next`` is set. Closing
  footer (``N checks · A ok · B warn · C err``) mirrors the JSON
  contract counts so a user grepping CI logs for the on-screen
  numbers finds the same shape on disk. The terminal renderer is
  extracted from ``schemabrain/setup/doctor_flow.py`` into a new
  ``schemabrain/setup/doctor_render.py`` module (mirroring the
  ``inspect/render.py`` + ``check/render.py`` boundary); the local
  ``_GLYPHS`` dict in ``doctor_flow.py`` is removed. Per-check
  status routes through ``schemabrain._ui.status_glyph`` via a new
  ``_DOCTOR_STATUS_TO_TIER`` translation map
  (``pass → ok``, ``warn → warn``, ``fail → err``) — the
  per-surface translation pattern PR #73 established. Unknown
  outcomes raise a ``ValueError`` rather than silently rendering as
  ``✗`` red, so vocabulary drift between ``CheckOutcome`` and the
  translation map surfaces visibly. The JSON output
  (``--json`` flag) contract (``checks`` / ``summary`` / ``exit_code``)
  is unchanged — wall-clock elapsed-ms is a presentation-only field
  threaded from ``cli._cmd_doctor`` into the renderer, not folded
  into the JSON shape. ``render_doctor`` re-exported from
  ``doctor_flow`` so existing
  ``from schemabrain.setup.doctor_flow import render_doctor``
  imports keep working.

- **`schemabrain init` wizard re-rendered onto the design's hero
  surface** — first operator-visible win from the design-system
  migration (PR #71 + PR #72 shipped the foundation primitives).
  The previous per-stage Rich Panel column collapses onto the
  design's compact StageRow layout: each stage now renders as one
  row of (zero-padded ordinal · glyph · display name · message ·
  duration), with optional next-step hints indented under the
  message column. A new progress rule above the stage list
  (``  7 stages  ────────  {elapsed} · {advisory count}``)
  summarises the run shape before the operator scans rows. The
  bordered cyan header Panel collapses to a one-line brand line
  (``◆ SchemaBrain init — activating for {host}. ~30s.``) so the
  visual weight lands on the stages, not the framing. The local
  ``_STAGE_GLYPHS`` + ``_STAGE_PANEL_BORDER`` dicts in
  ``schemabrain/cli.py`` are removed; stage status routes through
  ``schemabrain._ui.status_glyph`` via a new
  ``_WIZARD_STATUS_TO_TIER`` translation map
  (``done → ok``, ``skipped → skipped``, ``failed → err``) — the
  PR #72 deferred migration. Visible glyph flip: the previous
  ``↷`` (RIGHTWARDS WAVE ARROW) used for skipped stages becomes
  the design-spec ``⊘`` (CIRCLED DIVISION SLASH); the flip bundles
  with this surface migration so the diff is auditable. Closing
  block (``Restart Claude…``, audit/tail hints, thesis tagline)
  + abort panel + wire-host detail rendering preserved unchanged.

### Added
- **`GLYPH_RULE` constant + `top_rule(label, right=None, *, width,
  style)` text builder in `schemabrain/_ui.py`** — renders the
  design's section-header band (``  label  ────────  right``)
  consumed by the wizard's progress rule today and by ``doctor``
  / ``audit list`` follow-up PRs. ``Text``-returning helper
  (not pre-rendered string) so callers can compose with other
  Rich primitives. Narrow-terminal collapse: dashed run floors
  at 4 dashes rather than wrapping. Six new tests in
  ``TestTopRule`` pin the contract.

- **`status_glyph(status_name)` + seven new glyph constants in
  `schemabrain/_ui.py`** — second wave of design-system primitives
  for the wizard, ``doctor``, and ``tail`` re-renders. The new
  helper routes general operator-status tier names (``ok`` /
  ``warn`` / ``err`` / ``active`` / ``pending`` / ``skipped``) to
  ``(glyph, rich_style)`` tuples, matching ``drift_glyph``'s
  unknown-tier hard-break fallback by design. The new glyph
  constants (``GLYPH_ACTIVE`` ``▸``, ``GLYPH_PENDING`` ``◇``,
  ``GLYPH_SKIPPED`` ``⊘``, ``GLYPH_BRAND`` ``◆``, ``GLYPH_ARROW``
  ``→``, ``GLYPH_BULLET`` ``•``, ``GLYPH_SEP`` ``·``) round out
  the design's glyph vocabulary. Local stage / check glyph dicts
  in ``setup/doctor_flow.py:_GLYPHS`` and ``cli.py:_STAGE_GLYPHS``
  collapse onto ``status_glyph`` when their surfaces are
  re-rendered (visible glyph flip for ``skipped``: current ``↷``
  → design-spec ``⊘`` bundles with that surface's re-render).

- **Shared CLI shell vocabulary `schemabrain/_ui.py`** — foundation
  for the design-system migration anchored on the ``schemabrain-v1``
  handoff bundle. Defines the glyph constants
  (``GLYPH_OK``/``GLYPH_WARN``/``GLYPH_ERR``), the
  ``drift_glyph(def_kind) -> (glyph, rich_style)`` router
  (entity → ``✗ red``, metric / canonical_join → ``⚠ yellow``,
  unknown → hard-break fallback by design), the
  ``pii_marker(sensitivity)`` label vocabulary (verbatim
  pass-through for unknown tiers so indexer-introduced tiers
  surface rather than disappear), and the ``make_console(...)``
  factory — the single Console hook for future ``--no-color`` /
  ``--json`` / palette work (``NO_COLOR=1`` honoured via Rich's
  built-in env contract). Threaded through ``cli_ui.RichReporter``,
  ``check/render.py``, and ``inspect/render.py`` with zero
  behaviour change. Follow-up PRs migrate one operator surface at
  a time on top of this seam.

### Changed
- **`schemabrain._ui.severity_glyph` renamed to `drift_glyph`**
  (PR #71's foundation helper). The function's input is a
  ``def_kind`` noun (``entity`` / ``metric`` / ``canonical_join``),
  not a tier name — the rename makes its scope honest now that the
  general ``status_glyph(status_name)`` primitive ships alongside.
  Sole consumer (``check/render.py``) updated in the same commit;
  no other in-repo callsites existed.

- **Four new `SCHEMABRAIN_*` env-var overrides for tier-1 config
  knobs surfaced by the 2026-05-19 config-flexibility audit.**
  Following the env-var-with-strict-parser convention PR #67 locked
  in (`SCHEMABRAIN_*` prefix + positive ASCII regex + one-shot
  empty-env warn). All four resolve at call time, not module import,
  so operator overrides take effect mid-process:
  - `SCHEMABRAIN_PROFILER_SAMPLE_SIZE` (int, default `5`) —
    rows fetched per column for stats sampling. Deep schemas with
    LLMs that benefit from more exemplars can raise; linearly grows
    per-column SELECT cost + enrichment input-token bill.
  - `SCHEMABRAIN_PIPELINE_DEFAULT_CONCURRENCY` (int, default `8`)
    and `SCHEMABRAIN_PIPELINE_CRYPTIC_CONCURRENCY` (int, default
    `4`) — per-tier concurrency for the async enrichment pipeline.
    Tier-1 Anthropic accounts (50 RPM) typically want lower values;
    higher concurrency triggers cascading 429s.
  - `SCHEMABRAIN_WIZARD_INDEX_ENRICH_CAP_USD` (float, default
    `10.0`) — cost ceiling for the wizard's index-stage enrichment
    when `init --enrich` is set. Previously hardcoded with no
    override path.

  All four use `on_invalid="raise"` so a typo'd env value (`"1_000"`,
  fullwidth digits, scientific notation, leading zeros, negatives)
  fails fast with a clear message rather than silently mis-tuning
  the runtime knob. Test pins in `tests/test_env_resolution.py`.

- **Shared `schemabrain/_env.py` env-var resolution module.**
  Promotes the PR #67 strict-regex parser into a reusable seam
  (`resolve_positive_int_env`, `resolve_positive_float_env`) with
  two invalid-handling modes: `"raise"` (fail-fast for security-
  and rate-limit-sensitive knobs) and `"warn_and_default"` (graceful
  fallback for interactive flows like the wizard). Same shared
  strict-regex contract for both int and float parsers — closes
  the `float("1_000.5")` silent-coercion footgun on the cost-cap
  surface that the int parser was already hardened against.
  Refactored callers: `enrichment/anthropic_client.py` (max_tokens),
  `setup/wizard.py` (entities/metrics cost caps), `cli.py`
  (entities/metrics suggest cost). All callers now route through
  the shared module; no behavior change for sane operator inputs;
  three latent footgun-acceptance bugs closed on the cost-cap path.

### Changed
- **`SCHEMABRAIN_MAX_LLM_COST_USD=""` (empty) in `schemabrain
  entities suggest` / `metrics suggest` now warns + uses the
  package default instead of rendering a guided error + exit 2.**
  Pre-refactor, `float("")` raised `ValueError` which the CLI
  translated into a `suggest_cost_env_malformed` guided error. The
  shared `_env` parser (matching the convention PR #67 established
  for `max_tokens`) treats an empty env value as a benign
  misconfiguration: emits a one-shot stderr breadcrumb so the
  operator sees their override didn't take effect, then falls back
  to the package default. The new behavior unifies CLI + wizard:
  both now follow the same "empty != invalid" contract. Operators
  who relied on the empty-env exit code to fail CI should set the
  env var to a real number or unset it entirely. Invalid values
  (`"not-a-number"`, `"-1.0"`, `"1_000"`) still raise + exit 2 as
  before — only `""` flipped.

- **Per-tier env-var override for Anthropic max-output-tokens.** Two
  new env vars expose the per-tier output cap as configuration
  without requiring a code change:
  - `SCHEMABRAIN_SONNET_MAX_OUTPUT_TOKENS` (default `4096`)
  - `SCHEMABRAIN_HAIKU_MAX_OUTPUT_TOKENS` (default `200`)
  Both follow the existing `SCHEMABRAIN_*` env-var convention. Parser
  is strict: positive ASCII decimal integers only (with optional
  leading `+`); rejects `"1_000"` (Python's underscore-separator silent
  coercion), `"04096"` (leading zero), fullwidth unicode digits,
  hex/octal prefixes, decimals, and negatives — any of which would
  otherwise become a silently-smaller cap. Set-but-empty values emit a
  one-shot stderr warning ("set but empty; using default N") rather
  than silently falling through.
- **`schemabrain index --source URL` flag.** Surface parity with
  `check` / `inspect` / `init` / `serve`, which already accepted
  `--source URL`. The positional `url` form still works for backwards
  compatibility; passing BOTH `--source` AND the positional form
  errors out (the resolution would otherwise be ambiguous). Same
  argv-leakage trade-off as the other commands — prefer `--url-env`
  for production use.
- **Strict MCP-tool argument validation.** A `_StrictArgsFastMCP`
  subclass intercepts the FastMCP dispatch seam and rejects calls
  whose `arguments` dict contains keys not declared on the tool's
  Pydantic arg model (FastMCP's auto-generated arg models default to
  `extra="ignore"`, so a typo'd kwarg like `grain` for `time_grain`
  on `get_metric` previously got silently dropped — the tool ran with
  the typo missing and returned a structurally-valid wrong answer).
  Rejected calls now raise `FastMCPToolError` so the client sees
  `isError: true` AND write one audit row + one bus event (status
  `error`, error_kind `invalid_argument`) so the rejection appears in
  `schemabrain audit list` and events.jsonl alongside successful
  calls. The internal `invalid_argument` ErrorKind never appears in a
  `ToolResponse` returned to the agent — only in audit rows and bus
  events for ops visibility.
- **`find_relevant_tables` empty result now carries
  `follow_up_hints=("describe_table",)`** instead of `null`.
  Previously, a new user who ran the cost-free wizard (no `--enrich`)
  and asked Claude "what tables have customer orders?" saw the agent
  get an empty response with no actionable next step — a silent
  dead-end. The hint now surfaces `describe_table` as the
  embedding-independent fallback so the agent has somewhere to chain.
- **`find_relevant_entities` ranks entity descriptions alongside
  column embeddings.** Per-entity score is now
  `MAX(column_cosine, description_cosine)` rather than `column_cosine`
  alone. The smoke surfaced this as: query "customer" surfacing
  `order` above `user` because the user-id column description in
  orders scored higher on "customer" than the email/full_name
  columns in users. With description-embedding ranking, the
  LLM-generated one-sentence description (`"customer accounts and
  identity"`) embedded against the query promotes `user` correctly.
  Description embeddings are cached per process in `_DESC_EMBED_CACHE`
  so the first call pays O(entities) embedder calls and subsequent
  calls amortize to zero. Empty descriptions skip the embedder
  entirely — no `embedder.embed("")` call. When the description
  wins, `EntityHit.best_column` is set to the sentinel
  `(entity description)` and `best_column_description` carries the
  description text, so the agent sees WHY the entity surfaced
  rather than a confusing column-name attribution.

### Fixed
- **`schemabrain inspect --source <VARNAME>` AND
  `schemabrain check --source <VARNAME>` no longer crash with an
  unhandled `ValueError` traceback.** Pre-fix, passing a bare
  env-var name (a common new-user mistake given `--url-env <VARNAME>`
  exists alongside `--source URL`) crashed `_canonical_url` because
  the bare string has no scheme. Both commands now route through the
  existing `_resolve_url` helper which intercepts the `ValueError`
  and emits a guided `url_invalid` block with exit code 2.
  Regression test in `tests/test_polish_bundle_regressions.py::TestB2_*`.
- **Bundled `ecommerce.sql` fixture now seeds orders / order_items /
  product_categories rows.** Pre-fix, the fixture seeded only users
  / addresses / products / categories — `orders` and `order_items`
  were both 0-row. The `examples/ecommerce/` README walkthrough
  Step 6 ("ask Claude for total_revenue") returned `null` against a
  fresh fixture, leaving new users to assume the product was broken.
  Three orders + four line items + two product_categories junction
  rows ($869.93 total revenue across three users) now make the
  marquee metrics (`total_revenue`, `order_count`,
  `distinct_ordering_customers`, `average_order_value`,
  `total_units_sold`) all return non-null sanity-checkable numbers.
- **Doctor's `host_config_store_path` warning now explains WHY the
  mismatch matters.** Pre-fix the warning just said "snippet
  store-path X differs from Y" — true but unactionable; new users
  saw it, didn't understand the impact, and ignored it. The new copy
  spells out that the MCP host reads from the SNIPPET's store, not
  the cwd's, so a mismatch means the host is talking to a different
  workspace than the one running doctor.
- **`schemabrain check` / `inspect` "no URL provided" error now
  hints at `--url-env DATABASE_URL` when `$DATABASE_URL` is set to
  a URL-shaped value.** Pre-fix, a user with DATABASE_URL exported
  (the common convention) still had to guess the flag form. The
  hint only fires when the env var contains a real-looking URL
  (with `://`), so a misnamed-but-populated env var doesn't trigger
  a misleading suggestion.
- **Wizard stage-3 entity suggestion consistently truncated on the
  bundled ecommerce fixture.** PR #66 caught the *crash* path —
  stages 3 + 4 now surface a clean failed `StageOutcome` instead of an
  unhandled traceback when Anthropic returns `stop_reason="max_tokens"`
  — but the *underlying* issue was that
  `_SONNET_DEFAULT_MAX_OUTPUT_TOKENS = 300` was sized for one-paragraph
  column descriptions, not for the multi-candidate YAML that
  `entities.suggest` and `metrics.suggest` produce. The default now
  bumps to `4096` (still bounded against runaway responses, but
  comfortably fits 10+ candidates with rationale + PII hints).
  Discovered via the 2026-05-19 ecommerce-fixture smoke: a real
  `ANTHROPIC_API_KEY` against the 7-table fixture hit `max_tokens` on
  the very first stage-3 call on every new user's first
  `schemabrain init` run. `_HAIKU_DEFAULT_MAX_OUTPUT_TOKENS = 200`
  stays unchanged (sized correctly for per-column descriptions).
  Regression-pinned via exact-equality assertions in
  `tests/test_enrichment_anthropic.py::TestMaxOutputTokensConfiguration`.
- **Three CLI smoke findings from the post-PR-#65 manual pass.** No
  CLI flag, JSON schema, or public API change.
  - **Wizard stage + abort `Panel` width** stretched unbounded with
    `expand=False` whenever a stage's failure or recovery message ran
    long (the store-schema-version-mismatch error is ~250 chars and
    rendered as a 200+ col panel that horizontal-scrolled on most
    terminals). Panels now soft-cap at `min(console.width, 100)` via
    a shared `_wizard_panel_width` helper; long messages wrap inside
    the panel rather than blowing it out.
  - **Wizard stages 3 + 4 crashed the entire run** when Anthropic
    hit `max_tokens` mid-prompt — the bare `RuntimeError` raised by
    `anthropic_client._extract_text` propagated through
    `pipeline.propose_from_*` and surfaced to the user as an
    unhandled traceback, violating the documented best-effort
    contract. Both `_run_entity_suggestion` + `_run_metric_suggestion`
    now wrap the LLM call in a narrow-scope catch that re-raises as a
    new `_LLMClientErrorAtWizard`; stage handlers catch it and emit a
    structured failed `StageOutcome` with a recovery hint naming
    `max_tokens` as the most common trigger. `MetricSuggestionParseError`
    is now explicitly caught alongside the entity-side
    `SuggestionParseError` so a malformed-LLM-YAML response on stage 4
    surfaces the right "transient LLM hiccups" hint instead of the
    LLM-network message. `ValueError` from `propose_from_*` is
    re-raised defensively (today's guards make it unreachable, but
    future drift now stays loud).
  - **All three `apply` subcommands (`entities apply` / `joins apply`
    / `metrics apply`)** crashed with `unrecognized arguments` when a
    shell glob (`apply dir/*.yaml`) expanded to more than one path.
    The positional `yaml_path` argument now uses `nargs="+"` on every
    apply command and accepts a mix of files and directories. A new
    shared `_expand_yaml_paths` helper resolves each path, expands
    directories to their immediate `.yaml`/`.yml` children, dedupes
    on canonicalised paths, and routes unreadable paths into the
    per-file failure summary (no more raw `PermissionError`
    traceback). `_cmd_entities_apply` gained the per-file failure-
    aggregation loop that `joins apply` and `metrics apply` already
    had — partial success now reports correctly, and a mid-loop
    structural error flushes the applied/failed summary before
    exiting so users see what landed.

### Changed
- **CLI rendering polish (Rich-only, no new deps).** Three operator
  surfaces upgraded from hand-rolled string formatting to Rich
  primitives. No CLI flag, JSON schema, or public API change.
  - **`schemabrain inspect` (no-arg)** now renders the Entities /
    Metrics / Joins listing as a guided Rich `Tree` under a
    "Definitions" root with per-category counts; empty categories are
    omitted from the tree entirely so a metrics-less store does not
    render an empty section heading.
  - **`schemabrain inspect <name>`** renders columns, related
    entities, and anchored metrics as `box.SIMPLE_HEAD` Tables
    (underlined headers, no row borders). The columns Table fits
    Name / Type / Null / Flags / PII; related entities collapse
    direction + cardinality into one "Edge" column and put the via-join
    annotation on a second row of the "On" cell.
  - **`schemabrain init`** renders each wizard stage as its own
    outcome-coloured Rich `Panel` — green border for done, yellow for
    skipped, red for failed. Title carries the existing
    `[N/7] Stage (duration)` header verbatim; `expand=False` keeps each
    panel as wide as its content so the sequence reads as a status
    checklist rather than a column of full-width banners. The
    wire_host follow-up detail (config path, backup, redacted shell-out
    argv, manual snippet) continues to render outside the panel
    because the `printed_only` branch writes the JSON snippet to
    stdout and would be hidden inside ANSI box-drawing otherwise.
  - **`schemabrain tail`** collapses the two-line per-event render
    (line 1: time + tool + args; line 2: indented arrow + result) to
    one column-aligned line. Time (12) and tool (22) widths are
    fixed-width via `ljust` so the eye reads each column straight
    down the stream; long arg strings stay contiguous in the output
    buffer rather than being column-folded (which would split
    substrings across visual rows and break `tail | grep` matching).
    Tool names longer than the column cap truncate with a single "…".

### Fixed
- **Documentation sync with the wizard semantic-layer arc (PRs #60–#63).**
  README, `docs/setup.md`, and `docs/assets/demo.tape` still described
  the pre-arc 5-stage entities-only wizard. All three are now in sync
  with the shipped 7-stage flow:
  - **README hero + Quickstart §3** — the "five stages" claim is gone;
    the wizard is now described as seven stages (source check → index →
    entities → metrics → joins → wire host → next) with auto-detection
    of a dbt manifest routing stages 3 and 4 through the importer when
    present. Sample wizard-output block updated to show `[1/7]…[7/7]`
    with the new metrics + joins lines.
  - **README "What each stage does"** — expanded from 5 entries to 7,
    documenting `Curate metrics` (LLM, cost-capped, anchored on
    entities) and `Curate joins` (deterministic FK + query-log mining,
    no cost cap). New paragraphs document the best-effort posture for
    stages 3–5, the pre-LLM Enter-to-continue pause for stages 3–4,
    and the dbt source-of-truth path.
  - **README "Import from dbt"** — now surfaces the `init --from-dbt
    PATH` first-class flag and the auto-detect rules
    (`$DBT_PROJECT_DIR/target/manifest.json` or cwd-walk for
    `dbt_project.yml`) alongside the existing standalone
    `schemabrain import dbt` command.
  - **`docs/setup.md`** — wizard description updated from 5 to 7
    stages with matching descriptions of the new flags
    (`--no-metrics`, `--metrics-max-cost-usd`, `--no-joins`,
    `--from-dbt`, `--skip-llm-confirm`). The pre-existing comment
    about `ANTHROPIC_API_KEY` now mentions both LLM-driven stages.
  - **`docs/assets/demo.tape`** — header comments corrected from the
    internally-broken "five stages: doctor → connect → entities (LLM)
    → joins → metrics" copy (stage names didn't match reality, count
    was wrong) to the actual 7-stage sequence. Recording shell now
    needs `--skip-llm-confirm` on the `init` invocation so the
    Enter-to-continue prompt doesn't hang the recording in a TTY
    shell. Sleep budget after `init` bumped from 20s to 30s to cover
    the second LLM stage (metrics) on top of the existing entities
    call.

- **Documentation accuracy pass (pre-tag audit).** Three-agent verification
  of README, `docs/mcp-tools.md`, and `docs/assets/demo.tape` against the
  shipped code surfaced the following inaccuracies, all now corrected:
  - **README Sample-session `tail` excerpt** now shows the actual two-line
    renderer output (event header on line 1, indented `→ result in Nms` on
    line 2) — the previous single-line condensation didn't match what
    `schemabrain tail` actually prints.
  - **README §4 doctor check count** softened from `11 checks` to
    `up to 11 checks`. The full set runs for Claude Desktop on macOS/Windows
    with a Postgres source URL; Linux, claude-code, and source-less
    invocations skip the inapplicable checks.
  - **`docs/mcp-tools.md` `get_example_queries` response sample** keyed
    into `items` but the actual `ExampleQueriesResult` field is `queries`.
    Sample also advertised `first_seen_at` / `last_seen_at` fields that
    don't exist on `ExampleQueryItem`. Both corrected to match the model.
  - **`docs/mcp-tools.md` `describe_entity` sample** showed
    `pii_sensitivity: "pii"` on the `email` column, but the field is
    currently hardcoded to `"public"` on every column (a wire-shape
    placeholder for upcoming column-level classification). Sample now
    matches the actual response; prose explains the placeholder.
  - **`docs/mcp-tools.md` `get_metric`** now documents that non-empty
    `fan_out_join_names` downgrades the envelope to `status="degraded"`
    with `confidence="MEDIUM"` — the machine-readable signal the agent
    should key on, previously left implicit.
  - **`docs/mcp-tools.md` `resolve_join`** previously only documented
    `error.kind="ambiguous_join"`. Now also documents the three other
    real error kinds: `no_canonical_join`, `unknown_join_name`,
    `join_name_mismatch`, each with its recovery hint.
- **`docs/assets/demo.tape`** — `Sleep 8s` after `schemabrain init`
  bumped to `Sleep 20s` to cover the stage-3 Haiku call on top of
  indexing. Header comment now also flags that `schemabrain init` must
  be run manually once before recording to warm the fastembed model
  cache (a cold first-run download can exceed the entire act's Sleep
  budget on its own), and that a skipped stage 3 will also break Act 2's
  `inspect customer` drill.

### Added
- **`schemabrain init` pre-LLM confirmation pause.** Each LLM-driven
  wizard stage (entities + metrics) now pauses for an Enter-to-continue
  confirmation before calling Anthropic. The prompt shows the stage
  label and the cost ceiling that's active for that stage:

      This stage calls Anthropic to suggest entities (cap: $1.00).
      Press Enter to continue, or Ctrl-C to skip this stage.

  `Ctrl-C` skips the stage cleanly (records a `skipped` outcome with
  "user cancelled the LLM call" and lets the wizard continue to the
  next stage — same best-effort posture as the existing skip
  branches). Joins is unaffected — it's deterministic, no LLM call.
  dbt-mode is unaffected — the dbt branch runs before the prompt, so
  importing from a dbt manifest never asks for confirmation.

  **Auto-suppression in non-interactive environments**: the prompt
  helper checks `sys.stdin.isatty()` and returns "proceed" without
  printing or reading when stdin isn't a TTY. CI runs, pytest, and
  scripted pipelines all get the previous frictionless behaviour.
  The whole feature is purely an interactive-terminal affordance.

  Two new opt-out flags:
  - `--skip-llm-confirm` — narrow opt-out: skip the LLM-prompt only,
    leave the host-overwrite prompt firing for existing entries.
  - `--yes` (existing flag, semantics extended) — superset shorthand:
    skip BOTH the LLM-prompt and the host-overwrite prompt. The right
    flag for CI / scripted runs. `--yes` help text updated.

  New `WizardConfig.skip_llm_confirm: bool = False` field, appended
  with default so existing callers stay valid. The CLI dispatch layer
  derives `effective_skip_llm_confirm = args.skip_llm_confirm or
  args.assume_yes`, encoding the locked design that `--yes` is the
  superset shorthand without silently mutating the existing
  `assume_yes` field's meaning.
- **`schemabrain init` dbt-import branch.** The activation wizard now
  auto-detects a compiled dbt project and routes stages 3 (entities)
  and 4 (metrics) through the dbt-manifest importer instead of the
  Anthropic-backed LLM pipeline when one is found. No new stage —
  the existing seven-stage layout is preserved; the new behaviour is
  a conditional branch INSIDE stages 3 and 4. Stage 5 (joins) is
  unchanged because dbt has no canonical-join concept; the
  deterministic FK + query-log path runs regardless.

  Detection happens during stage 1 (`source_check`) and writes to
  `WizardContext.dbt_manifest_path: Path | None`. Search order:
  (1) explicit `--from-dbt PATH` flag — wizard ABORTS at stage 1
  if the path is missing or the source is non-Postgres (the dbt
  importer needs a live Postgres connection for column
  verification); (2) `$DBT_PROJECT_DIR/target/manifest.json` if the
  env var is set AND the file exists; (3) walk cwd up to 3 parents
  looking for a `dbt_project.yml` sentinel, then use
  `<dir>/target/manifest.json` if compiled. Auto-detection is
  best-effort — a missing manifest falls through to LLM-suggest
  silently.

  New flag: `--from-dbt PATH`. No cost cap (dbt import doesn't call
  the LLM); no API-key check inside the dbt branch (sits BEFORE the
  API-key check in stages 3 and 4 so dbt mode works without
  `ANTHROPIC_API_KEY`). When dbt mode is active, stage 1's outcome
  carries a next-step hint surfacing that stages 3 and 4 will route
  through dbt; outcome messages use "imported from dbt" provenance
  text instead of "(cost $N)".

  `_EntityApplyResult` and `_MetricApplyResult` gain a
  `source: Literal["llm", "dbt"] = "llm"` field. PR C of the wizard
  semantic-layer expansion arc; closes the arc.
- **`schemabrain init` canonical-join suggestion stage.** The activation
  wizard is now seven stages instead of six: `source_check → index →
  entities → metrics → joins → wire_host → next_step`. Stage 5 (joins)
  is best-effort (continues on failure, mirroring stages 3 and 4) and
  skips cleanly in five conditions: `--no-joins` opt-out, `--skip-index`
  set, non-Postgres source, store already has canonical joins for this
  source, or empty entity store (the cross-stage dependency — joins
  anchor on entities). **Joins is deterministic** — `suggest_canonical_joins`
  mines FK constraints + query-log evidence from the indexed schema, no
  LLM call. So this stage has no API-key check, no cost cap, no
  `--joins-max-cost-usd` knob. One new flag: `--no-joins` (opt out).
  The renderer's closing block grows a third parallel pending block
  (`_render_pending_joins_block`) with three branches: opt-out,
  empty-entity-store cross-stage hint ("Joins anchor on entities"),
  and generic failure. Stage labels in the rendered output update
  from `[N/6]` to `[N/7]`; the abort-panel denominator likewise
  updates. PR B of the wizard semantic-layer expansion arc; joins
  precedes the dbt-import branch (PR C).
- **`schemabrain init` metrics suggestion stage.** The activation wizard
  is now six stages instead of five: `source_check → index → entities →
  metrics → wire_host → next_step`. Stage 4 (metrics) is best-effort
  (continues on failure, mirroring stage 3 entities) and skips cleanly
  with a guided next-step pointer in six conditions:
  `--no-metrics` opt-out, `--skip-index` set, non-Postgres source, store
  already has metrics for this source, entity store is empty (the
  cross-stage dependency — metrics anchor on entities), or
  `ANTHROPIC_API_KEY` is missing. Two new flags: `--no-metrics` (opt
  out) and `--metrics-max-cost-usd N` (per-stage USD cost cap;
  defaults to `$SCHEMABRAIN_MAX_LLM_COST_USD` env var, then to the
  package default of $0.50). The renderer's closing block grows a
  parallel pending-metrics block (mirror of pending-entity-block)
  that surfaces the right recovery action for each skip condition —
  including the cross-stage hint to curate entities first when the
  entity store is empty. Stage labels in the rendered output update
  from `[N/5]` to `[N/6]`; the abort-panel denominator likewise
  updates. PR A of the wizard semantic-layer expansion arc; metrics
  precedes joins (PR B) and dbt-import branch (PR C).
- **`find_relevant_entities` MCP tool** (10th MCP tool, 5th
  semantic-layer tool). Embedding-cosine retrieval scoped to curated
  entities: per-entity score = MAX cosine across the columns of the
  entity's bound table — same MAX aggregation as
  `find_relevant_tables`, gated by the entity-binding lookup so only
  curated entities surface. Reuses the existing column embedding
  index (no new embedding work). Returns a `list[EntityHit]` with
  `name`, `score`, `qualified_table`, `best_column`,
  `best_column_description`, and `token_estimate`. Empty envelope
  routes the agent to `list_entities` and `find_relevant_tables`;
  success envelope chains to `describe_entity`. Charter-compliant
  (description ≤500 chars, "instead when" disambiguation, sibling
  composition). 25 dedicated tests covering impl-level edge cases,
  envelope round-trip, and the `result_summary` extractor for the
  audit row + tail render. Closes the demo-vision Act 3 gap.

### Fixed
- **PII classifier — four bug shapes surfaced by a 2026-05-18
  internal production-DB smoke.**
  - **S1** `<noun>_name` columns in non-PII tables no longer classify
    as `pii (contact)`. A denylist of thing-noun prefixes
    (`product`, `brand`, `category`, `language`, etc.) suppresses the
    bare-name rule for `^<prefix>_name$` shapes. People-noun shapes
    (`customer_name`, `user_name`, `display_name`, `full_name`) still
    classify as before.
  - **S2** `<token>_id` INTEGER FK columns no longer inherit PII
    categories from the referenced table's keyword. The classifier
    accepts a new `column_type` parameter; when type is integer-like
    AND the name matches the `<x>_id` shape, the matched category
    set is intersected with the FK-safe allowlist (`credential`,
    `online_identifier`, `government_id`, `health`,
    `demographic_protected`). `address_id BIGINT` now classifies as
    `public`; `patient_id BIGINT` keeps `health`.
  - **S3** `date_of_birth` / `birthdate` / `dob` / `birth_date` now
    classify as `demographic_protected` instead of `contact`,
    aligning with HIPAA Safe Harbor + GDPR Article 9.
  - **S4** False negatives covered: `drivers_license` /
    `drivers_license_number` (plural variants joined the
    government_id rule), `face_embedding` / `face_print` /
    `face_vector` (biometric rule), `patient_id` / `insurance_id` /
    `health_record_id` (new health rule), `age` (joined the
    demographic_protected rule).
- **Indexer** now passes `Column.data_type` to `classify_column` so
  the S2 integer-FK guard fires on live introspection.

### Added
- Synthetic PII regression fixture at
  `schemabrain/eval/fixtures/pii_mockup.sql` (5 tables, ~62 columns
  exercising every PIICategory plus the S1-S4 bug shapes) and a
  matching snapshot test at `tests/pii/test_pii_mockup_snapshot.py`
  that pins the desired `(column → (sensitivity, categories))`
  mapping for every column. Future rule changes that regress against
  the smoke's findings fail CI before merge.

### Added (original v1 semantic-layer arc — PRs #48-#52, landed 2026-05-18)

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
  (Postgres 16 + bundled fixture loader + SchemaBrain indexer). The
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
  the canonical join on the FK column from one side" — replacing a
  stale roadmap reference. 10 internal milestone-tagged comment
  references cleaned up across `schemabrain/core/join.py`,
  `schemabrain/core/store.py`, `schemabrain/core/store_protocol.py`,
  `schemabrain/joins/suggest.py`, `schemabrain/joins/yaml_grammar.py`,
  and three test files.

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

## [0.2.0a1] - 2026-05-15

### Added
- PEP 561 `py.typed` marker shipped in the wheel. SchemaBrain's
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
- `schemabrain mine-queries` now filters SchemaBrain's own profiler
  SELECT statements out of the mined `example_queries` set.
  Previously, running `mine-queries` against a Postgres that was also
  indexed surfaced SchemaBrain's own profiling chatter (positional-
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

[Unreleased]: https://github.com/Arun-kc/schemabrain/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/Arun-kc/schemabrain/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/Arun-kc/schemabrain/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/Arun-kc/schemabrain/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/Arun-kc/schemabrain/compare/v0.2.0a1...v0.3.0
[0.2.0a1]: https://github.com/Arun-kc/schemabrain/releases/tag/v0.2.0a1
[0.1.0a1]: https://github.com/Arun-kc/schemabrain/releases/tag/v0.1.0a1

[#90]: https://github.com/Arun-kc/schemabrain/pull/90
[#91]: https://github.com/Arun-kc/schemabrain/pull/91
[#92]: https://github.com/Arun-kc/schemabrain/pull/92
[#94]: https://github.com/Arun-kc/schemabrain/pull/94
[#95]: https://github.com/Arun-kc/schemabrain/pull/95
[#96]: https://github.com/Arun-kc/schemabrain/pull/96
[#97]: https://github.com/Arun-kc/schemabrain/pull/97
[#98]: https://github.com/Arun-kc/schemabrain/pull/98
[#99]: https://github.com/Arun-kc/schemabrain/pull/99
[#100]: https://github.com/Arun-kc/schemabrain/pull/100
[#101]: https://github.com/Arun-kc/schemabrain/pull/101
[#102]: https://github.com/Arun-kc/schemabrain/pull/102
[#104]: https://github.com/Arun-kc/schemabrain/pull/104
[#106]: https://github.com/Arun-kc/schemabrain/pull/106
[#110]: https://github.com/Arun-kc/schemabrain/pull/110
[#111]: https://github.com/Arun-kc/schemabrain/pull/111
[#112]: https://github.com/Arun-kc/schemabrain/pull/112
[#113]: https://github.com/Arun-kc/schemabrain/pull/113
[#115]: https://github.com/Arun-kc/schemabrain/pull/115
[#116]: https://github.com/Arun-kc/schemabrain/pull/116
[#117]: https://github.com/Arun-kc/schemabrain/pull/117
[#118]: https://github.com/Arun-kc/schemabrain/pull/118
[#119]: https://github.com/Arun-kc/schemabrain/pull/119
[#120]: https://github.com/Arun-kc/schemabrain/pull/120
[#121]: https://github.com/Arun-kc/schemabrain/pull/121
[#122]: https://github.com/Arun-kc/schemabrain/pull/122
[#123]: https://github.com/Arun-kc/schemabrain/pull/123
[#124]: https://github.com/Arun-kc/schemabrain/pull/124
[#125]: https://github.com/Arun-kc/schemabrain/pull/125
[#126]: https://github.com/Arun-kc/schemabrain/pull/126
[#127]: https://github.com/Arun-kc/schemabrain/pull/127
[#129]: https://github.com/Arun-kc/schemabrain/pull/129
[#130]: https://github.com/Arun-kc/schemabrain/pull/130
[#132]: https://github.com/Arun-kc/schemabrain/pull/132
[#133]: https://github.com/Arun-kc/schemabrain/pull/133
[#135]: https://github.com/Arun-kc/schemabrain/pull/135
[#136]: https://github.com/Arun-kc/schemabrain/pull/136
[#137]: https://github.com/Arun-kc/schemabrain/pull/137
[#138]: https://github.com/Arun-kc/schemabrain/pull/138
[#140]: https://github.com/Arun-kc/schemabrain/pull/140
[#141]: https://github.com/Arun-kc/schemabrain/pull/141
[#142]: https://github.com/Arun-kc/schemabrain/pull/142
[#143]: https://github.com/Arun-kc/schemabrain/pull/143
[#144]: https://github.com/Arun-kc/schemabrain/pull/144
[#145]: https://github.com/Arun-kc/schemabrain/pull/145
[#146]: https://github.com/Arun-kc/schemabrain/pull/146
[#147]: https://github.com/Arun-kc/schemabrain/pull/147
[#148]: https://github.com/Arun-kc/schemabrain/pull/148
[#149]: https://github.com/Arun-kc/schemabrain/pull/149
[#150]: https://github.com/Arun-kc/schemabrain/pull/150
[#151]: https://github.com/Arun-kc/schemabrain/pull/151
[#152]: https://github.com/Arun-kc/schemabrain/pull/152
[#153]: https://github.com/Arun-kc/schemabrain/pull/153
[#154]: https://github.com/Arun-kc/schemabrain/pull/154
[#155]: https://github.com/Arun-kc/schemabrain/pull/155
[#156]: https://github.com/Arun-kc/schemabrain/pull/156
[#157]: https://github.com/Arun-kc/schemabrain/pull/157
[#158]: https://github.com/Arun-kc/schemabrain/pull/158
[#160]: https://github.com/Arun-kc/schemabrain/pull/160
[#161]: https://github.com/Arun-kc/schemabrain/pull/161
[#162]: https://github.com/Arun-kc/schemabrain/pull/162
[#163]: https://github.com/Arun-kc/schemabrain/pull/163
[#164]: https://github.com/Arun-kc/schemabrain/pull/164
[#165]: https://github.com/Arun-kc/schemabrain/pull/165
[#166]: https://github.com/Arun-kc/schemabrain/pull/166
[#167]: https://github.com/Arun-kc/schemabrain/pull/167
[#172]: https://github.com/Arun-kc/schemabrain/pull/172
[#173]: https://github.com/Arun-kc/schemabrain/pull/173
[#174]: https://github.com/Arun-kc/schemabrain/pull/174
[#179]: https://github.com/Arun-kc/schemabrain/pull/179
[#180]: https://github.com/Arun-kc/schemabrain/pull/180
[#181]: https://github.com/Arun-kc/schemabrain/pull/181
[#183]: https://github.com/Arun-kc/schemabrain/pull/183
[#184]: https://github.com/Arun-kc/schemabrain/pull/184
[#186]: https://github.com/Arun-kc/schemabrain/pull/186
[#187]: https://github.com/Arun-kc/schemabrain/pull/187
[#189]: https://github.com/Arun-kc/schemabrain/pull/189
[#190]: https://github.com/Arun-kc/schemabrain/pull/190
[#191]: https://github.com/Arun-kc/schemabrain/pull/191
[#192]: https://github.com/Arun-kc/schemabrain/pull/192
[#193]: https://github.com/Arun-kc/schemabrain/pull/193
[#194]: https://github.com/Arun-kc/schemabrain/pull/194
[#195]: https://github.com/Arun-kc/schemabrain/pull/195
[#196]: https://github.com/Arun-kc/schemabrain/pull/196
[#197]: https://github.com/Arun-kc/schemabrain/pull/197
[#198]: https://github.com/Arun-kc/schemabrain/pull/198
[#199]: https://github.com/Arun-kc/schemabrain/pull/199
[#200]: https://github.com/Arun-kc/schemabrain/pull/200
[#201]: https://github.com/Arun-kc/schemabrain/pull/201
[#202]: https://github.com/Arun-kc/schemabrain/pull/202
[#203]: https://github.com/Arun-kc/schemabrain/pull/203
[#204]: https://github.com/Arun-kc/schemabrain/pull/204
[#205]: https://github.com/Arun-kc/schemabrain/pull/205
[#206]: https://github.com/Arun-kc/schemabrain/pull/206
[#207]: https://github.com/Arun-kc/schemabrain/pull/207
[#208]: https://github.com/Arun-kc/schemabrain/pull/208
[#209]: https://github.com/Arun-kc/schemabrain/pull/209
[#210]: https://github.com/Arun-kc/schemabrain/pull/210
[#212]: https://github.com/Arun-kc/schemabrain/pull/212
[#213]: https://github.com/Arun-kc/schemabrain/pull/213
[#214]: https://github.com/Arun-kc/schemabrain/pull/214
[#215]: https://github.com/Arun-kc/schemabrain/pull/215
[#216]: https://github.com/Arun-kc/schemabrain/pull/216
[#217]: https://github.com/Arun-kc/schemabrain/pull/217
[#218]: https://github.com/Arun-kc/schemabrain/pull/218
[#219]: https://github.com/Arun-kc/schemabrain/pull/219
[#220]: https://github.com/Arun-kc/schemabrain/pull/220
[#221]: https://github.com/Arun-kc/schemabrain/pull/221
[#222]: https://github.com/Arun-kc/schemabrain/pull/222
[#223]: https://github.com/Arun-kc/schemabrain/pull/223
[#224]: https://github.com/Arun-kc/schemabrain/pull/224
[#225]: https://github.com/Arun-kc/schemabrain/pull/225
[#226]: https://github.com/Arun-kc/schemabrain/pull/226
[#233]: https://github.com/Arun-kc/schemabrain/pull/233
[#234]: https://github.com/Arun-kc/schemabrain/pull/234
[#235]: https://github.com/Arun-kc/schemabrain/pull/235
[#236]: https://github.com/Arun-kc/schemabrain/pull/236
[#237]: https://github.com/Arun-kc/schemabrain/pull/237
[#240]: https://github.com/Arun-kc/schemabrain/pull/240
[#241]: https://github.com/Arun-kc/schemabrain/pull/241
[#242]: https://github.com/Arun-kc/schemabrain/pull/242
[#243]: https://github.com/Arun-kc/schemabrain/pull/243
