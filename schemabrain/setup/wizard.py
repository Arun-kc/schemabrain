"""Five-stage activation wizard behind `schemabrain init`.

The wizard turns a single `schemabrain init` invocation into the
end-to-end activation surface a new user expects: validate the source,
index the schema, suggest entities, wire the MCP host, and print the
next step.

Stages in order:

  1. source_check  — URL reachable + (Postgres) read-only session
  2. index         — cache-aware DDL introspection into the local store
  3. entities      — Anthropic-backed entity suggestion (cost-capped)
  4. wire_host     — write the MCP host config (or print the snippet)
  5. next_step     — render the "ask Claude X" hint

Each stage produces a `StageOutcome` (`done` / `skipped` / `failed`).
Stages 1, 2, and 4 abort the wizard on `failed`; stage 3 emits a
warning outcome and lets stages 4 and 5 finish, because partial
success (no entities curated yet) is still useful to the user.

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

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, get_args

from sqlalchemy.exc import OperationalError

from schemabrain.enrichment.pipeline import CostCapExceeded
from schemabrain.errors import GuidedError
from schemabrain.setup.hosts import HostName, is_postgres_url
from schemabrain.setup.init_flow import (
    InitRefusal,
    InitResult,
    _validate_source_reachable,
    _validate_source_read_only,
    init,
)

if TYPE_CHECKING:
    from schemabrain.indexer import IndexResult

# ----- public types ---------------------------------------------------------

StageStatus = Literal["done", "skipped", "failed"]

_VALID_STATUSES: frozenset[str] = frozenset(get_args(StageStatus))

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
    point the user at the recovery action.
    """

    stage: int
    name: str
    status: StageStatus
    message: str
    next_step: str | None = None

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


@dataclass(frozen=True)
class WizardConfig:
    """Resolved configuration passed to every stage handler.

    Carries everything a stage might need to do its work — source URL,
    store path, host target, plus the flag-driven knobs for stages 2
    and 3. Frozen so handlers can't mutate the config under each
    other.
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
    """

    config: WizardConfig
    outcomes: list[StageOutcome] = field(default_factory=list)
    host_install_result: InitResult | None = None


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


def run_wizard(
    config: WizardConfig,
    *,
    stages: Sequence[WizardStage] | None = None,
) -> WizardResult:
    """Run the wizard pipeline against `config`.

    `stages` is dependency-injected so tests can drive the state
    machine without standing up a real database. Production callers
    use `run_default_wizard`, which binds `DEFAULT_STAGES`.

    Stage handlers MUST NOT raise — the contract is that they
    translate every exception into a `StageOutcome(status="failed",
    ...)`. An exception that escapes a handler signals a wizard bug,
    not user error, and is allowed to propagate so the CLI's top-level
    handler can render a crash trace rather than a misleading
    `failed` line.
    """
    actual_stages = stages if stages is not None else DEFAULT_STAGES
    ctx = WizardContext(config=config)
    for stage in actual_stages:
        outcome = stage.handler(ctx)
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


