"""Seven-stage activation wizard behind `schemabrain init`.

The wizard turns a single `schemabrain init` invocation into the
end-to-end activation surface a new user expects: validate the source,
index the schema, suggest entities, suggest metrics anchored on those
entities, suggest canonical joins between the entities, wire the MCP
host, and print the next step.

Stages in order:

  1. source_check  — URL reachable + (Postgres) read-only session,
                     plus dbt-project auto-detection (PR C). When a
                     manifest is found, stages 3 and 4 import from
                     dbt instead of calling the LLM.
  2. index         — cache-aware DDL introspection into the local store
  3. entities      — Anthropic-backed entity suggestion (cost-capped)
                     OR dbt-manifest import when `ctx.dbt_manifest_path`
                     is set
  4. metrics       — Anthropic-backed metric suggestion anchored on
                     entities (cost-capped, requires entities present)
                     OR dbt-manifest import when `ctx.dbt_manifest_path`
                     is set
  5. joins         — deterministic canonical-join suggestion from FK +
                     query-log evidence (no LLM, requires entities
                     present). Unchanged by dbt mode — dbt does not
                     have a canonical-join concept, so the FK +
                     query-log path runs regardless.
  6. wire_host     — write the MCP host config (or print the snippet)
  7. next_step     — render the "ask Claude X" hint

Each stage produces a `StageOutcome` (`done` / `skipped` / `failed`).
Stages 1, 2, 6, and 7 abort the wizard on `failed`; stages 3
(entities), 4 (metrics), and 5 (joins) emit a warning outcome and
let the wizard continue, because partial success (no entities /
metrics / joins curated yet) is still useful — the MCP host gets
wired either way.

The orchestrator is dependency-injected: tests pass synthetic
`WizardStage` lists to drive the state machine without touching a
database. Production callers use `run_default_wizard(...)`, which
binds the production stage handlers in the canonical order.

The dataclasses are frozen on purpose — they're the public contract
the CLI renderer (and future `--json` output mode) walks. Mutating
them after construction would invite drift between what the renderer
shows and what the wizard recorded.
"""

from __future__ import annotations

import contextlib
import os
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal, get_args

from sqlalchemy.exc import OperationalError

from schemabrain import _ui
from schemabrain._env import resolve_positive_float_env
from schemabrain._ui import prompt_yes_no, short_path
from schemabrain.errors import GuidedError
from schemabrain.errors_render import LlmFailureKind, classify_llm_failure
from schemabrain.setup.hosts import HostName, is_postgres_url
from schemabrain.setup.init_flow import (
    ClaudeDesktopEntryComparison,
    InitRefusal,
    InitResult,
    _validate_source_reachable,
    _validate_source_read_only,
    init,
    peek_claude_desktop_overwrite,
)

if TYPE_CHECKING:
    from rich.console import Console

    from schemabrain.indexer import IndexResult

    # NOTE: `ClaudeDesktopEntryComparison` is imported at runtime
    # at module top (used as a return type from
    # `peek_claude_desktop_overwrite` and as an annotation on the
    # `_render_overwrite_diff_summary` signature). No duplicate
    # `TYPE_CHECKING` import here — the duplicate would mislead
    # readers into thinking the runtime import is missing or
    # conditional.

# ----- public types ---------------------------------------------------------

StageStatus = Literal["done", "skipped", "failed"]

_VALID_STATUSES: frozenset[str] = frozenset(get_args(StageStatus))
_VALID_HOSTS: frozenset[str] = frozenset(get_args(HostName))

# Stage 4 is the only stage that produces side-data the CLI cares about
# beyond a `StageOutcome` (the `InitResult` that describes what was
# written to the host config). The orchestrator parks it on
# `WizardContext.host_install_result`; the CLI renderer reaches for it
# when rendering the stage-4 outcome.


@dataclass(frozen=True)
class StageOutcome:
    """The result of running one wizard stage.

    `stage` is the 1-based ordinal (1..5 at v1). `name` is a stable
    snake_case identifier the renderer + future `--json` output key
    on. `status` is the tri-state outcome. `message` is the one-line
    human summary. `next_step` is an optional second line shown only
    when present — typically populated on `skipped` or `failed` to
    point the user at the recovery action. `duration_s` is the
    wall-clock seconds the stage spent; the orchestrator overwrites
    handler-returned values with a measured `perf_counter` delta so
    handlers can leave it at its 0.0 default.
    """

    stage: int
    name: str
    status: StageStatus
    message: str
    next_step: str | None = None
    duration_s: float = 0.0
    user_cancelled: bool = False
    """True iff a `failed` outcome reflects a deliberate user choice.

    Set by stage handlers that catch an interactive-prompt decline
    (today: only `_stage_wire_host` when the F3 overwrite prompt
    returns False). Consumed by `_cmd_init` to map the abort back
    to exit code 0 — a user-cancelled overwrite is not a failure,
    it's an informed choice.

    Replaces an earlier message-prefix-coupling shape
    (``message.startswith("cancelled by user")``) which silently
    broke whenever the prompt copy was edited.
    """

    def __post_init__(self) -> None:
        if self.stage < 1:
            raise ValueError(f"StageOutcome.stage must be >= 1; got {self.stage}")
        if not self.name:
            raise ValueError("StageOutcome.name must be a non-empty identifier")
        if not self.message:
            raise ValueError("StageOutcome.message must be a non-empty string")
        if self.status not in _VALID_STATUSES:
            raise ValueError(
                f"StageOutcome.status must be one of {sorted(_VALID_STATUSES)}; got {self.status!r}"
            )
        if self.duration_s < 0:
            raise ValueError(f"StageOutcome.duration_s must be >= 0; got {self.duration_s}")
        # A `failed` outcome without a recovery hint leaves the user
        # at a dead end. The renderer's dim next-step line simply
        # doesn't render when `next_step` is None, so the failure
        # message stands alone — useful only to someone who already
        # knows what to do. Enforce a recovery pointer on every
        # failure outcome.
        if self.status == "failed" and not self.next_step:
            raise ValueError(
                "StageOutcome with status='failed' must include next_step "
                "pointing the user at a recovery action"
            )
        # `user_cancelled=True` is only meaningful on a `failed`
        # outcome. Setting it on done/skipped is a contract violation
        # the orchestrator's exit-code mapping would silently ignore.
        if self.user_cancelled and self.status != "failed":
            raise ValueError("StageOutcome.user_cancelled is only valid with status='failed'")


@dataclass(frozen=True)
class WizardConfig:
    """Resolved configuration passed to every stage handler.

    Carries everything a stage might need to do its work — source URL,
    store path, host target, plus the flag-driven knobs for stages 2,
    3, 4, and 5. Frozen so handlers can't mutate the config under
    each other.

    `no_metrics` / `metrics_max_cost_usd` are stage-4 knobs added in
    PR A. `no_joins` is the stage-5 knob added in PR B (joins is
    deterministic — no cost cap needed). `from_dbt` is the PR C knob
    — when set, stage 1 validates the path and stages 3 + 4 import
    from the dbt manifest instead of calling the LLM.

    `skip_llm_confirm` bypasses the interactive "Press Enter to
    continue" pause that fires before each LLM-driven stage
    (entities + metrics). The CLI's `--yes` flag implies this; it is
    also the field a programmatic caller sets when constructing a
    `WizardConfig` directly. The pause auto-suppresses in non-TTY
    environments (CI, pytest, scripts) regardless of this field, so
    the field only matters when the wizard is actually running
    interactively.

    New fields are appended (with defaults) so existing callers
    constructing this dataclass with positional or keyword arguments
    remain valid.
    """

    source_url: str
    store_path: Path
    host: HostName
    env_var_name: str
    skip_index: bool
    no_entities: bool
    enrich: bool
    entities_max_cost_usd: float | None
    assume_yes: bool
    no_metrics: bool = False
    metrics_max_cost_usd: float | None = None
    no_joins: bool = False
    from_dbt: Path | None = None
    skip_llm_confirm: bool = False
    # Default `--pii-block` categories written into the host snippet.
    # `("contact",)` is the safe default — blocks the most universally
    # sensitive PII (email, phone, address) without surprising the
    # operator with over-broad refusals on benign columns. `()` opts
    # out entirely (no `--pii-block` flag added to the snippet, server
    # runs with enforcement off — useful for development databases
    # or fully synthetic fixtures).
    pii_block: tuple[str, ...] = ("contact",)

    def __post_init__(self) -> None:
        if self.host not in _VALID_HOSTS:
            raise ValueError(
                f"WizardConfig.host must be one of {sorted(_VALID_HOSTS)}; got {self.host!r}"
            )
        if self.entities_max_cost_usd is not None and self.entities_max_cost_usd <= 0:
            raise ValueError(
                "WizardConfig.entities_max_cost_usd must be a positive value "
                f"or None; got {self.entities_max_cost_usd!r}"
            )
        if self.metrics_max_cost_usd is not None and self.metrics_max_cost_usd <= 0:
            raise ValueError(
                "WizardConfig.metrics_max_cost_usd must be a positive value "
                f"or None; got {self.metrics_max_cost_usd!r}"
            )


@dataclass
class WizardContext:
    """Mutable runtime state shared across stage handlers.

    `outcomes` is appended to by the orchestrator after each stage
    returns. `host_install_result` is set by the stage-4 handler so
    the renderer can decide which post-wiring next-step copy to
    print.

    Intentionally non-frozen: stages need a single back-channel for
    the host install result, and threading a sentinel through every
    `StageOutcome` would muddy the contract.

    `dbt_manifest_path` is populated by stage 1 (`_stage_source_check`)
    when either `cfg.from_dbt` is set (explicit) or auto-detection
    finds a manifest under cwd. Stages 3 and 4 branch on this field
    to decide between dbt-import and LLM-suggest. `None` means
    LLM-suggest mode. Set only by stage 1; downstream stages MUST
    treat it as read-only.
    """

    config: WizardConfig
    outcomes: list[StageOutcome] = field(default_factory=list)
    host_install_result: InitResult | None = None
    dbt_manifest_path: Path | None = None


StageHandler = Callable[[WizardContext], StageOutcome]


@dataclass(frozen=True)
class WizardStage:
    """A single stage in the wizard pipeline.

    `abort_on_fail=True` means a `failed` outcome from `handler` stops
    the wizard immediately (stages 1, 2, 4). `abort_on_fail=False`
    means a `failed` outcome is recorded but the wizard continues
    (stage 3 — entity suggestion is aspirational).
    """

    stage: int
    name: str
    handler: StageHandler
    abort_on_fail: bool


@dataclass(frozen=True)
class WizardResult:
    """The complete result of one `run_wizard` call.

    `outcomes` is in execution order. `aborted=True` means the wizard
    stopped before reaching the final stage. `host_install_result` is
    set when the stage-4 handler ran (whether or not the wizard
    finished).
    """

    outcomes: tuple[StageOutcome, ...]
    aborted: bool
    host_install_result: InitResult | None = None

    @property
    def aborted_at(self) -> StageOutcome | None:
        """The outcome that caused the abort, if any.

        Returns the FIRST failed outcome in execution order — the
        wizard short-circuits on the first abort so there is at most
        one. Returns `None` on clean runs.
        """
        if not self.aborted:
            return None
        for outcome in self.outcomes:
            if outcome.status == "failed":
                return outcome
        return None  # pragma: no cover — defensive; aborted=True implies a failed outcome


# ----- orchestrator ---------------------------------------------------------


StageContext = Callable[[WizardStage], AbstractContextManager[None]]


@contextlib.contextmanager
def _null_stage_context_impl(_stage: WizardStage) -> Iterator[None]:
    """Default `stage_context` — runs each handler with no extra side effects."""
    yield


# Annotate explicitly so the conformance of the @contextmanager
# decorator's `GeneratorContextManager` return to `StageContext`'s
# wider `AbstractContextManager` bound is self-documenting (and would
# fail-fast if a future change drifts the signature).
_null_stage_context: StageContext = _null_stage_context_impl


