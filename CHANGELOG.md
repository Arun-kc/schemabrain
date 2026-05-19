# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
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
  no other in-repo callsites existed. Surfaced by the PR #71
  3-agent reviewer rotation (code-reviewer MED #4); deferred to
  this PR so PR #71 stayed foundation-only.

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
  Discovered via the 2026-05-19 ecommerce-fixture smoke. Regression
  test in `tests/test_smoke_2026_05_19_fixes.py::TestB2_*`.
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
- **PII classifier — four bug shapes surfaced by the 2026-05-18
  production-DB smoke** (`docs/internal/manual_smoke_2026_05_18.md`).
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