def run_default_wizard(config: WizardConfig) -> WizardResult:
    """Production entry point: run the wizard with the canonical stages."""
    return run_wizard(config)


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
    standalone `init` would.
    """
    return StageOutcome(
        stage=stage,
        name=name,
        status="failed",
        message=error.message,
        next_step=error.next_step,
    )


# ----- stage 1: source_check -----------------------------------------------


def _stage_source_check(ctx: WizardContext) -> StageOutcome:
    """Validate the source URL is reachable + read-only (Postgres only).

    Wraps the same validators `init_flow.init` runs as its first two
    preconditions. SQLite sources skip the read-only check (no
    session-level read-only setting exists in SQLite).
    """
    cfg = ctx.config
    try:
        _validate_source_reachable(cfg.source_url)
        if is_postgres_url(cfg.source_url):
            _validate_source_read_only(cfg.source_url)
    except InitRefusal as refusal:
        return _failed_from_refusal(stage=1, name="source_check", error=refusal.error)
    return StageOutcome(
        stage=1,
        name="source_check",
        status="done",
        message="source reachable + read-only",
    )


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
        return StageOutcome(
            stage=2,
            name="index",
            status="failed",
            message="indexing only supports Postgres sources today",
            next_step="re-run with a Postgres URL, "
            "or pass --skip-index to wire the host without indexing",
        )

    source_id = _source_id_for(cfg.source_url)

    if cfg.store_path.exists():
        existing = _peek_store_table_count(cfg.store_path, source_id)
        if isinstance(existing, StageOutcome):
            return existing  # schema-version mismatch surfaced as failed
        if existing > 0:
            return StageOutcome(
                stage=2,
                name="index",
                status="skipped",
                message=f"already indexed: {existing} table(s) present for this source",
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

    Returns the count on success, or a `StageOutcome(failed)` carrying
    a guided error message when the store's schema version doesn't
    match this installation's expectation. The caller treats the
    failure as terminal for stage 2.
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
            pipeline = EnrichmentPipeline(
                client=anthropic_haiku_45_client(api_key=api_key),
                cryptic_client=None,
                max_cost_usd=_WIZARD_INDEX_ENRICH_CAP_USD,
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
    """Pick a one-line summary for a successful stage-2 outcome."""
    cols = result.columns_added + result.columns_changed
    base = f"{result.tables_seen} tables, {cols} columns indexed"
    if enrich and result.descriptions_generated > 0:
        base += f" (enriched {result.descriptions_generated} columns, ${result.llm_cost_usd:.4f})"
    return base


# Cost + concurrency knobs used by `_run_indexer`. Kept here, not at
# the WizardConfig level, because they're internal to the wizard's
# bundled enrichment policy (a power-user run via `schemabrain index`
# directly carries its own knobs).
_WIZARD_INDEX_ENRICH_CAP_USD: float = 10.0
_WIZARD_INDEX_CONCURRENCY: int = 4
_WIZARD_INDEX_CRYPTIC_CONCURRENCY: int = 2


# ----- stage 3: entities ---------------------------------------------------


def _stage_entities(ctx: WizardContext) -> StageOutcome:
    """Suggest + apply entities via the Anthropic-backed pipeline.

    Stub at this commit — returns a `skipped` outcome. Real pipeline
    wiring lands in a subsequent commit.
    """
    return StageOutcome(
        stage=3,
        name="entities",
        status="skipped",
        message="entity suggestion handler not yet wired",
        next_step="run `schemabrain entities suggest --apply` separately for now",
    )


# ----- stage 4: wire_host --------------------------------------------------


def _stage_wire_host(ctx: WizardContext) -> StageOutcome:
    """Write the MCP host config (or print the snippet in manual mode).

    Delegates to the existing `init_flow.init`, which handles runner
    resolution, store validation, host target resolution, snippet
    assembly, and install. The wizard always passes `skip_index=True`
    because stage 2 has already managed the indexed-store
    precondition (either by running index, or by recording an
    explicit skip).
    """
    cfg = ctx.config
    try:
        result = init(
            source_url=cfg.source_url,
            store_path=cfg.store_path,
            host=cfg.host,
            env_var_name=cfg.env_var_name,
            skip_index=True,
            assume_yes=cfg.assume_yes,
        )
    except InitRefusal as refusal:
        return _failed_from_refusal(stage=4, name="wire_host", error=refusal.error)
    ctx.host_install_result = result
    return StageOutcome(
        stage=4,
        name="wire_host",
        status="done",
        message=_wire_host_message(result),
    )


def _wire_host_message(result: InitResult) -> str:
    """Pick a short summary line for the stage-4 outcome.

    The detailed rendering (path, backup, redacted shell-out argv)
    lives in the CLI renderer, which reads from
    `WizardResult.host_install_result`. The wizard outcome just
    captures the one-liner version for the stage list.
    """
    if result.state == "written":
        return f"wrote schemabrain entry to {result.config_path}"
    if result.state == "unchanged":
        return f"schemabrain entry already configured in {result.config_path}; no changes"
    if result.state == "shell_out_succeeded":
        return "registered schemabrain with Claude Code"
    if result.state == "shell_out_failed":
        return "Claude Code registration failed; the snippet is printable below"
    # state == "printed_only" — manual / --print-only
    return "manual mode: snippet ready to paste into your host's config"


# ----- stage 5: next_step --------------------------------------------------


def _stage_next_step(ctx: WizardContext) -> StageOutcome:
    """Closing stage — never fails; always renders the next-step hint."""
    return StageOutcome(
        stage=5,
        name="next_step",
        status="done",
        message='restart your MCP host, then ask: "list the entities Schema Brain knows about"',
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
        name="wire_host",
        handler=_stage_wire_host,
        abort_on_fail=True,
    ),
    WizardStage(
        stage=5,
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
