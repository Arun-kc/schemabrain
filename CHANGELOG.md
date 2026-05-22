# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- **Reverse-traversal cardinality flip missed same-name FK joins —
  silent over-counting on grouped aggregates.** The earlier fan-out
  detector compared on-pair tuples against the stored `join.on` to
  decide whether BFS had walked a canonical join in reverse. When the
  FK source and target columns shared a name (extremely common: an FK
  on `customer_id` between `rental.customer_id` and
  `customer.customer_id`), the swapped pairs compared equal to the
  stored pairs and the cardinality flip silently never fired. A
  metric anchored on `customer` grouped by `rental.rental_date`
  reported `cardinality=many_to_one` instead of the correct
  `one_to_many`, `fan_out_join_names` came back empty, and the
  envelope shipped `status="success"` with inflated counts. Replaced
  the heuristic with an explicit `is_reverse_traversal: bool` flag
  carried on `_ChainEdge` and set deterministically at graph-build
  time. Surfaced by a Pagila re-test: 158 distinct customers per
  rental timestamp were reported as 182 by the compiler before the
  fix. New regression test
  `TestReverseTraversalCardinalityFlip::test_same_name_fk_reverse_traversal_flips_cardinality`.

### Added
- **Junction-table bridge synthesis on read.** `list_joins` and
  `schemabrain inspect <entity>` now surface logical M:N bridges
  through junction entities (e.g. `products <-> categories via
  product_categories`) alongside direct canonical joins. Detection
  reuses the existing `Table.is_junction_table()` heuristic
  (composite PK whose columns are all FK sources to ≥2 distinct
  targets); synthesis pairs the junction's canonical-join legs and
  emits one `JoinSummary` per unordered endpoint pair with
  `via_junction` + `via_joins` carrying the bridge mediation. No
  schema change — bridges are computed fresh on every read so
  follow-up edits to either leg reflect immediately. New module
  `schemabrain.joins.bridges` (`find_junction_entities`,
  `synthesize_bridges`, `synthesize_bridges_for_entity`,
  `composed_on_pairs`). Bridge inference inherits the WORST signal
  of its two legs — if either leg is `llm_suggested`, the bridge
  is too, so the agent cannot trust the bridge more than its
  weakest link. New optional `via_junction: str | None = None` +
  `via_joins: tuple[str, ...] = ()` fields on `JoinSummary` carry
  bridges over the wire; old clients that ignore them still see
  the entity pair and provenance. `RelatedEntity` gains the same
  `via_junction` field so `inspect` can mark mediated relationships
  in the renderer.
- **Charter v1.2 time-dimension inheritance via canonical-join
  chains.** When a metric has no `time_dimension` of its own AND
  the caller passes `time_grain`, the resolver BFSes the canonical-
  join graph for reachable entities with timestamp-typed columns
  over non-fan-out edges (`many_to_one` / `one_to_one` only) and
  inherits the unique candidate. The plan ships with
  `time_dimension_resolution="inherited"` and
  `inherited_time_dimension="<entity>.<column>"`; the emitter
  date_truncs against the joined entity's alias rather than the
  anchor's. 2+ candidates raise the new
  `AmbiguousTimeDimensionError`; 0 candidates ship unbucketed with
  `time_dimension_resolution="unavailable"`. New ErrorKind
  `ambiguous_time_dimension` (recovery contract: agent re-calls
  with explicit `time_dimension` once the override path lands).
  New DegradationReason `time_dimension_unavailable`. The fields
  surface on `MetricResult` so the agent sees which column was
  used and via which chain.