def run_wizard(
    config: WizardConfig,
    *,
    stages: Sequence[WizardStage] | None = None,
    stage_context: StageContext | None = None,
) -> WizardResult:
    """Run the wizard pipeline against `config`.

    `stages` is dependency-injected so tests can drive the state
    machine without standing up a real database. Production callers
    use `run_default_wizard`, which binds `DEFAULT_STAGES`.

    `stage_context` is an optional context-manager factory invoked
    around each handler call. The CLI uses it to display a spinner
    while slow stages (index, entities) run; tests omit it. The
    callable receives the `WizardStage` so it can specialise on
    `stage.name` (e.g. only show a spinner for known-slow stages).

    Stage handlers MUST NOT raise — the contract is that they
    translate every exception into a `StageOutcome(status="failed",
    ...)`. An exception that escapes a handler signals a wizard bug,
    not user error, and is allowed to propagate so the CLI's top-level
    handler can render a crash trace rather than a misleading
    `failed` line.
    """
    actual_stages = stages if stages is not None else DEFAULT_STAGES
    actual_stage_context: StageContext = (
        stage_context if stage_context is not None else _null_stage_context
    )
    ctx = WizardContext(config=config)
    for stage in actual_stages:
        # Measure each handler ourselves rather than trust the handler
        # to report its own duration. Centralising the measurement
        # gives every stage a consistent timing surface for the
        # renderer + future --json output mode.
        started_at = time.perf_counter()
        with actual_stage_context(stage):
            outcome = stage.handler(ctx)
        elapsed = time.perf_counter() - started_at
        outcome = replace(outcome, duration_s=elapsed)
        ctx.outcomes.append(outcome)
        if outcome.status == "failed" and stage.abort_on_fail:
            return WizardResult(
                outcomes=tuple(ctx.outcomes),
                aborted=True,
                host_install_result=ctx.host_install_result,
            )
    return WizardResult(
        outcomes=tuple(ctx.outcomes),
        aborted=False,
        host_install_result=ctx.host_install_result,
    )


def run_default_wizard(
    config: WizardConfig, *, stage_context: StageContext | None = None
) -> WizardResult:
    """Production entry point: run the wizard with the canonical stages."""
    return run_wizard(config, stage_context=stage_context)


# ----- helper -------------------------------------------------------------


def _failed_from_refusal(
    *,
    stage: int,
    name: str,
    error: GuidedError,
) -> StageOutcome:
    """Translate an `InitRefusal`-style error into a failed `StageOutcome`.

    Preserves the guided-error's user-facing `message` + `next_step`
    so the wizard renderer surfaces the same recovery hint the
    standalone `init` would. The `StageOutcome` invariant requires a
    non-empty `next_step` on `failed`, so the diagnostic fallback
    kicks in for refusals that don't carry one (some `GuidedError`s
    set `next_step=None` because the `error.fix` field carries the
    operator-facing detail).
    """
    return StageOutcome(
        stage=stage,
        name=name,
        status="failed",
        message=error.message,
        next_step=error.next_step
        or "run `schemabrain doctor --source $URL` for a deeper diagnostic",
    )


def _prompt_llm_confirmation(*, stage_label: str, cost_cap_usd: float) -> bool:
    """Pause for user confirmation before calling the LLM.

    Returns True if the wizard should proceed (user pressed Enter,
    OR stdin is non-TTY so the prompt was auto-suppressed). Returns
    False if the user cancelled (Ctrl-C / EOF) — the caller then
    emits a `skipped` outcome and the wizard continues to the next
    stage.

    Auto-suppression: pytest captures stdin → `isatty()` is False →
    this function returns True without printing or reading, keeping
    every existing test green without changes. CI environments
    (GitHub Actions, etc.) are similarly non-interactive; the pause
    is purely an interactive-terminal affordance.

    `stage_label` and `cost_cap_usd` are baked into the prompt copy
    so the user sees exactly which stage they're authorising and
    the maximum cost the cost-ceiling guard will accept. We do NOT
    show an estimated cost — token-count estimates are heuristic
    and can mislead; the cap is the load-bearing number.

    The CLI's ``_wizard_stage_context`` registers the active Rich
    spinner before the stage handler runs; ``pause_active_spinner``
    stops it for the duration of the ``input()`` call. Smoke
    2026-05-19 surfaced the bug this fixes — the spinner kept
    rendering during the prompt and the user read it as "stage is
    already running" rather than "waiting on Enter". When there's no
    active spinner (non-spinner stages, test fixtures, ``--quiet``)
    the helper is a no-op.
    """
    if not sys.stdin.isatty():
        return True
    from schemabrain._ui import pause_active_spinner

    with pause_active_spinner():
        print(
            f"  This stage calls Anthropic to suggest {stage_label} (cap: ${cost_cap_usd:.2f}).",
            file=sys.stderr,
        )
        print(
            "  Press Enter to continue, or Ctrl-C to skip this stage. ",
            end="",
            file=sys.stderr,
            flush=True,
        )
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            # `^C` doesn't emit its own newline when caught here, so a
            # follow-up renderer line would land mid-cursor. Print one
            # explicitly so the stage-skipped line renders on a clean row.
            print("", file=sys.stderr)
            return False
    return True


# ----- stage 1: source_check -----------------------------------------------


def _stage_source_check(ctx: WizardContext) -> StageOutcome:
    """Validate the source URL is reachable + read-only (Postgres only).

    Wraps the same validators `init_flow.init` runs as its first two
    preconditions. SQLite sources skip the read-only check (no
    session-level read-only setting exists in SQLite).

    Stage 1 also handles dbt-project detection (PR C). When
    `cfg.from_dbt` is set explicitly, the path is validated and the
    wizard aborts if it does not exist. When `cfg.from_dbt` is None,
    the helper auto-detects a manifest via cwd-walk + `DBT_PROJECT_DIR`
    env var. Detection writes the result to `ctx.dbt_manifest_path`;
    stages 3 and 4 read that field to decide between dbt-import and
    LLM-suggest. Stage 1 never fails the wizard for missing
    auto-detection — that's the LLM-suggest path. The explicit
    `--from-dbt PATH` case is the only path that aborts.
    """
    cfg = ctx.config
    try:
        _validate_source_reachable(cfg.source_url)
        is_postgres = is_postgres_url(cfg.source_url)
        if is_postgres:
            _validate_source_read_only(cfg.source_url)
    except InitRefusal as refusal:
        return _failed_from_refusal(stage=1, name="source_check", error=refusal.error)

    # ----- dbt-project detection (PR C) ----------------------------
    #
    # `cfg.from_dbt` set explicitly: validate the path exists.
    # If it doesn't, abort — the user asked for a specific manifest
    # and a missing file is a configuration mistake worth surfacing.
    # If it exists, also refuse on non-Postgres source (dbt importer
    # requires a live Postgres connection for schema verification).
    if cfg.from_dbt is not None:
        if not cfg.from_dbt.exists():
            return StageOutcome(
                stage=1,
                name="source_check",
                status="failed",
                message=f"--from-dbt path {cfg.from_dbt} does not exist",
                next_step="check the path, run `dbt compile` to produce "
                "target/manifest.json, or omit --from-dbt to auto-detect "
                "or fall back to LLM-suggest",
            )
        if not is_postgres:
            return StageOutcome(
                stage=1,
                name="source_check",
                status="failed",
                message="--from-dbt requires a Postgres source "
                "(the dbt importer verifies model columns against the live schema)",
                next_step="point --source at the Postgres URL the dbt project "
                "targets, or omit --from-dbt to fall back to LLM-suggest",
            )
        ctx.dbt_manifest_path = cfg.from_dbt
    elif is_postgres:
        # Auto-detect only on Postgres sources. Best-effort — if no
        # manifest is found (or one is found without a Postgres
        # source-compatibility check), stages 3 and 4 fall through to
        # the LLM-suggest path.
        ctx.dbt_manifest_path = _auto_detect_dbt_manifest()

    base_message = (
        "Postgres reachable · session is read-only" if is_postgres else "source reachable"
    )
    if ctx.dbt_manifest_path is not None:
        return StageOutcome(
            stage=1,
            name="source_check",
            status="done",
            message=f"{base_message} · dbt manifest detected at {ctx.dbt_manifest_path}",
            next_step="stages 3 and 4 will import entities + metrics from dbt "
            "instead of calling the LLM",
        )
    return StageOutcome(
        stage=1,
        name="source_check",
        status="done",
        message=base_message,
    )


# Walk this many parents up from cwd looking for a `dbt_project.yml`
# sentinel. Empirical: 3 levels covers nested-monorepo layouts where
# the dbt project sits under `analytics/` or `etl/` from the
# Schema Brain run directory. Beyond 3 invites cross-project misfires.
_DBT_DETECT_PARENT_LIMIT: int = 3


def _auto_detect_dbt_manifest(*, cwd: Path | None = None) -> Path | None:
    """Search the operator's working tree for a compiled dbt manifest.

    Search order:
      1. `$DBT_PROJECT_DIR` env var, if set → probe
         `$DBT_PROJECT_DIR/target/manifest.json`. If that file exists,
         return it.
      2. Otherwise, starting at `cwd`, walk up to
         `_DBT_DETECT_PARENT_LIMIT` parents looking for a directory
         containing `dbt_project.yml`. If found, return
         `<that_dir>/target/manifest.json` IF that file exists.
      3. Otherwise, return `None`.

    Detection silently returns `None` rather than raising — a missing
    manifest just means LLM-suggest mode. The `--from-dbt PATH`
    branch in `_stage_source_check` handles the user-explicit case
    where a missing path SHOULD fail loudly.

    `cwd` is dependency-injected so tests can drive the walk against
    `tmp_path` without monkeypatching `Path.cwd`. Production callers
    pass `cwd=None`, which falls back to `Path.cwd()`.

    Compiled-manifest requirement: a `dbt_project.yml` without
    `target/manifest.json` means the user hasn't run `dbt compile` —
    importing nothing would be confusing. Falling through to
    LLM-suggest is the right behaviour. Users who want the dbt path
    can run `dbt compile` then re-run `schemabrain init`.
    """
    # Step 1: explicit env var trumps cwd-walk.
    env_dir = os.environ.get("DBT_PROJECT_DIR")
    if env_dir:
        manifest = Path(env_dir) / "target" / "manifest.json"
        if manifest.exists():
            return manifest
        # If the env is set but the manifest is absent, fall through
        # to the cwd-walk rather than refuse — `DBT_PROJECT_DIR` may
        # be set as a default while the user is working in a different
        # tree.

    # Step 2: cwd-walk for a `dbt_project.yml` sentinel.
    start = cwd if cwd is not None else Path.cwd()
    candidate: Path = start
    for _ in range(_DBT_DETECT_PARENT_LIMIT + 1):
        if (candidate / "dbt_project.yml").is_file():
            manifest = candidate / "target" / "manifest.json"
            if manifest.exists():
                return manifest
            # `dbt_project.yml` is present but no compiled manifest.
            # Stop walking — there's no point checking parents above
            # the dbt project root.
            return None
        parent = candidate.parent
        if parent == candidate:  # pragma: no cover — filesystem root
            break
        candidate = parent
    return None


# ----- stage 2: index ------------------------------------------------------