- **Charter v1.2 column-granular PII redaction in
  `describe_entity`.** Each column's real
  `pii_sensitivity` and `pii_categories` now propagate from the
  `column_pii_tags` store layer (previously hardcoded to
  `"public"`). When the server-policy `--pii-block` set
  intersects a column's categories, that column's
  `EntityColumn.redacted` field is `True` and its LLM-enriched
  semantic `description` is cleared — moving PII refusal from
  "whole entity blocked" to "specific columns redacted". The
  agent still sees the column exists; it just cannot read the
  model-generated semantics for it. The `EntityDetail`-level
  `redacted_columns: tuple[str, ...]` lists the redacted names
  at a glance. Envelope `confidence` is capped at MEDIUM when
  any redaction is applied so the agent knows it saw a partial
  view. `describe_entity_impl` accepts a `pii_block` kwarg for
  callers that build their own server scaffold.
- **Non-e-commerce-domain PII coverage.** Five new rules in
  `schemabrain.pii.classifier` extend the heuristic taxonomy to medical
  and blockchain-analytics schemas without relying on operator overlays.
  `RULE_COUNT` 41 → 46.
  - Medical: `encounter_id` / `visit_id` / `admission_id` /
    `discharge_id` → `health` (a clinical interaction occurring is
    HIPAA-sensitive even without clinical detail); `insurance_member_id`
    / `insurance_subscriber_id` → `health`; `npi` / `provider_npi` /
    `dea_number` → `government_id` (HHS / DEA-issued professional
    identifiers).
  - Blockchain: `wallet_address` / `blockchain_address` /
    `crypto_address` → `online_identifier` (pseudonymous on-chain
    identifier; matched in union with the existing `address` contact
    rule per the over-tag posture); `private_key` / `seed_phrase` /
    `mnemonic_phrase` / `mnemonic` → `credential` (key material whose
    disclosure is catastrophic).
- **Composite-expression measures via a strict whitelist grammar.**
  `MetricMeasure` now accepts `expression: str` alongside `column: str`
  (XOR: exactly one set). Composite expressions like `unit_price *
  quantity` parse through Python's stdlib `ast.parse(mode='eval')` with
  a node-type whitelist — identifier-shaped columns, integer + float
  literals, unary `-`, binary `+ - * /`, and parens. Anything outside
  the whitelist (function calls, comparisons, attribute access, etc.)
  raises `MalformedMeasureExpressionError` at parse time so the SQL
  emitter never sees free-form text. The emitter renders each operand
  with the same double-quoting + alias-prefix discipline as the
  single-column path; numeric literals are formatted via
  `str(int)` / `repr(float)`. SQL injection surface closed by
  construction. New module `schemabrain.semantic.compiler.measure_expression`.
- **Compile-time `unknown_measure_column` envelope.** Parallel to
  the existing `unknown_group_by_column` / `unknown_filter_column` —
  every column the measure references is now validated against the
  anchor entity's table at compile time. Closes a typo-becomes-
  `internal_error` gap that existed even for v1 bare-column measures
  and would have widened sharply with composite expressions.
- **`schemabrain/metrics/fixtures/ecommerce/total_revenue_real.yaml`:**
  bundled composite-expression metric over the demo's `order_item`
  entity. Computes line-level revenue as
  `SUM(unit_price_cents * quantity)` — closes a v1 DSL gap where
  a metric anchored on `order_item` couldn't express revenue
  derived from the line-item columns themselves.
- **Charter v1.2: 2D trust signal — `Provenance.inference_method`
  + `Provenance.validation_state`.** Replaces the v1.1 era's
  hardcoded `confidence="HIGH"` on every entity / metric / join
  producer with a derived label computed from two orthogonal axes:
  HOW was the fact derived (`manually_authored`, `llm_suggested`,
  `fk_constraint`, `dbt_import`, `observed_in_query_log`) and HOW
  VALIDATED is it (`draft`, `applied`, `confirmed`). `derive_
  confidence(method, state)` is the matrix; `derive_provenance_
  source(method)` keeps the v1.0 `Provenance.source` field
  consistent so old clients see a sensible value. Additive
  charter bump — v1.1 / v1.0 clients still deserialize cleanly.
  Per-row signal lands on the Pydantic summaries (`EntitySummary`,
  `MetricSummary`, `JoinSummary`, `EntityDetail`,
  `CanonicalJoinInfo`, `MetricResult`).