def _stage_index(ctx: WizardContext) -> StageOutcome:
    """Run cache-aware indexing into the local store.

    Decision tree:

      1. `cfg.skip_index` set → emit `skipped` (user opt-out).
      2. Non-Postgres source → emit `failed`. Indexing only supports
         Postgres sources today; SQLite remains the local-store
         format, not a source format.
      3. Store already has tables for this `source_id` → emit
         `skipped` (idempotent re-run).
      4. `cfg.enrich` set but `ANTHROPIC_API_KEY` missing → emit
         `failed` so the user can fix the env or drop `--enrich`.
      5. Otherwise, run the indexer pipeline and emit `done`.

    Exceptions from the indexer (cost-cap, source operational
    errors, unwritable store) are caught here and translated into
    `failed` outcomes. The wizard never lets the indexer's raw
    exception propagate — the stage-handler contract is "always
    return a `StageOutcome`".
    """
    cfg = ctx.config

    if cfg.skip_index:
        return StageOutcome(
            stage=2,
            name="index",
            status="skipped",
            message="--skip-index set; not running indexer",
            next_step="run `schemabrain index --url-env $VAR` later to populate the store",
        )

    if not is_postgres_url(cfg.source_url):
        # Non-Postgres sources (e.g. SQLite) can't be indexed today,
        # but the wizard should still wire the host so the user gets
        # a working MCP entry. Surface the gap as `skipped`, not
        # `failed`, so stage 2's `abort_on_fail=True` doesn't kill
        # the wizard for a known limitation.
        return StageOutcome(
            stage=2,
            name="index",
            status="skipped",
            message="indexing only supports Postgres sources today",
            next_step="re-run with a Postgres URL to index the schema, "
            "or continue with the snippet for a non-Postgres source",
        )

    source_id = _source_id_for(cfg.source_url)

    if cfg.store_path.exists():
        existing = _peek_store_table_count(cfg.store_path, source_id)
        if isinstance(existing, StageOutcome):
            return existing  # schema-version mismatch surfaced as failed
        if existing > 0:
            # F4 (post-PR-#79 polish): the message used to read
            # "already indexed: 7 table(s) present for this source",
            # which the operator couldn't temporally locate — they
            # saw the hero panel saying "Setting up SchemaBrain"
            # then "already indexed" and wondered if their fresh
            # `init` was even doing anything. The check happens
            # BEFORE any writes in this stage, so a non-zero
            # `existing` count is unambiguously from a prior run
            # (no other code path inside this single wizard run
            # could have populated the store before stage 2 reached
            # this check). Make the temporal framing explicit and
            # surface the store path so the operator knows which
            # artifact is being reused.
            refresh_command = (
                f"run `schemabrain index --url-env {cfg.env_var_name}` "
                "to re-index against the live source"
            )
            return StageOutcome(
                stage=2,
                name="index",
                status="skipped",
                message=(
                    f"reusing {existing} table(s) from a prior indexing run "
                    f"({short_path(str(cfg.store_path))})"
                ),
                next_step=(f"{refresh_command} if the source schema has changed"),
            )

    api_key: str | None = None
    if cfg.enrich:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            return StageOutcome(
                stage=2,
                name="index",
                status="failed",
                message="--enrich passed but ANTHROPIC_API_KEY is not set",
                next_step="export ANTHROPIC_API_KEY=sk-ant-... or re-run without --enrich",
            )

    # Lazy-import the pipeline's exception type. Importing it at
    # module scope would pull in `enrichment.pipeline` (and its
    # transitive deps) on every wizard import, even for callers that
    # never reach stage 2.
    from schemabrain.enrichment.pipeline import CostCapExceeded

    try:
        result = _run_indexer(cfg=cfg, source_id=source_id, api_key=api_key)
    except OperationalError as exc:
        first_line = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        return StageOutcome(
            stage=2,
            name="index",
            status="failed",
            message=f"source unreachable during index: {first_line}",
            next_step="verify the URL and that the database is reachable",
        )
    except CostCapExceeded as exc:
        return StageOutcome(
            stage=2,
            name="index",
            status="failed",
            message=str(exc),
            next_step="re-run with a higher --max-cost-usd, or without --enrich",
        )
    except OSError as exc:
        return StageOutcome(
            stage=2,
            name="index",
            status="failed",
            message=f"store unwritable: {exc}",
            next_step="check filesystem permissions on the store path and re-run",
        )

    return StageOutcome(
        stage=2,
        name="index",
        status="done",
        message=_index_done_message(result, enrich=cfg.enrich),
    )


def _peek_store_table_count(store_path: Path, source_id: str) -> int | StageOutcome:
    """Open the store read-only and count tables for `source_id`.

    Returns the count on success, or a `StageOutcome(failed)` on:

    - schema-version mismatch — the store was created by a different
      schemabrain release
    - `OSError` — the store path's parent is unwritable or the file
      is unreadable (SQLiteStore's `mkdir(parents=True, exist_ok=True)`
      on the parent can raise here)

    The caller treats either failure as terminal for stage 2.
    """
    from schemabrain.core.store import SchemaVersionMismatchError, SQLiteStore

    try:
        with SQLiteStore(path=store_path) as store:
            return len(store.list_tables(source_connection_id=source_id))
    except SchemaVersionMismatchError as exc:
        return StageOutcome(
            stage=2,
            name="index",
            status="failed",
            message=str(exc),
            next_step=f"delete {store_path} and re-run, "
            "or install the matching schemabrain version",
        )
    except OSError as exc:
        return StageOutcome(
            stage=2,
            name="index",
            status="failed",
            message=f"store unreadable: {exc}",
            next_step=f"check filesystem permissions on {store_path.parent}",
        )


def _run_indexer(*, cfg: WizardConfig, source_id: str, api_key: str | None) -> IndexResult:
    """Build the indexer's dependencies and run one pass.

    Lazy-imports the heavy connector/profiler/pipeline/embedder
    modules so importing `wizard` is cheap for callers that never
    reach stage 2 (e.g. the eventual `--json` output mode).

    The cost cap on enrichment here is intentionally generous —
    indexing-time enrichment is a power-user surface guarded by the
    `--enrich` opt-in. Stage 3's `--entities-max-cost-usd` is the
    user-visible cost knob for the wizard's overall LLM spend.
    """
    from schemabrain.connectors.postgres import PostgresDataSource
    from schemabrain.core.store import SQLiteStore
    from schemabrain.enrichment.anthropic_client import anthropic_haiku_45_client
    from schemabrain.enrichment.embeddings import fastembed_default
    from schemabrain.enrichment.pipeline import EnrichmentPipeline
    from schemabrain.indexer import NullReporter, index
    from schemabrain.profiler.postgres import PostgresProfiler

    embedder = None
    with (
        PostgresDataSource(cfg.source_url) as source,
        PostgresProfiler(cfg.source_url) as profiler,
        SQLiteStore(cfg.store_path) as store,
    ):
        pipeline: EnrichmentPipeline | None = None
        if cfg.enrich and api_key is not None:
            # Resolve the wizard's index-stage enrichment cap at call
            # time: SCHEMABRAIN_WIZARD_INDEX_ENRICH_CAP_USD > $10
            # default. `on_invalid="raise"` is intentional — an
            # operator who exports a typo'd cap and then sees the
            # wizard silently aborting at a stale $10 wouldn't know
            # why their override isn't working. Raise lets the wizard
            # surface "set this env var, fix the typo".
            enrich_cap = resolve_positive_float_env(
                _WIZARD_INDEX_ENRICH_CAP_USD_ENV,
                _WIZARD_INDEX_ENRICH_CAP_USD,
            )
            pipeline = EnrichmentPipeline(
                client=anthropic_haiku_45_client(api_key=api_key),
                cryptic_client=None,
                max_cost_usd=enrich_cap,
                default_concurrency=_WIZARD_INDEX_CONCURRENCY,
                cryptic_concurrency=_WIZARD_INDEX_CRYPTIC_CONCURRENCY,
                store=store,
                source_connection_id=source_id,
            )
            embedder = fastembed_default()
        return index(
            source=source,
            profiler=profiler,
            store=store,
            source_connection_id=source_id,
            pipeline=pipeline,
            embedder=embedder,
            reporter=NullReporter(),
            no_pii_classify=False,
        )


def _source_id_for(url: str) -> str:
    """Stable short identifier for a source DB.

    Lazy-imports `cli._make_source_id` to avoid a module-level import
    cycle (cli imports wizard from `_cmd_init`). The function is
    fully resolved by the time any stage runs.
    """
    from schemabrain.cli import _make_source_id

    return _make_source_id(url)


def _index_done_message(result: IndexResult, *, enrich: bool) -> str:
    """Pick a one-line summary for a successful stage-2 outcome.

    The middle-dot separator matches the visual rhythm of the rest of
    the wizard output (source-check + closing block both use it) and
    reads more confidently than a comma — the numbers are facts, not
    a list.
    """
    cols = result.columns_added + result.columns_changed
    base = f"{result.tables_seen} tables · {cols} columns indexed"
    if enrich and result.descriptions_generated > 0:
        base += f" (enriched {result.descriptions_generated} columns, ${result.llm_cost_usd:.4f})"
    return base


# Cost + concurrency knobs used by `_run_indexer`. Kept here, not at
# the WizardConfig level, because they're internal to the wizard's
# bundled enrichment policy (a power-user run via `schemabrain index`
# directly carries its own knobs).
#
# `_WIZARD_INDEX_ENRICH_CAP_USD` can be overridden at runtime via
# `SCHEMABRAIN_WIZARD_INDEX_ENRICH_CAP_USD`. Resolved at call time in
# `_run_indexer` (not module import) so test monkeypatching works.
# The concurrency knobs stay hardcoded — they're guardrails to keep
# the bundled wizard flow tier-1 rate-limit-safe, not user-tunable
# goals (a power-user who wants higher concurrency runs `schemabrain
# index` directly, which has its own `SCHEMABRAIN_PIPELINE_*`
# overrides).
_WIZARD_INDEX_ENRICH_CAP_USD: float = 10.0
_WIZARD_INDEX_ENRICH_CAP_USD_ENV = "SCHEMABRAIN_WIZARD_INDEX_ENRICH_CAP_USD"
_WIZARD_INDEX_CONCURRENCY: int = 4
_WIZARD_INDEX_CRYPTIC_CONCURRENCY: int = 2


# ----- stage 3: entities ---------------------------------------------------


def _stage_entities(ctx: WizardContext) -> StageOutcome:
    """Suggest + apply entities via the Anthropic-backed pipeline,
    OR import from dbt when `ctx.dbt_manifest_path` is set (PR C).

    Decision tree:

      1. `cfg.no_entities` → skipped (explicit opt-out).
      2. `cfg.skip_index` → skipped (no schema to analyse).
      3. Non-Postgres source → skipped (entities need an indexed
         Postgres schema today).
      4. Store already has entities for this `source_id` → skipped
         (idempotent re-run).
      5. **dbt-import branch (PR C)**: `ctx.dbt_manifest_path` set →
         call `_run_entities_from_dbt` instead of the LLM pipeline.
         Sits BEFORE the API-key check because dbt import doesn't
         require an API key.
      6. `ANTHROPIC_API_KEY` missing → skipped with hint (entity
         suggestion is aspirational; a missing key is not a wizard-
         fatal condition). LLM path only.
      7. Otherwise, run the pipeline + apply candidates and emit
         `done` with the applied count + cost.

    Stage 3 is `abort_on_fail=False`: a failure here records a
    `failed` outcome but lets the wizard wire the host and print
    the next step.
    """
    cfg = ctx.config

    if cfg.no_entities:
        return StageOutcome(
            stage=3,
            name="entities",
            status="skipped",
            message="--no-entities set; not running entity suggestion",
            next_step="run `schemabrain entities suggest --apply` later to curate entities",
        )

    if cfg.skip_index:
        return StageOutcome(
            stage=3,
            name="entities",
            status="skipped",
            message="skipped because --skip-index is set (no indexed schema to analyse)",
        )

    if not is_postgres_url(cfg.source_url):
        return StageOutcome(
            stage=3,
            name="entities",
            status="skipped",
            message="entity suggestion needs a Postgres source",
        )

    # Already-curated check must run BEFORE the API-key check. An
    # idempotent re-run on a store that already has entities does NOT
    # need ANTHROPIC_API_KEY — but if the env-var check ran first, the
    # user saw "ANTHROPIC_API_KEY not set" on every re-run even though
    # the store was fine, and the closing block surfaced the same
    # misleading recovery hint. Reordering keeps the wizard honest:
    # the env-var prompt fires only when the wizard actually intends
    # to call the LLM.
    source_id = _source_id_for(cfg.source_url)
    if cfg.store_path.exists():
        existing = _peek_entity_count(cfg.store_path, source_id)
        if isinstance(existing, StageOutcome):
            return existing
        if existing > 0:
            return StageOutcome(
                stage=3,
                name="entities",
                status="skipped",
                message=f"already curated: {existing} entity/ies present for this source",
                next_step="run `schemabrain entities suggest --apply` directly "
                "to re-curate from a fresh prompt",
            )

    # PR C: dbt-import branch. Sits before the API-key check because
    # the dbt path doesn't call the LLM. Stage 1 (source_check)
    # populated `ctx.dbt_manifest_path` if a manifest was detected
    # or `--from-dbt` was set.
    if ctx.dbt_manifest_path is not None:
        try:
            result = _run_entities_from_dbt(
                cfg=cfg,
                manifest_path=ctx.dbt_manifest_path,
                source_id=source_id,
            )
        except _DbtImportFailedAtWizard as exc:
            return StageOutcome(
                stage=3,
                name="entities",
                status="failed",
                message=exc.message,
                next_step=exc.next_step,
            )
        plural = "y" if result.applied_count == 1 else "ies"
        if result.applied_count == 0:
            message = (
                f"dbt manifest planned {result.candidates_proposed} entit"
                f"{'y' if result.candidates_proposed == 1 else 'ies'} but "
                f"none could be written"
                if result.candidates_proposed > 0
                else "dbt manifest contained 0 importable models"
            )
            return StageOutcome(
                stage=3,
                name="entities",
                status="failed",
                message=message,
                next_step="check the audit log for write failures, "
                "or omit --from-dbt to fall back to LLM-suggest",
            )
        if result.applied_count < result.candidates_proposed:
            suffix = (
                f"; {result.candidates_proposed - result.applied_count} "
                f"skipped: {result.skip_reason}"
                if result.skip_reason
                else ""
            )
            return StageOutcome(
                stage=3,
                name="entities",
                status="done",
                message=(
                    f"{result.applied_count} of {result.candidates_proposed} "
                    f"entit{plural} imported from dbt{suffix}"
                ),
                next_step="inspect the audit log for skipped entities, or re-run "
                "`schemabrain import dbt --dry-run` to preview",
            )
        return StageOutcome(
            stage=3,
            name="entities",
            status="done",
            message=f"{result.applied_count} entit{plural} imported from dbt",
        )

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return StageOutcome(
            stage=3,
            name="entities",
            status="skipped",
            message="ANTHROPIC_API_KEY not set; entity suggestion skipped",
            next_step="export ANTHROPIC_API_KEY=sk-ant-... and then run "
            "`schemabrain entities suggest --apply`",
        )

    max_cost = _resolve_entities_cost_cap(cfg.entities_max_cost_usd)

    # Interactive confirmation pause before calling the LLM.
    # Auto-suppressed in non-TTY environments (CI, pytest) and when
    # `cfg.skip_llm_confirm` is True (set by `--yes`). User Ctrl-C
    # turns into a `skipped` outcome rather than aborting the wizard.
    if not cfg.skip_llm_confirm and not _prompt_llm_confirmation(
        stage_label="entities", cost_cap_usd=max_cost
    ):
        return StageOutcome(
            stage=3,
            name="entities",
            status="skipped",
            message="user cancelled the LLM call",
            next_step="re-run with `--yes` to skip the prompt, "
            "or run `schemabrain entities suggest --apply` directly",
        )

    try:
        result = _run_entity_suggestion(
            cfg=cfg,
            source_id=source_id,
            api_key=api_key,
            max_cost_usd=max_cost,
        )
    except _CostCeilingExceededAtWizard as exc:
        return StageOutcome(
            stage=3,
            name="entities",
            status="failed",
            message=exc.message,
            next_step="re-run with `--entities-max-cost-usd N` for a higher cap, "
            "or `--no-entities` to skip",
        )
    except _SuggestionParseAtWizard as exc:
        return StageOutcome(
            stage=3,
            name="entities",
            status="failed",
            message=f"LLM returned unparseable response: {exc.message}",
            next_step="re-run — transient LLM hiccups usually clear",
        )
    except _LLMClientErrorAtWizard as exc:
        return StageOutcome(
            stage=3,
            name="entities",
            status="failed",
            message=f"LLM call failed: {exc.message}",
            next_step=_llm_failure_next_step(
                exc.kind,
                apply_command="schemabrain entities apply",
            ),
        )
    except _EmptySchemaAtWizard:
        return StageOutcome(
            stage=3,
            name="entities",
            status="skipped",
            message="no indexed tables for this source — nothing to analyse",
            next_step="re-run `schemabrain init` after the schema has tables",
        )

    if result.applied_count == 0:
        return StageOutcome(
            stage=3,
            name="entities",
            status="failed",
            message=(
                f"LLM returned {result.candidates_proposed} candidates but "
                f"none could be applied (cost ${result.cost_usd:.4f})"
            )
            if result.candidates_proposed > 0
            else f"LLM returned 0 candidates (cost ${result.cost_usd:.4f})",
            next_step="re-run `schemabrain entities suggest --dry-run` to inspect, "
            "or curate entities by hand via `schemabrain entities apply`",
        )

    if result.candidates_proposed > 0 and result.applied_count < result.candidates_proposed:
        # Partial-success: writes started failing mid-loop. Surface
        # the count + reason so the user can act, instead of letting
        # "3 entities created" mask "5 of 8 were dropped".
        suffix = (
            f"; {result.candidates_proposed - result.applied_count} skipped: {result.skip_reason}"
            if result.skip_reason
            else ""
        )
        return StageOutcome(
            stage=3,
            name="entities",
            status="done",
            message=(
                f"{result.applied_count} of {result.candidates_proposed} "
                f"entit{'y' if result.applied_count == 1 else 'ies'} created "
                f"(cost ${result.cost_usd:.4f}{suffix})"
            ),
            next_step="inspect the skipped candidates with "
            "`schemabrain entities suggest --dry-run` and apply manually if needed",
        )

    return StageOutcome(
        stage=3,
        name="entities",
        status="done",
        message=(
            f"{result.applied_count} "
            f"entit{'y' if result.applied_count == 1 else 'ies'} created "
            f"(cost ${result.cost_usd:.4f})"
        ),
    )


@dataclass(frozen=True)
class _EntityApplyResult:
    """Internal outcome of `_run_entity_suggestion` or `_run_entities_from_dbt`.

    `applied_count` is the number of entities that landed in the
    store (writes commit per call; a partial-success run reports
    the count before the first failure). `candidates_proposed` is
    what the LLM produced — when it exceeds `applied_count`, writes
    started failing mid-loop and `skip_reason` carries the exception
    name + the candidate that triggered it so the renderer can
    surface partial state.

    `source` distinguishes the LLM path from the dbt-import path
    (PR C). In dbt mode, `cost_usd` is always 0.0 and `llm_model`
    carries the sentinel `"dbt-import"` — the stage handler uses
    `source` to render different message text rather than
    string-matching the sentinel.
    """

    applied_count: int
    cost_usd: float
    llm_model: str
    candidates_proposed: int = 0
    skip_reason: str | None = None
    source: Literal["llm", "dbt"] = "llm"


# Wizard-internal exception wrappers. The pipeline's
# `CostCeilingExceededError` and `SuggestionParseError` are caught at
# the `_run_entity_suggestion` boundary and re-raised as these so
# the stage handler's `except` clauses don't depend on importing the
# pipeline's exception classes at module scope (they live behind a
# lazy import).