- **Store schema bump 13 → 14: `inference_method` +
  `validation_state` columns on entities, metrics, and
  canonical_joins.** First real in-place migration:
  `_migrate_v13_to_v14` ALTERs the three tables, backfills from
  the existing `origin` column (`manual` → `manually_authored` +
  `confirmed`; `suggested` → `llm_suggested` + `applied`;
  `dbt_import` → `dbt_import` + `applied`), and stamps version to
  14. Pre-v13 stores still raise `SchemaVersionMismatchError`.
- **FK-inferred cardinality at suggest time.** `joins suggest`
  now infers `many_to_one` / `one_to_one` / `one_to_many` from FK
  + PK info via `_infer_fk_cardinality`. Removes the spurious
  fan-out warning the prior `cardinality=None`-default suggester
  produced on every typical OLTP FK chain.
- **Direction-aware effective cardinality on `ResolvedJoin`.**
  The resolver flips the stored cardinality via
  `core.join.flip_cardinality` when BFS walks a canonical join in
  reverse of its stored direction. The fan-out detector now reads
  the EFFECTIVE value verbatim — multi-hop chains whose reverse
  hop multiplied anchor rows used to be missed.
- **`schemabrain inspect <name>` drills through metrics and
  joins.** Adds `MetricDetail` + `JoinDetail` data builders and
  parallel renderers; `_cmd_inspect` resolves `name` as
  entity → metric → join in priority. The pre-fix surface returned
  `no entity named <X>` for a metric or join name, breaking the
  summary view's "Drill into one" link.
- **MCP server `icons` + `website_url`.** FastMCP `initialize`
  response now carries three icon sizes (32 / 64 / 512 PNG) and
  the project repo URL so hosts that render server cards (Claude
  Desktop, Cursor) display the schemabrain mark instead of a
  generic placeholder.
- **Init wizard installs `--pii-block contact` by default.** The
  Claude Desktop snippet `build_snippet` writes the categories
  passed via `WizardConfig.pii_block`; the default
  (`("contact",)`) ensures the firewall is active on a fresh
  install. Operators with development / synthetic-data sources
  can opt out.

### Changed
- **MCP `CHARTER_VERSION` bumps `1.1` → `1.2`.** Wire-compatible
  with v1.1 / v1.0 clients; `confidence` is now a derivation
  rather than a hardcoded HIGH.

### Fixed
- **`schemabrain metrics list` empty-state hint.** Mirrors the
  MCP `list_metrics` tool's empty-state shape — the CLI now tells
  the operator the next command instead of dead-ending with a
  parenthetical.

### Changed
- **YAML measure schema:** `measure.expression` is now a valid
  alternative to `measure.column`; exactly one of the two must be set.
- **`MetricSummary` MCP wire shape:** `measure_column` becomes
  `str | None`; new `measure_expression: str | None` field. Discriminated
  union — agents reading `list_metrics` branch on which field is
  populated to decide bare-column vs composite handling.
- **Store schema bump 12 → 13** for the composite-expression column on
  the `metrics` table (nullable `measure_column`, new
  `measure_expression`, table-level XOR CHECK). Pre-alpha contract:
  operators with v12 stores re-index (re-indexing an unchanged schema
  costs $0 — fingerprint dedup skips the LLM call).
- **PII propagation across composite expressions:** every column the
  measure touches contributes to the propagated category set —
  previously only `measure.column` was harvested, which for composite
  expressions would have silently bypassed `--pii-block` on any
  tagged-but-unwalked operand.
- **README: promote `examples/anthropic_demo.py` above the Quickstart
  as the 5th firewall property.** The 230-LOC drop-in proof was buried
  inside `## After the wizard > Plug into your own agent loop`; now
  sits at the end of `## The firewall` with the same shape as the
  four SQL-boundary properties (one-line claim + the actual command
  + inline forward link). The after-wizard bullet for the Anthropic
  SDK path is reduced to a one-line pointer so the same proof doesn't
  appear twice. No code change.