class _CostCeilingExceededAtWizard(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class _SuggestionParseAtWizard(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class _EmptySchemaAtWizard(Exception):
    pass


class _LLMClientErrorAtWizard(Exception):
    """Wraps any LLM client-side failure surfaced from `propose_from_*`.

    Today's known triggers: the bare `RuntimeError` raised by
    `enrichment.anthropic_client._extract_text` when the model hits
    `max_tokens` or returns a non-text first block. Also catches
    network / SDK errors (HTTP timeouts, rate-limit `APIError`
    subclasses) that aren't classified by the more-specific
    `CostCeilingExceededError` / `SuggestionParseError` catches.

    Without this wrapper, those exceptions propagate through
    `pipeline.propose_from_*` → `_run_*_suggestion` → stage handler →
    `run_wizard` → `_cmd_init` → user sees an unhandled traceback,
    which violates the documented "best-effort" contract for stages
    3 / 4 / 5 (failure records a guided StageOutcome, doesn't abort
    the wizard).

    F5 (post-PR-#79 polish): carries an optional `kind` field set by
    the call site using ``errors_render.classify_llm_failure``. When
    present, the stage handler picks a kind-specific `next_step`
    instead of the generic "re-run, transient errors usually clear"
    fallback — a 529 overload gets "wait 30-60s and retry", a
    connection error gets "check network/proxy", and so on. `None`
    preserves the pre-F5 generic behavior for non-Anthropic causes
    (e.g. `max_tokens` RuntimeError from the SDK extractor).
    """

    def __init__(self, message: str, kind: LlmFailureKind | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind


def _llm_failure_next_step(
    kind: LlmFailureKind | None,
    *,
    apply_command: str,
) -> str:
    """Pick kind-specific `next_step` copy for a failed StageOutcome.

    Kept beside `_LLMClientErrorAtWizard` so the kind→hint mapping
    is local to one module. `apply_command` is the manual-curation
    escape hatch surfaced in the fallback branch (e.g.
    ``"schemabrain entities apply"``); kind-specific branches don't
    need it because they recommend a wait + retry, not manual work.

    The pre-F5 generic copy is the fallback (``kind=None``) and
    covers non-Anthropic causes — most notably the ``max_tokens``
    ``RuntimeError`` raised by
    ``enrichment.anthropic_client._extract_text``, which the
    classifier returns ``None`` for (RuntimeError, not an SDK-typed
    error).
    """
    # Handle the documented `kind is None` fallback FIRST, then
    # dispatch on the four known kinds. With a permissive shape, an
    # unknown kind (typo or a new `LlmFailureKind` value forgotten
    # here) would silently fall into the None branch and return the
    # max_tokens-style copy — making this the only dispatch across
    # the three parallel per-kind functions that didn't loudly fail
    # on a typo (`_llm_failure_titles` and `_llm_failure_retry_hint` in
    # `errors_render.py` raise on unknown kinds; this one didn't).
    if kind is None:
        return (
            "re-run — transient LLM/network errors usually clear. "
            "If the message mentions `max_tokens`, the model went off-prompt; "
            f"re-run or curate by hand via `{apply_command}`."
        )
    if kind == "overloaded":
        return (
            "Anthropic is overloaded — wait 30-60s and re-run. "
            "See https://status.anthropic.com if it persists."
        )
    if kind == "rate_limited":
        return (
            "rate-limited — wait and re-run; consider lowering SCHEMABRAIN_*_CONCURRENCY env vars."
        )
    if kind == "connection":
        return "couldn't reach Anthropic — check network / proxy / DNS, then re-run."
    if kind == "api_error":
        return (
            "Anthropic returned an error — re-run later; if it persists, check the message above."
        )
    raise ValueError(
        f"unknown kind {kind!r}; expected one of "
        "'overloaded' | 'rate_limited' | 'connection' | 'api_error' | None. "
        "Add the new kind to `LlmFailureKind` (in errors_render.py) and update "
        "this dispatch + `_llm_failure_titles` + `_llm_failure_retry_hint` "
        "together so all three stay aligned."
    )


class _DbtImportFailedAtWizard(Exception):
    """Wraps `DbtManifestParseError` / `DbtMetricImportError` /
    `OperationalError` raised inside the dbt-import path (PR C).

    `message` is the human-readable error, `next_step` is the
    recovery hint to surface in the failed `StageOutcome`. Stage
    handlers catch this and translate into a `StageOutcome` of
    `status="failed"`.
    """

    def __init__(self, message: str, next_step: str) -> None:
        super().__init__(message)
        self.message = message
        self.next_step = next_step


def _peek_entity_count(store_path: Path, source_id: str) -> int | StageOutcome:
    """Open the store read-only and count entities for `source_id`.

    Same shape as `_peek_store_table_count` — returns the count, or a
    `StageOutcome(failed)` on schema-version mismatch or `OSError`
    from the store file being unreadable.
    """
    from schemabrain.core.store import SchemaVersionMismatchError, SQLiteStore

    try:
        with SQLiteStore(path=store_path) as store:
            return len(store.list_entities(source_connection_id=source_id))
    except SchemaVersionMismatchError as exc:
        return StageOutcome(
            stage=3,
            name="entities",
            status="failed",
            message=str(exc),
            next_step=f"delete {store_path} and re-run, "
            "or install the matching schemabrain version",
        )
    except OSError as exc:
        return StageOutcome(
            stage=3,
            name="entities",
            status="failed",
            message=f"store unreadable: {exc}",
            next_step=f"check filesystem permissions on {store_path.parent}",
        )


def _resolve_entities_cost_cap(flag_value: float | None) -> float:
    """Pick the effective entity-suggest cost cap.

    Priority: explicit `--entities-max-cost-usd` > env var > package
    default. The env var name matches what `entities suggest` reads
    so the user's chosen ceiling carries across both surfaces.

    Uses `warn_and_default` on invalid env values — the wizard is an
    interactive flow and shouldn't abort because of a leftover env
    var; a stderr warning + fallback to the package default is the
    right UX. Same posture applies to `_resolve_metrics_cost_cap`.
    """
    if flag_value is not None:
        return flag_value
    return resolve_positive_float_env(
        "SCHEMABRAIN_MAX_LLM_COST_USD",
        _WIZARD_ENTITIES_DEFAULT_COST_CAP_USD,
        on_invalid="warn_and_default",
        default_display=f"${_WIZARD_ENTITIES_DEFAULT_COST_CAP_USD:.2f} cap",
    )


def _run_entity_suggestion(
    *,
    cfg: WizardConfig,
    source_id: str,
    api_key: str,
    max_cost_usd: float,
) -> _EntityApplyResult:
    """Build the suggestion pipeline, propose candidates, apply them.

    Translates the pipeline's exception classes into wizard-internal
    wrappers (`_CostCeilingExceededAtWizard`, `_SuggestionParseAtWizard`,
    `_EmptySchemaAtWizard`) so the stage handler doesn't need a
    module-level import of the pipeline's package.

    Writes commit per `store.write_entity` call. A partial-success
    write loop (the LLM proposed a table not in the store, or a name
    collides with a dbt-imported entity) breaks out and returns the
    count of entities that landed before the first failure.
    """
    from sqlite3 import IntegrityError

    from schemabrain._ui import live_llm_stage_progress, make_console
    from schemabrain.core.store import DbtOwnedEntityError, SQLiteStore
    from schemabrain.enrichment.anthropic_client import anthropic_sonnet_46_client
    from schemabrain.entities.suggest import (
        CostCeilingExceededError,
        CostCeilingGuard,
        EntitySuggestionPipeline,
        SuggestionParseError,
    )

    llm = anthropic_sonnet_46_client(api_key=api_key)
    guard = CostCeilingGuard(inner=llm, max_cost_usd=max_cost_usd)
    pipeline = EntitySuggestionPipeline(llm=guard)

    with SQLiteStore(cfg.store_path) as store:
        names = store.list_tables(source_connection_id=source_id)
        if not names:
            raise _EmptySchemaAtWizard()
        tables = []
        for schema, name in names:
            table = store.get_table(schema, name, source_connection_id=source_id)
            if table is not None:
                tables.append(table)
        if not tables:
            raise _EmptySchemaAtWizard()

        # D1 (post-PR-#79 polish): wrap the LLM round-trip in a Rich
        # ``Live`` display that shows the cost preview AND an
        # auto-updating elapsed timer. Pre-D1, the wizard printed a
        # one-line static preamble (PR-1 c6c899e) then went silent
        # for the ~30s LLM call — operators couldn't tell whether the
        # call was still in flight or hung. The Live timer ticks
        # every 0.5s so the operator gets continuous feedback.
        # Non-TTY (CI / piped stderr) falls back to the static
        # preamble shape automatically.
        progress = live_llm_stage_progress(
            make_console(stderr=True),
            action=f"identify business entities ({len(tables)} tables)",
            model="claude-sonnet-4-6",
            cost_estimate_usd=0.01,
            cap_usd=max_cost_usd,
        )

        try:
            with progress:
                result = pipeline.propose_from_tables(tables)
        except CostCeilingExceededError as exc:
            raise _CostCeilingExceededAtWizard(str(exc)) from exc
        except SuggestionParseError as exc:
            raise _SuggestionParseAtWizard(str(exc)) from exc
        except ValueError:
            # Defensive: `propose_from_tables` raises ValueError on
            # empty inputs / negative top_k — the `_EmptySchemaAtWizard`
            # guard above makes these unreachable today, but re-
            # raising keeps any future drift loud rather than hidden
            # behind a misleading "LLM call failed" message.
            raise  # pragma: no cover — defensive; guarded unreachable today
        except Exception as exc:
            # Narrow scope: only the LLM round-trip is inside this try.
            # Local validation, store reads, and the apply-loop happen
            # outside, so a bug there still surfaces as a traceback (we
            # want loud failure on local programming bugs). Anything
            # escaping `propose_from_tables` that isn't already typed is
            # by elimination an LLM-client or network failure — surface
            # it as a structured `_LLMClientErrorAtWizard` so the stage
            # handler can produce a failed StageOutcome instead of
            # letting the wizard crash mid-pipeline. `repr(exc)`
            # fallback (instead of `type(exc).__name__`) preserves
            # argument visibility when `str(exc)` is empty.
            # F5: classify the SDK exception so the stage handler can
            # pick kind-specific next_step copy (529 → "wait 30-60s",
            # connection → "check network", etc.).
            raise _LLMClientErrorAtWizard(
                str(exc) or repr(exc),
                kind=classify_llm_failure(exc),
            ) from exc

        applied = 0
        skip_reason: str | None = None
        for candidate in result.candidates:
            try:
                store.write_entity(candidate.entity, source_connection_id=source_id)
            except (DbtOwnedEntityError, IntegrityError) as exc:
                skip_reason = f"{type(exc).__name__} at '{candidate.entity.name}'"
                break
            applied += 1

    return _EntityApplyResult(
        applied_count=applied,
        cost_usd=result.total_cost_usd,
        llm_model=result.llm_model,
        candidates_proposed=len(result.candidates),
        skip_reason=skip_reason,
    )


# Stage-3 cost cap default. Kept aligned with the standalone
# `entities suggest` CLI's default so a wizard run and a direct
# `entities suggest` run see the same ceiling.
_WIZARD_ENTITIES_DEFAULT_COST_CAP_USD: float = 1.0


# Sentinel `llm_model` value carried in `_EntityApplyResult` /
# `_MetricApplyResult` when the dbt-import path produced the row.
# The stage handler reads `result.source` (not this sentinel) for
# branching the message; this literal is a human-readable artifact
# only — surfaces in audit-log entries and `--json` output mode if
# either consumer ever cares.
_DBT_IMPORT_MODEL_LABEL: str = "dbt-import"


def _run_entities_from_dbt(
    *,
    cfg: WizardConfig,
    manifest_path: Path,
    source_id: str,
) -> _EntityApplyResult:
    """Import entities from a compiled dbt manifest.

    Mirror of `_run_entity_suggestion` for the dbt path. Calls
    `parse_dbt_manifest` → `plan_dbt_import` → `apply_dbt_import_plan`
    against the live Postgres source (for column verification) and
    the local SQLite store.

    Translates `DbtManifestParseError`, `DbtImportError`, and
    `OperationalError` into `_DbtImportFailedAtWizard` so the stage
    handler renders a clean `failed` outcome instead of an
    unhandled traceback.

    `applied_count` totals successful writes across all three
    writable buckets (`to_add` + `to_update` + `to_take_ownership`)
    minus any per-entity write failures. `candidates_proposed` is
    the planned-writes count so the partial-success branch of the
    stage handler renders the same "N of M" message format the
    LLM path uses.
    """
    from sqlalchemy.exc import OperationalError as _SAOperationalError

    from schemabrain.connectors.postgres import PostgresDataSource
    from schemabrain.core.store import SQLiteStore
    from schemabrain.imports.dbt import (
        DbtManifestParseError,
        apply_dbt_import_plan,
        parse_dbt_manifest,
        plan_dbt_import,
    )

    try:
        manifest = parse_dbt_manifest(manifest_path)
    except DbtManifestParseError as exc:
        raise _DbtImportFailedAtWizard(
            f"dbt manifest unreadable: {exc}",
            next_step="run `dbt compile` in the dbt project to regenerate "
            "target/manifest.json, or omit --from-dbt to fall back to LLM-suggest",
        ) from exc

    try:
        with (
            SQLiteStore(cfg.store_path) as store,
            PostgresDataSource(cfg.source_url) as source,
        ):
            plan = plan_dbt_import(manifest, source, store, source_connection_id=source_id)
            result = apply_dbt_import_plan(plan, store, source_connection_id=source_id)
    except _SAOperationalError as exc:
        raise _DbtImportFailedAtWizard(
            f"Postgres connection failed during dbt import: {exc}",
            next_step="verify the source URL + credentials, then re-run",
        ) from exc

    planned_writes = len(plan.to_add) + len(plan.to_update) + len(plan.to_take_ownership)
    applied_count = planned_writes - len(result.write_failures)
    skip_reason: str | None = None
    if result.write_failures:
        first = result.write_failures[0]
        skip_reason = f"{first.entity_name}: {first.message}"

    return _EntityApplyResult(
        applied_count=applied_count,
        cost_usd=0.0,
        llm_model=_DBT_IMPORT_MODEL_LABEL,
        candidates_proposed=planned_writes,
        skip_reason=skip_reason,
        source="dbt",
    )


# ----- stage 4: metrics ----------------------------------------------------


def _stage_metrics(ctx: WizardContext) -> StageOutcome:
    """Suggest + apply metrics via the Anthropic-backed pipeline.

    Mirror of `_stage_entities` adapted for metrics. The metric model
    requires every metric to bind to an existing entity, so the
    decision tree adds one branch on top of the entity tree: if the
    entity store is empty, the stage skips with a pointer at
    `entities suggest --apply` (running the pipeline against zero
    entities would either crash or hallucinate).

    Decision tree:

      1. `cfg.no_metrics` → skipped (explicit opt-out).
      2. `cfg.skip_index` → skipped (no schema to analyse).
      3. Non-Postgres source → skipped (metrics need an indexed
         Postgres schema today).
      4. Store already has metrics for this `source_id` → skipped
         (idempotent re-run). Done BEFORE the API-key + empty-entity
         checks so a re-run on a fully curated store doesn't moan
         about a missing key.
      5. Entity store empty for this `source_id` → skipped with hint
         pointing at entity curation first. This is the cross-stage
         dependency PR A introduces: metrics anchor on entities, and
         an empty entity store means there is nothing for the LLM to
         anchor on. Running the pipeline anyway would yield
         `propose_from_entities` raising `ValueError("requires at
         least one entity")`.
      6. `ANTHROPIC_API_KEY` missing → skipped with hint.
      7. Otherwise, run the pipeline + apply candidates and emit
         `done` with the applied count + cost.

    Like stage 3 (entities), stage 4 (metrics) is `abort_on_fail=False`:
    a failure here records a `failed` outcome but lets the wizard
    wire the host and print the next step.
    """
    cfg = ctx.config

    if cfg.no_metrics:
        return StageOutcome(
            stage=4,
            name="metrics",
            status="skipped",
            message="--no-metrics set; not running metric suggestion",
            next_step="run `schemabrain metrics suggest --apply` later to curate metrics",
        )

    if cfg.skip_index:
        return StageOutcome(
            stage=4,
            name="metrics",
            status="skipped",
            message="skipped because --skip-index is set (no indexed schema to analyse)",
        )

    if not is_postgres_url(cfg.source_url):
        return StageOutcome(
            stage=4,
            name="metrics",
            status="skipped",
            message="metric suggestion needs a Postgres source",
        )

    # Already-curated check runs BEFORE the API-key + empty-entity
    # checks. An idempotent re-run on a fully curated store does NOT
    # need ANTHROPIC_API_KEY and does NOT need to re-validate entities
    # — running the pipeline would just collide with the canonical
    # set. Same reordering rationale as stage 3.
    source_id = _source_id_for(cfg.source_url)
    if cfg.store_path.exists():
        existing = _peek_metric_count(cfg.store_path, source_id)
        if isinstance(existing, StageOutcome):
            return existing
        if existing > 0:
            return StageOutcome(
                stage=4,
                name="metrics",
                status="skipped",
                message=f"already curated: {existing} metric/s present for this source",
                next_step="run `schemabrain metrics suggest --apply` directly "
                "to re-curate from a fresh prompt",
            )

    # Cross-stage dependency: metrics anchor on entities. An empty
    # entity store means the pipeline has nothing to anchor on. The
    # peek mirrors `_peek_entity_count`; we reuse it directly.
    if cfg.store_path.exists():
        entity_count = _peek_entity_count(cfg.store_path, source_id)
        if isinstance(entity_count, StageOutcome):
            # Translate the stage-3-shaped outcome into a stage-4
            # outcome so the renderer's stage label matches the stage
            # the error is being reported under.
            return StageOutcome(
                stage=4,
                name="metrics",
                status=entity_count.status,
                message=entity_count.message,
                next_step=entity_count.next_step,
            )
        if entity_count == 0:
            return StageOutcome(
                stage=4,
                name="metrics",
                status="skipped",
                message="entity store is empty; metrics need entities to anchor on",
                next_step="run `schemabrain entities suggest --apply` first, "
                "then re-run `schemabrain init` (or `schemabrain metrics suggest --apply`)",
            )
    else:
        # No store file at all means stage 2 (index) was skipped AND
        # the store doesn't exist yet. Treat the same as the
        # empty-entity case — there are no entities to anchor on.
        return StageOutcome(
            stage=4,
            name="metrics",
            status="skipped",
            message="entity store is empty; metrics need entities to anchor on",
            next_step="run `schemabrain entities suggest --apply` first, "
            "then re-run `schemabrain init` (or `schemabrain metrics suggest --apply`)",
        )

    # PR C: dbt-import branch. Mirror of the entities-stage dbt
    # branch. Sits before the API-key check because the dbt path
    # doesn't call the LLM.
    if ctx.dbt_manifest_path is not None:
        try:
            result = _run_metrics_from_dbt(
                cfg=cfg,
                manifest_path=ctx.dbt_manifest_path,
                source_id=source_id,
            )
        except _DbtImportFailedAtWizard as exc:
            return StageOutcome(
                stage=4,
                name="metrics",
                status="failed",
                message=exc.message,
                next_step=exc.next_step,
            )
        except _EmptySchemaAtWizard:
            # Defensive: entities were dropped between the peek and
            # the dbt-metrics open. Same handling as the LLM path.
            return StageOutcome(
                stage=4,
                name="metrics",
                status="skipped",
                message="entity store became empty between checks — re-run after entities curate",
                next_step="run `schemabrain entities suggest --apply` first, then re-run",
            )
        plural = "" if result.applied_count == 1 else "s"
        if result.applied_count == 0:
            message = (
                f"dbt manifest planned {result.candidates_proposed} "
                f"metric{'' if result.candidates_proposed == 1 else 's'} but "
                f"none could be written"
                if result.candidates_proposed > 0
                else "dbt manifest contained 0 importable simple metrics"
            )
            return StageOutcome(
                stage=4,
                name="metrics",
                status="failed",
                message=message,
                next_step="check the audit log for write failures, "
                "or omit --from-dbt to fall back to LLM-suggest",
            )
        if result.applied_count < result.candidates_proposed:
            suffix = (
                f"; {result.candidates_proposed - result.applied_count} "
                f"skipped: {result.skip_reason}"
                if result.skip_reason
                else ""
            )
            return StageOutcome(
                stage=4,
                name="metrics",
                status="done",
                message=(
                    f"{result.applied_count} of {result.candidates_proposed} "
                    f"metric{plural} imported from dbt{suffix}"
                ),
                next_step="inspect the audit log for skipped metrics, or re-run "
                "`schemabrain import dbt --include-metrics --dry-run` to preview",
            )
        return StageOutcome(
            stage=4,
            name="metrics",
            status="done",
            message=f"{result.applied_count} metric{plural} imported from dbt",
        )

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return StageOutcome(
            stage=4,
            name="metrics",
            status="skipped",
            message="ANTHROPIC_API_KEY not set; metric suggestion skipped",
            next_step="export ANTHROPIC_API_KEY=sk-ant-... and then run "
            "`schemabrain metrics suggest --apply`",
        )

    max_cost = _resolve_metrics_cost_cap(cfg.metrics_max_cost_usd)

    # Interactive confirmation pause before calling the LLM. Same
    # auto-suppression contract as _stage_entities: non-TTY stdin OR
    # `cfg.skip_llm_confirm=True` bypasses the prompt.
    if not cfg.skip_llm_confirm and not _prompt_llm_confirmation(
        stage_label="metrics", cost_cap_usd=max_cost
    ):
        return StageOutcome(
            stage=4,
            name="metrics",
            status="skipped",
            message="user cancelled the LLM call",
            next_step="re-run with `--yes` to skip the prompt, "
            "or run `schemabrain metrics suggest --apply` directly",
        )

    try:
        result = _run_metric_suggestion(
            cfg=cfg,
            source_id=source_id,
            api_key=api_key,
            max_cost_usd=max_cost,
        )
    except _CostCeilingExceededAtWizard as exc:
        return StageOutcome(
            stage=4,
            name="metrics",
            status="failed",
            message=exc.message,
            next_step="re-run with `--metrics-max-cost-usd N` for a higher cap, "
            "or `--no-metrics` to skip",
        )
    except _SuggestionParseAtWizard as exc:
        return StageOutcome(
            stage=4,
            name="metrics",
            status="failed",
            message=f"LLM returned unparseable response: {exc.message}",
            next_step="re-run — transient LLM hiccups usually clear",
        )
    except _LLMClientErrorAtWizard as exc:
        return StageOutcome(
            stage=4,
            name="metrics",
            status="failed",
            message=f"LLM call failed: {exc.message}",
            next_step=_llm_failure_next_step(
                exc.kind,
                apply_command="schemabrain metrics apply",
            ),
        )
    except _EmptySchemaAtWizard:
        return StageOutcome(
            stage=4,
            name="metrics",
            status="skipped",
            message="no indexed tables for this source — nothing to analyse",
            next_step="re-run `schemabrain init` after the schema has tables",
        )

    if result.applied_count == 0:
        return StageOutcome(
            stage=4,
            name="metrics",
            status="failed",
            message=(
                f"LLM returned {result.candidates_proposed} candidates but "
                f"none could be applied (cost ${result.cost_usd:.4f})"
            )
            if result.candidates_proposed > 0
            else f"LLM returned 0 candidates (cost ${result.cost_usd:.4f})",
            next_step="re-run `schemabrain metrics suggest --dry-run` to inspect, "
            "or curate metrics by hand via `schemabrain metrics apply`",
        )

    if result.candidates_proposed > 0 and result.applied_count < result.candidates_proposed:
        # Partial-success — same shape as stage 3 reports.
        suffix = (
            f"; {result.candidates_proposed - result.applied_count} skipped: {result.skip_reason}"
            if result.skip_reason
            else ""
        )
        return StageOutcome(
            stage=4,
            name="metrics",
            status="done",
            message=(
                f"{result.applied_count} of {result.candidates_proposed} "
                f"metric{'' if result.applied_count == 1 else 's'} created "
                f"(cost ${result.cost_usd:.4f}{suffix})"
            ),
            next_step="inspect the skipped candidates with "
            "`schemabrain metrics suggest --dry-run` and apply manually if needed",
        )

    return StageOutcome(
        stage=4,
        name="metrics",
        status="done",
        message=(
            f"{result.applied_count} "
            f"metric{'' if result.applied_count == 1 else 's'} created "
            f"(cost ${result.cost_usd:.4f})"
        ),
    )


@dataclass(frozen=True)
class _MetricApplyResult:
    """Internal outcome of `_run_metric_suggestion` or `_run_metrics_from_dbt`.

    Mirrors `_EntityApplyResult` field-for-field — see that dataclass
    for the partial-success semantics. The `skip_reason` carries the
    exception name + the candidate name that triggered it when writes
    started failing mid-loop (typically `DbtOwnedMetricError` on a
    name collision with a dbt-imported metric).

    `source` distinguishes the LLM path from the dbt-import path
    (PR C). Same convention as `_EntityApplyResult`.
    """

    applied_count: int
    cost_usd: float
    llm_model: str
    candidates_proposed: int = 0
    skip_reason: str | None = None
    source: Literal["llm", "dbt"] = "llm"


def _peek_metric_count(store_path: Path, source_id: str) -> int | StageOutcome:
    """Open the store read-only and count metrics for `source_id`.

    Mirror of `_peek_entity_count`. The returned `StageOutcome` on
    failure carries `stage=4, name="metrics"` so the renderer routes
    the line to the metrics stage label.
    """
    from schemabrain.core.store import SchemaVersionMismatchError, SQLiteStore

    try:
        with SQLiteStore(path=store_path) as store:
            return len(store.list_metrics(source_connection_id=source_id))
    except SchemaVersionMismatchError as exc:
        return StageOutcome(
            stage=4,
            name="metrics",
            status="failed",
            message=str(exc),
            next_step=f"delete {store_path} and re-run, "
            "or install the matching schemabrain version",
        )
    except OSError as exc:
        return StageOutcome(
            stage=4,
            name="metrics",
            status="failed",
            message=f"store unreadable: {exc}",
            next_step=f"check filesystem permissions on {store_path.parent}",
        )


def _resolve_metrics_cost_cap(flag_value: float | None) -> float:
    """Pick the effective metric-suggest cost cap.

    Priority mirrors `_resolve_entities_cost_cap`: explicit
    `--metrics-max-cost-usd` > `SCHEMABRAIN_MAX_LLM_COST_USD` env var
    > package default. The env var is shared across all aspirational
    LLM stages — operators who set it once cap every stage at the
    same per-stage ceiling.
    """
    if flag_value is not None:
        return flag_value
    return resolve_positive_float_env(
        "SCHEMABRAIN_MAX_LLM_COST_USD",
        _WIZARD_METRICS_DEFAULT_COST_CAP_USD,
        on_invalid="warn_and_default",
        default_display=f"${_WIZARD_METRICS_DEFAULT_COST_CAP_USD:.2f} cap",
    )


def _run_metric_suggestion(
    *,
    cfg: WizardConfig,
    source_id: str,
    api_key: str,
    max_cost_usd: float,
) -> _MetricApplyResult:
    """Build the metric pipeline, propose candidates, apply them.

    Mirror of `_run_entity_suggestion`. Pulls entities + tables from
    the store, hands them to `MetricSuggestionPipeline.propose_from_entities`,
    walks the candidates and writes each via `store.write_metric`.

    Translates the pipeline's exception classes into wizard-internal
    wrappers shared with stage 3 (`_CostCeilingExceededAtWizard`,
    `_SuggestionParseAtWizard`, `_EmptySchemaAtWizard`).
    """
    from sqlite3 import IntegrityError

    from schemabrain._ui import live_llm_stage_progress, make_console
    from schemabrain.core.store import DbtOwnedMetricError, SQLiteStore
    from schemabrain.enrichment.anthropic_client import anthropic_sonnet_46_client
    from schemabrain.entities.suggest import (
        CostCeilingExceededError,
        CostCeilingGuard,
        SuggestionParseError,
    )
    from schemabrain.metrics.suggest import (
        MetricSuggestionParseError,
        MetricSuggestionPipeline,
    )

    llm = anthropic_sonnet_46_client(api_key=api_key)
    guard = CostCeilingGuard(inner=llm, max_cost_usd=max_cost_usd)
    pipeline = MetricSuggestionPipeline(llm=guard)

    with SQLiteStore(cfg.store_path) as store:
        entities = store.list_entities(source_connection_id=source_id)
        if not entities:
            # Caller already gated on `_peek_entity_count == 0`. This
            # is the defensive race — entities were dropped between
            # the peek and the open. Treat the same as empty schema.
            raise _EmptySchemaAtWizard()
        # Pull every table the entities bind to. A binding's
        # qualified_table is "schema.name"; the pipeline's
        # `serialize_entities_for_llm` indexes tables by qualified_name,
        # so we hand it whatever the store has and let the serializer
        # drop entities whose tables are missing (loudly, via warning).
        names = store.list_tables(source_connection_id=source_id)
        if not names:
            raise _EmptySchemaAtWizard()
        tables = []
        for schema, name in names:
            table = store.get_table(schema, name, source_connection_id=source_id)
            if table is not None:
                tables.append(table)
        if not tables:
            raise _EmptySchemaAtWizard()

        # D1 (post-PR-#79 polish): wrap in Rich Live with elapsed
        # timer — same shape as the entities stage. Metrics
        # suggestion is typically a bit more expensive than entities
        # (more context tokens — entity list + table schemas — so the
        # estimate is 2x the entities default).
        progress = live_llm_stage_progress(
            make_console(stderr=True),
            action=f"define metrics ({len(entities)} entities, {len(tables)} tables)",
            model="claude-sonnet-4-6",
            cost_estimate_usd=0.02,
            cap_usd=max_cost_usd,
        )

        try:
            with progress:
                result = pipeline.propose_from_entities(entities, tables)
        except CostCeilingExceededError as exc:
            raise _CostCeilingExceededAtWizard(str(exc)) from exc
        except (SuggestionParseError, MetricSuggestionParseError) as exc:
            # `MetricSuggestionParseError` is the metrics-side parse
            # error (a sibling of entities.suggest.SuggestionParseError,
            # not a subclass). Without explicitly catching it here, a
            # bad-LLM-metric-YAML hit fell into the `except Exception`
            # net below and was misclassified as an LLM client error
            # — the user saw a max_tokens recovery hint when the real
            # fix was a retry. Both branches converge on the same
            # `_SuggestionParseAtWizard` translation so the stage
            # handler shows the same "transient LLM hiccups" hint.
            raise _SuggestionParseAtWizard(str(exc)) from exc
        except ValueError:
            # Defensive: `propose_from_entities` raises ValueError on
            # empty inputs / negative top_k — the guards earlier in
            # this function make these unreachable today, but re-
            # raising keeps any future drift loud rather than hidden
            # behind a misleading "LLM call failed" message.
            raise  # pragma: no cover — defensive; guarded unreachable today
        except Exception as exc:
            # Same narrow-scope LLM-error wrapper as stage 3 — see
            # `_run_entity_suggestion` for the rationale. Re-raising as
            # `_LLMClientErrorAtWizard` keeps stage 4's failure path
            # structured (failed StageOutcome with recovery hint)
            # rather than crashing the wizard mid-pipeline. `repr(exc)`
            # fallback (instead of `type(exc).__name__`) preserves
            # argument visibility when `str(exc)` is empty, so a bare
            # `RuntimeError()` surfaces as "RuntimeError()" not just
            # "RuntimeError" — better signal for the operator.
            # F5: classify SDK exceptions so stage 4 can pick kind-
            # specific next_step copy (mirror of stage 3's handler).
            raise _LLMClientErrorAtWizard(
                str(exc) or repr(exc),
                kind=classify_llm_failure(exc),
            ) from exc

        applied = 0
        skip_reason: str | None = None
        for candidate in result.candidates:
            try:
                store.write_metric(candidate.metric, source_connection_id=source_id)
            except (DbtOwnedMetricError, IntegrityError) as exc:
                skip_reason = f"{type(exc).__name__} at '{candidate.metric.name}'"
                break
            applied += 1

    return _MetricApplyResult(
        applied_count=applied,
        cost_usd=result.total_cost_usd,
        llm_model=result.llm_model,
        candidates_proposed=len(result.candidates),
        skip_reason=skip_reason,
    )


# Stage-4 cost cap default. Kept aligned with the standalone
# `metrics suggest` CLI's default so a wizard run and a direct
# `metrics suggest` run see the same ceiling.
_WIZARD_METRICS_DEFAULT_COST_CAP_USD: float = 0.5


def _run_metrics_from_dbt(
    *,
    cfg: WizardConfig,
    manifest_path: Path,
    source_id: str,
) -> _MetricApplyResult:
    """Import metrics from a compiled dbt manifest.

    Mirror of `_run_metric_suggestion` for the dbt path. Calls
    `parse_dbt_metrics` against the manifest, scoped to the entity
    names already present in the store (set by stage 3, whether via
    dbt import or LLM-suggest). Writes each `Metric` via
    `store.write_metric` with the same per-call commit + partial-
    success semantics the LLM path uses.

    Translates `DbtMetricImportError` into `_DbtImportFailedAtWizard`
    so the stage handler renders a clean `failed` outcome.
    """
    from sqlite3 import IntegrityError

    from schemabrain.core.store import DbtOwnedMetricError, SQLiteStore
    from schemabrain.imports.dbt_metrics import DbtMetricImportError, parse_dbt_metrics

    with SQLiteStore(cfg.store_path) as store:
        entity_names = {e.name for e in store.list_entities(source_connection_id=source_id)}
        if not entity_names:
            # Defensive race: caller's `_peek_entity_count > 0` guard
            # passed, but entities were dropped between the peek and
            # the open. Same shape as the LLM path treats this.
            raise _EmptySchemaAtWizard()

        try:
            metrics, _skipped = parse_dbt_metrics(manifest_path, imported_entity_names=entity_names)
        except DbtMetricImportError as exc:
            raise _DbtImportFailedAtWizard(
                f"dbt metric import failed: {exc}",
                next_step="run `dbt compile` to regenerate target/manifest.json, "
                "or omit --from-dbt to fall back to LLM-suggest",
            ) from exc

        applied = 0
        skip_reason: str | None = None
        for metric in metrics:
            try:
                store.write_metric(metric, source_connection_id=source_id)
            except (DbtOwnedMetricError, IntegrityError) as exc:
                skip_reason = f"{type(exc).__name__} at '{metric.name}'"
                break
            applied += 1

    return _MetricApplyResult(
        applied_count=applied,
        cost_usd=0.0,
        llm_model=_DBT_IMPORT_MODEL_LABEL,
        candidates_proposed=len(metrics),
        skip_reason=skip_reason,
        source="dbt",
    )


# ----- stage 5: joins ------------------------------------------------------


def _stage_joins(ctx: WizardContext) -> StageOutcome:
    """Suggest + apply canonical joins via the deterministic pipeline.

    Unlike stage 3 (entities) and stage 4 (metrics), the join
    suggester is NOT LLM-driven — it mines FK constraints + query-log
    evidence and returns a deterministic ranked candidate list. So
    this stage has no API-key check, no cost cap, no parse-error
    branch, no `--joins-max-cost-usd` knob. It's fast and free.

    Decision tree:

      1. `cfg.no_joins` → skipped (explicit opt-out).
      2. `cfg.skip_index` → skipped (no schema to analyse).
      3. Non-Postgres source → skipped (joins need an indexed
         Postgres schema today).
      4. Store already has canonical joins for this `source_id` →
         skipped (idempotent re-run).
      5. Entity store empty for this `source_id` → skipped with hint
         pointing at entity curation first. Same cross-stage
         dependency as stage 4: joins between entities require
         entities to exist. The suggester would silently return an
         empty list otherwise, masking the missing prerequisite.
      6. Otherwise, run `suggest_canonical_joins`, write each
         candidate, emit `done` with the applied count.

    Like stages 3 and 4, stage 5 is `abort_on_fail=False`: a failure
    here records a `failed` outcome but lets the wizard wire the
    host and print the next step.
    """
    cfg = ctx.config

    if cfg.no_joins:
        return StageOutcome(
            stage=5,
            name="joins",
            status="skipped",
            message="--no-joins set; not running canonical-join suggestion",
            next_step="run `schemabrain joins suggest --apply` later to curate joins",
        )

    if cfg.skip_index:
        return StageOutcome(
            stage=5,
            name="joins",
            status="skipped",
            message="skipped because --skip-index is set (no indexed schema to analyse)",
        )

    if not is_postgres_url(cfg.source_url):
        return StageOutcome(
            stage=5,
            name="joins",
            status="skipped",
            message="canonical-join suggestion needs a Postgres source",
        )

    # Already-curated check runs before the empty-entity check. An
    # idempotent re-run on a store that already has canonical joins
    # short-circuits — the suggester is deterministic, so re-running
    # against an unchanged FK + query-log set yields the same
    # candidates that already landed.
    source_id = _source_id_for(cfg.source_url)
    if cfg.store_path.exists():
        existing = _peek_join_count(cfg.store_path, source_id)
        if isinstance(existing, StageOutcome):
            return existing
        if existing > 0:
            return StageOutcome(
                stage=5,
                name="joins",
                status="skipped",
                message=f"already curated: {existing} canonical join/s present for this source",
                next_step="run `schemabrain joins suggest --apply` directly "
                "to re-curate from fresh evidence",
            )

    # Cross-stage dependency check (mirror of `_stage_metrics`): the
    # join suggester needs entities to map FK pairs through. Without
    # any entities, the suggester returns an empty list silently — we
    # surface a guided pointer instead.
    if cfg.store_path.exists():
        entity_count = _peek_entity_count(cfg.store_path, source_id)
        if isinstance(entity_count, StageOutcome):
            return StageOutcome(
                stage=5,
                name="joins",
                status=entity_count.status,
                message=entity_count.message,
                next_step=entity_count.next_step,
            )
        if entity_count == 0:
            return StageOutcome(
                stage=5,
                name="joins",
                status="skipped",
                message="entity store is empty; joins need entities to anchor on",
                next_step="run `schemabrain entities suggest --apply` first, "
                "then re-run `schemabrain init` (or `schemabrain joins suggest --apply`)",
            )
    else:
        # No store file at all → treat as empty entity store. Stage 2
        # was skipped AND no prior session indexed.
        return StageOutcome(
            stage=5,
            name="joins",
            status="skipped",
            message="entity store is empty; joins need entities to anchor on",
            next_step="run `schemabrain entities suggest --apply` first, "
            "then re-run `schemabrain init` (or `schemabrain joins suggest --apply`)",
        )

    try:
        result = _run_join_suggestion(cfg=cfg, source_id=source_id)
    except _EmptySchemaAtWizard:
        return StageOutcome(
            stage=5,
            name="joins",
            status="skipped",
            message="no FK constraints or query-log evidence found",
            next_step="run `schemabrain joins suggest --dry-run` to confirm, "
            "or define joins by hand via `schemabrain joins apply`",
        )

    if result.applied_count == 0:
        return StageOutcome(
            stage=5,
            name="joins",
            status="skipped",
            message="no canonical joins surfaced from FK or query-log evidence",
            next_step="define joins by hand via `schemabrain joins apply`, "
            "or run `schemabrain joins suggest --dry-run` to inspect evidence",
        )

    if result.candidates_proposed > 0 and result.applied_count < result.candidates_proposed:
        # Partial-success: writes started failing mid-loop (typically
        # a TOCTOU entity-delete race producing an IntegrityError).
        suffix = (
            f"; {result.candidates_proposed - result.applied_count} skipped: {result.skip_reason}"
            if result.skip_reason
            else ""
        )
        return StageOutcome(
            stage=5,
            name="joins",
            status="done",
            message=(
                f"{result.applied_count} of {result.candidates_proposed} "
                f"canonical join{'' if result.applied_count == 1 else 's'} created{suffix}"
            ),
            next_step="inspect the skipped candidates with "
            "`schemabrain joins suggest --dry-run` and apply manually if needed",
        )

    return StageOutcome(
        stage=5,
        name="joins",
        status="done",
        message=(
            f"{result.applied_count} canonical "
            f"join{'' if result.applied_count == 1 else 's'} created "
            "from FK + query-log evidence"
        ),
    )


@dataclass(frozen=True)
class _JoinApplyResult:
    """Internal outcome of `_run_join_suggestion`.

    No `cost_usd` / `llm_model` fields — the join suggester is
    deterministic. `skip_reason` carries the exception name + the
    candidate that triggered it when writes started failing mid-loop
    (today only `sqlite3.IntegrityError` from a TOCTOU entity-delete
    race; `DbtOwnedJoinError` will join the catch list when the
    dbt-relationships importer lands per the comment on
    `store.write_canonical_join`).
    """

    applied_count: int
    candidates_proposed: int = 0
    skip_reason: str | None = None


def _peek_join_count(store_path: Path, source_id: str) -> int | StageOutcome:
    """Open the store read-only and count canonical joins for `source_id`.

    Mirror of `_peek_entity_count` / `_peek_metric_count`. The
    returned `StageOutcome` on failure carries `stage=5, name="joins"`
    so the renderer routes the line to the joins stage label.
    """
    from schemabrain.core.store import SchemaVersionMismatchError, SQLiteStore

    try:
        with SQLiteStore(path=store_path) as store:
            return len(store.list_canonical_joins(source_connection_id=source_id))
    except SchemaVersionMismatchError as exc:
        return StageOutcome(
            stage=5,
            name="joins",
            status="failed",
            message=str(exc),
            next_step=f"delete {store_path} and re-run, "
            "or install the matching schemabrain version",
        )
    except OSError as exc:
        return StageOutcome(
            stage=5,
            name="joins",
            status="failed",
            message=f"store unreadable: {exc}",
            next_step=f"check filesystem permissions on {store_path.parent}",
        )


def _run_join_suggestion(*, cfg: WizardConfig, source_id: str) -> _JoinApplyResult:
    """Mine FK + query-log evidence, write each canonical join.

    No LLM, no cost guard. `suggest_canonical_joins` is a pure
    function over the store's FK table + query-log aggregates;
    returns an already-ranked candidate list. We walk it and call
    `store.write_canonical_join` per candidate, catching
    `sqlite3.IntegrityError` for the rare TOCTOU race where an
    entity was deleted between the suggester's read and the writer's
    insert.
    """
    from sqlite3 import IntegrityError

    from schemabrain.core.store import SQLiteStore
    from schemabrain.joins.suggest import suggest_canonical_joins

    with SQLiteStore(cfg.store_path) as store:
        candidates = suggest_canonical_joins(store=store, source_connection_id=source_id)
        applied = 0
        skip_reason: str | None = None
        for candidate in candidates:
            try:
                store.write_canonical_join(
                    candidate.to_canonical_join(),
                    source_connection_id=source_id,
                )
            except IntegrityError as exc:
                skip_reason = f"{type(exc).__name__} at '{candidate.name}'"
                break
            applied += 1

    return _JoinApplyResult(
        applied_count=applied,
        candidates_proposed=len(candidates),
        skip_reason=skip_reason,
    )


# ----- stage 6: wire_host --------------------------------------------------


def _stage_wire_host(ctx: WizardContext) -> StageOutcome:
    """Write the MCP host config (or print the snippet in manual mode).

    Delegates to the existing `init_flow.init`, which handles runner
    resolution, store validation, host target resolution, snippet
    assembly, and install. The wizard always passes `skip_index=True`
    because stage 2 has already managed the indexed-store
    precondition (either by running index, or by recording an
    explicit skip).

    F3 (post-PR-#79 polish): when the claude-desktop config already
    has a different schemabrain entry, this handler now prompts
    INLINE during stage 6 rather than letting `init()` raise
    `InitRefusal` so the orchestrator can prompt afterwards (which
    rendered the prompt as an orphaned line between the hero panel
    and the 7-stage table). The pre-check + inline prompt + retry
    is now scoped to one stage handler, with one render path.
    Store-path-only diffs auto-accept (operator moved the store —
    no need to prompt for the common case).
    """
    cfg = ctx.config
    effective_assume_yes = cfg.assume_yes
    # Capture the pre-write comparison in a local so
    # `_wire_host_message` can render the `(replaced /old → /new)`
    # trailer. An earlier shape re-peeked AFTER `init()` had already
    # written the new entry — the second peek always saw
    # `state="unchanged"`, making the replacement message dead code
    # in production.
    overwrite_comparison: ClaudeDesktopEntryComparison | None = None

    if cfg.host == "claude-desktop" and not cfg.assume_yes:
        try:
            comparison = peek_claude_desktop_overwrite(
                source_url=cfg.source_url,
                store_path=cfg.store_path,
                env_var_name=cfg.env_var_name,
                pii_block=cfg.pii_block,
            )
        except InitRefusal as refusal:
            return _failed_from_refusal(stage=6, name="wire_host", error=refusal.error)
        if comparison is not None:
            overwrite_comparison = comparison
            if comparison.state == "differs_store_path_only":
                # Common case: operator moved their store. Auto-accept
                # so they don't see a prompt for what is, in practice,
                # a no-op-ish field change. The renderer surfaces the
                # `wrote (replaced /old → /new)` line off
                # `comparison.existing_store_path` for transparency.
                effective_assume_yes = True
            elif comparison.state == "differs":  # noqa: SIM102 — keep state vs tty branches readable
                # Real diff (env-var, db_url, version pin, runner) —
                # surface the inline prompt only if stderr/stdin are
                # both TTYs. Non-interactive runs fall through to
                # `init()` which will raise InitRefusal, translated
                # to a failed StageOutcome as before (no behavior
                # change for CI / piped stderr).
                # Access through the module so tests can monkeypatch
                # `_ui.stderr_is_interactive_tty` once and reach this
                # call site without a separate `wizard.…` patch.
                if _ui.stderr_is_interactive_tty():
                    # Access `make_console` through the module to
                    # match the `_ui.stderr_is_interactive_tty()`
                    # call above — keeps both call sites
                    # monkeypatchable via one symbol (`_ui.<name>`)
                    # and avoids a latent binding asymmetry.
                    console = _ui.make_console(stderr=True)
                    _render_overwrite_diff_summary(console, comparison)
                    accepted = prompt_yes_no(
                        console,
                        "Overwrite the existing schemabrain entry?",
                        default=False,
                    )
                    if not accepted:
                        # Signal user-cancellation via the structured
                        # `user_cancelled=True` field instead of a
                        # message-prefix check in `_cmd_init`. With
                        # the prefix-coupling shape, a copy edit to
                        # this string would silently break
                        # the exit-0 contract; the typed field
                        # eliminates the cross-module string coupling.
                        return StageOutcome(
                            stage=6,
                            name="wire_host",
                            status="failed",
                            message="existing schemabrain entry not overwritten",
                            next_step="re-run with --yes to overwrite, or hand-edit the config",
                            user_cancelled=True,
                        )
                    effective_assume_yes = True

    try:
        result = init(
            source_url=cfg.source_url,
            store_path=cfg.store_path,
            host=cfg.host,
            env_var_name=cfg.env_var_name,
            skip_index=True,
            assume_yes=effective_assume_yes,
            pii_block=cfg.pii_block,
        )
    except InitRefusal as refusal:
        return _failed_from_refusal(stage=6, name="wire_host", error=refusal.error)
    ctx.host_install_result = result
    return StageOutcome(
        stage=6,
        name="wire_host",
        status="done",
        message=_wire_host_message(result, comparison=overwrite_comparison),
    )


def _render_overwrite_diff_summary(
    console: Console, comparison: ClaudeDesktopEntryComparison
) -> None:
    """Print the overwrite diff preview before the overwrite prompt.

    Two-block format:

      1. **Header lines** — yellow "A different schemabrain entry…"
         + bright_black "differing fields: …". Lets the operator
         eyeball whether the prompt is about the right entry without
         reading the full diff.
      2. **Unified diff** (D3) — each line of
         ``comparison.diff_preview`` rendered with a color matched to
         its leader byte: bright_black for ``---/+++/@@`` markers,
         red for ``-`` removals, green for ``+`` additions, default
         for context. Lets the operator see exactly which JSON bytes
         are about to change before approving the overwrite.

    The ``.bak`` sibling that captures the pre-write contents is
    written by ``write_mcp_config_atomic`` (PR-1 contract). D3
    surfaces what's about to change; the rollback target was already
    guaranteed by the existing atomic-write path.
    """
    fields = ", ".join(comparison.differing_field_names) or "schemabrain entry"
    console.print(
        f"  [yellow]A different schemabrain entry already exists in {comparison.config_path}.[/]"
    )
    console.print(f"  [bright_black]differing fields: {fields}[/]")
    if not comparison.diff_preview:
        # Defensive: comparison.diff_preview is always non-empty for
        # the differs/differs_store_path_only verdicts the caller
        # gates on, but this skip keeps the renderer safe if some
        # future caller passes a "new"/"unchanged" comparison.
        return
    console.print("")
    for raw_line in comparison.diff_preview.splitlines():
        # Rich treats square brackets as markup — JSON contains them
        # in ``args`` arrays. Use ``markup=False`` so the literal
        # bracket pair renders intact instead of triggering a parse
        # error / silent eat.
        if raw_line.startswith(("---", "+++", "@@")):
            style = "bright_black"
        elif raw_line.startswith("-"):
            style = "red"
        elif raw_line.startswith("+"):
            style = "green"
        else:
            style = ""
        console.print(raw_line, style=style, markup=False, highlight=False)


def _wire_host_message(
    result: InitResult,
    *,
    comparison: ClaudeDesktopEntryComparison | None = None,
) -> str:
    """Pick a short summary line for the stage-6 outcome.

    The detailed rendering (path, backup, redacted shell-out argv)
    lives in the CLI renderer, which reads from
    `WizardResult.host_install_result`. The wizard outcome just
    captures the one-liner version for the stage list.

    F3: when ``comparison`` reports a store-path-only diff that the
    wizard auto-accepted, the "written" message gets a
    ``(replaced /old → /new)`` trailer so the operator sees what
    actually changed. Other replacement paths render the plain
    "wrote" line — the diff is more meaningful in the
    store-path-only case because the operator usually intended it.
    """
    if result.state == "written":
        if (
            comparison is not None
            and comparison.state == "differs_store_path_only"
            and comparison.existing_store_path is not None
            and comparison.new_store_path is not None
            and comparison.existing_store_path != comparison.new_store_path
        ):
            return (
                f"wrote schemabrain entry to {result.config_path} "
                f"(replaced {comparison.existing_store_path} → "
                f"{comparison.new_store_path})"
            )
        return f"wrote schemabrain entry to {result.config_path}"
    if result.state == "unchanged":
        return f"schemabrain entry already configured in {result.config_path}; no changes"
    if result.state == "shell_out_succeeded":
        return "registered schemabrain with Claude Code"
    if result.state == "shell_out_failed":
        return "Claude Code registration failed; the snippet is printable below"
    # state == "printed_only" — manual / --print-only
    return "manual mode: snippet ready to paste into your host's config"


# ----- stage 7: next_step --------------------------------------------------


def _stage_next_step(ctx: WizardContext) -> StageOutcome:
    """Closing stage — never fails; emits a brief status line.

    The renderer's closing block carries the actionable next-step
    copy (restart prompt, example query, tail/audit hints, thesis
    tagline), so this handler only signals that the wizard reached
    the end successfully.
    """
    return StageOutcome(
        stage=7,
        name="next_step",
        status="done",
        message="Ready",
    )


# ----- canonical stage list ------------------------------------------------


DEFAULT_STAGES: tuple[WizardStage, ...] = (
    WizardStage(
        stage=1,
        name="source_check",
        handler=_stage_source_check,
        abort_on_fail=True,
    ),
    WizardStage(
        stage=2,
        name="index",
        handler=_stage_index,
        abort_on_fail=True,
    ),
    WizardStage(
        stage=3,
        name="entities",
        handler=_stage_entities,
        abort_on_fail=False,
    ),
    WizardStage(
        stage=4,
        name="metrics",
        handler=_stage_metrics,
        abort_on_fail=False,
    ),
    WizardStage(
        stage=5,
        name="joins",
        handler=_stage_joins,
        abort_on_fail=False,
    ),
    WizardStage(
        stage=6,
        name="wire_host",
        handler=_stage_wire_host,
        abort_on_fail=True,
    ),
    WizardStage(
        stage=7,
        name="next_step",
        handler=_stage_next_step,
        abort_on_fail=True,
    ),
)


__all__ = [
    "DEFAULT_STAGES",
    "StageHandler",
    "StageOutcome",
    "StageStatus",
    "WizardConfig",
    "WizardContext",
    "WizardResult",
    "WizardStage",
    "run_default_wizard",
    "run_wizard",
]