- **Domain-agnostic LLM system prompts.** The entity-suggestion and
  metric-suggestion system prompts now use placeholder column / table
  names in their grammar examples instead of consumer-data-shaped
  examples (`customer` / `public.users` / `email`, `order_item` /
  `unit_price_cents` / `quantity`). Rule text references cross-domain
  examples (`patient_id`, `wallet_address`, `mrn` for entity `pii_hints`;
  `success_rate` / `interest_rate` / `mortality_rate` for the metric
  percentage/ratio rule) to make it explicit that the same grammar
  applies to clinical, financial, legal, commerce, and blockchain-
  analytics schemas. No behavioural change for e-commerce-shaped
  schemas; reduces consumer-data prompt bias for non-commerce schemas.
- **`schemabrain/profiler/postgres.py` module docstring** now documents
  the v1 non-goal of JSONB-path decomposition explicitly, with
  workarounds (normalized views / dbt importer) for operators with
  JSONB-heavy schemas. No code change.

### Fixed
- **Measure-expression parser rejects non-finite float literals.**
  `ast.parse("1e500", mode="eval")` silently overflows to
  `ast.Constant(value=inf)`; without a guard the renderer's
  `repr(inf)` would have written the bare token `inf` into the SQL
  stream, which Postgres can't interpret as a numeric — surfacing as
  `internal_error` at execute time instead of a clean
  `MalformedMeasureExpressionError` at parse time. Same fix covers
  `nan` (via `1e500 - 1e500`).
- **Measure-expression parser rejects literal-only expressions.**
  An expression like `100` or `1 + 2` passed validation but would
  compile to `agg(100) AS "m"` — a constant per group. Reject at
  parse time so a typo like `expression: "100"` (instead of
  `column: "amount"`) surfaces immediately rather than as
  constant-value results.
- **`MetricMeasure.column is None`** is now handled at every consumer
  site: `check/engine.py` (drift detection iterates `measure_columns`
  so composite-expression operand drops show up as `measure_column_missing`
  drifts with the right fix-hint), `inspect/engine.py` + `inspect/render.py`
  (composite metrics render as `sum(unit_price * quantity)` instead of
  `sum(None)`), and three `cli.py` paths (`metrics list`, `metrics audit`,
  and the YAML body `metrics suggest --apply` writes so composite
  candidates round-trip through the grammar parser).
- **LLM-suggest path accepts composite-expression candidates.**
  `_MEASURE_ALLOWED_KEYS` now includes `expression`; `_parse_measure`
  enforces the column/expression XOR with dedicated error messages
  matching the YAML grammar. System prompt updated with both the
  output schema and a worked example showing when to reach for the
  composite shape (line-item revenue on `order_item`).
- **`MalformedMetricRowError`** preserves the offending metric name
  when a store row's `measure_expression` (written via direct SQL
  bypassing the dataclass invariants) fails the whitelist parser.
  Previously the wrapped `ValueError` was caught by the MCP server's
  defensive `except Exception` and reduced to a bare `internal_error`
  envelope with no diagnostic; the new exception preserves
  `name` + `reason` as structured fields, the server includes the
  metric name in the envelope message, and the CLI's existing
  `metrics list` exit-2 corrupt-store contract remains intact.

## [0.3.0] - 2026-05-20

**Highlights** — Schema Brain v0.3.0 is the first release where the
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

  - **`schemabrain inspect`** summary replaces the `Schema Brain
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
  (`Schema Brain inspect` / `Entity: customer` / `Dry-run:` /
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
  replaces the old ``Schema Brain doctor — N pass, N warn, N fail``
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
  (``◆ Schema Brain init — activating for {host}. ~30s.``) so the
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
  Regression test in `tests/test_smoke_2026_05_19_fixes.py::TestB2_*`.
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
