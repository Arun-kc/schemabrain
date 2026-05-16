"""Schema Brain CLI.

Entry point: `schemabrain <subcommand>`.

`index` connects to a Postgres URL, introspects every user-visible
schema and table, profiles columns whose schema changed since the last
run, generates LLM descriptions for changed columns (unless
`--no-enrich`), and writes structural metadata, fingerprints, and
descriptions to a local SQLite store.

URL sourcing: every subcommand that needs a connection URL
(`index`, `serve`, `eval`) accepts `--url-env VARNAME` to read the
URL from a named environment variable. This keeps credentials out of
argv (visible to `ps`, shell history, and journald). The legacy
positional / `--source <url>` form still works for backwards
compatibility but emits a deprecation warning when the URL contains
a password.

Re-running `index` against an unchanged source is a no-op: the
fingerprint cache lets us skip introspection writes, profiler queries,
AND LLM calls.

Cost discipline: `--max-cost N` (default $1) hard-caps LLM spend per
run. Spend is also persisted across runs in the SQLite store's
cost ledger — a fresh `index` run reads the prior cumulative total
and refuses to issue calls once the cap is reached, so the cap is
not just per-process. Use `--no-cost-cap` to disable the cap
entirely (intended for users who've previewed cost via `--dry-run`
and accept the projected spend). ANTHROPIC_API_KEY must be set in
the environment unless `--no-enrich` is passed.

`eval` scores a `Retriever` against a hand-curated `GoldenSet` and
prints recall@1/@3/@10. Two retrievers are available via `--retriever`:
`embedding` (default, cosine over stored column embeddings) and
`keyword` (the keyword-overlap baseline). The harness is
schema-agnostic: pass `--golden /path/to/your-schema.json` for a real
schema. The bundled default is just one starter example
(`schemabrain/eval/golden_sets/ecommerce.json`, paired with the
synthetic fixture in `schemabrain/eval/fixtures/ecommerce.sql`) so the
CLI works out of the box.

`serve` runs the MCP server on stdio against a previously-indexed
store. Seven tools are exposed: the five physical-schema tools
(`find_relevant_tables`, `describe_table`, `describe_column`,
`suggest_joins`, `get_example_queries`) plus the v1 semantic-layer
tools (`list_entities`, `describe_entity`). Wire into Claude Desktop
or any MCP client by adding an entry to `claude_desktop_config.json`
that runs `schemabrain serve --url-env DATABASE_URL --store-path
<PATH>` with `DATABASE_URL` set in the config's `env` block.

`entities apply <yaml-path>` loads one entity YAML definition into
the store — the deterministic file-to-store loader. `entities suggest`
runs the LLM-suggest pipeline against an indexed schema with three
output modes (`--dry-run`, `--out-dir DIR`, `--apply`), bounded by
`--max-cost-usd` (or the `SCHEMABRAIN_MAX_LLM_COST_USD` env var). Both
commands share the same `Entity` write path; suggested entities
land with `origin="suggested"` and a dbt-owned-entity write guard
refuses cross-origin overwrites.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import yaml
from sqlalchemy.exc import OperationalError

from schemabrain import __version__
from schemabrain.connectors._url import safe_engine_url
from schemabrain.connectors.postgres import PostgresDataSource
from schemabrain.core.models import Table
from schemabrain.core.store import DbtOwnedEntityError, SQLiteStore
from schemabrain.enrichment.anthropic_client import (
    anthropic_haiku_45_client,
    anthropic_sonnet_46_client,
)
from schemabrain.enrichment.embeddings import Embedder, fastembed_default
from schemabrain.enrichment.llm import FakeLLMClient, LLMClient
from schemabrain.enrichment.pipeline import CostCapExceeded, EnrichmentPipeline
from schemabrain.entities.suggest import (
    CostCeilingExceededError,
    CostCeilingGuard,
    EntityCandidate,
    EntitySuggestionPipeline,
    SuggestionParseError,
    SuggestionResult,
)
from schemabrain.entities.yaml_grammar import (
    EntityParseError,
    parse_entity_yaml_file,
)
from schemabrain.errors import (
    GuidedError,
    anthropic_auth_failed,
    postgres_operational_error,
    render_error,
    store_path_unwritable,
    url_wrong_driver,
)
from schemabrain.eval.bundled import resolve_bundled_path
from schemabrain.eval.golden import DEFAULT_GOLDEN_PATH, load_golden
from schemabrain.eval.retriever import EmbeddingRetriever, KeywordRetriever, Retriever
from schemabrain.eval.runner import format_report, run_eval
from schemabrain.indexer import IndexReporter, NullReporter, dry_run_index, index
from schemabrain.logging_config import configure_logging
from schemabrain.mcp.server import run_stdio
from schemabrain.mining.pipeline import mine_queries
from schemabrain.profiler.postgres import PostgresProfiler

_DEFAULT_STORE_PATH = "./schemabrain.db"
# Default cap deliberately low — a first-time user's `schemabrain index`
# should not be able to surprise-spend more than $1 against the LLM
# vendor before they understand what's running. Override with
# `--max-cost N` for higher limits, or `--no-cost-cap` to disable
# entirely (intended for large schemas where the operator has already
# previewed cost via `--dry-run`).
_DEFAULT_MAX_COST_USD = 1.0
# Default cost ceiling for `entities suggest`. Generous enough for
# ~50-table schemas with Sonnet, conservative enough that a first-time
# user can't accidentally rack up >$1 of spend. Override per-run via
# `--max-cost-usd N` or the `SCHEMABRAIN_MAX_LLM_COST_USD` env var.
_DEFAULT_SUGGEST_MAX_COST_USD = 1.0
# Default candidate cap for `entities suggest`. The pipeline both
# communicates this to the LLM (via the user prompt) and enforces it
# post-parse, so a misbehaving LLM that over-produces still gets
# capped before any output is written.
_DEFAULT_SUGGEST_TOP_K = 10
# Env var read by `entities suggest` when --max-cost-usd is omitted.
# Mirrors how --url-env keeps sensitive values out of argv (cost
# ceiling isn't secret, but env-var precedence is a familiar pattern
# for users wiring schemabrain into a shared toolchain).
_SUGGEST_COST_ENV_VAR = "SCHEMABRAIN_MAX_LLM_COST_USD"
# Env var that holds the canned LLM response when `--provider stub` is
# used. Same rationale as `--url-env`: keep multi-line YAML out of argv.
_SUGGEST_STUB_RESPONSE_ENV_VAR = "SCHEMABRAIN_STUB_RESPONSE"
# Sentinel returned by `_resolve_max_cost` when `--no-cost-cap` is
# passed. Large enough to never trip the pipeline's pre-call cap check
# under any realistic Anthropic spend, far below `math.inf` so it
# round-trips through any JSON / log serialiser cleanly.
_NO_COST_CAP_SENTINEL = 1e12
_DEFAULT_EVAL_LIMIT = 10

# Per-tier concurrency for the async enrichment pipeline.
# Module-level constants rather than locals so test
# fixtures can monkeypatch them to `1` for deterministic cap
# enforcement — under default concurrency, the per-task cap check
# races and a cap-trip test would need >= 9 columns to land
# deterministically. A future `--concurrency` CLI flag can plumb
# user-facing tuning through these constants without further
# CLI-wiring churn.
_PIPELINE_DEFAULT_CONCURRENCY = 8
_PIPELINE_CRYPTIC_CONCURRENCY = 4

# 16 hex chars = 64 bits of SHA-256. For a single user's plausible set of
# databases (<1000), birthday-collision probability is ~10^-14. If we ever
# share these IDs across users (multi-tenant), bump this.
_SOURCE_ID_LENGTH = 16

# Postgres URL schemes we accept, with their default port.
_POSTGRES_SCHEMES: dict[str, int] = {
    "postgresql": 5432,
    "postgres": 5432,
    "postgresql+psycopg": 5432,
    "postgresql+psycopg2": 5432,
    "postgresql+asyncpg": 5432,
}


def _resolve_max_cost(args: argparse.Namespace) -> float:
    """Resolve `--max-cost` and `--no-cost-cap` into a single value.

    `--no-cost-cap` takes precedence over `--max-cost` so users can
    flip from a capped run to an uncapped one without removing the
    earlier flag from their command-line history. The returned value
    flows directly to `EnrichmentPipeline(max_cost_usd=...)`.
    """
    if args.no_cost_cap:
        return _NO_COST_CAP_SENTINEL
    return float(args.max_cost)


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    # Configure stderr-only logging before any subcommand runs. Reads
    # `-v`/`-vv` from the parsed args; falls back to the
    # `SCHEMABRAIN_LOG_LEVEL` env var when no flag is passed.
    configure_logging(verbosity=args.verbose)
    if args.command == "index":
        return _cmd_index(
            positional_url=args.url,
            url_env=args.url_env,
            store_path=args.store_path,
            no_enrich=args.no_enrich,
            max_cost_usd=_resolve_max_cost(args),
            enable_sonnet=args.enable_sonnet,
            no_embed=args.no_embed,
            quiet=args.quiet,
            dry_run=args.dry_run,
        )
    if args.command == "eval":
        return _cmd_eval(
            golden_path=args.golden,
            store_path=args.store_path,
            positional_url=args.source,
            url_env=args.url_env,
            limit=args.limit,
            retriever_kind=args.retriever,
        )
    if args.command == "serve":
        return _cmd_serve(
            positional_url=args.source,
            url_env=args.url_env,
            store_path=args.store_path,
        )
    if args.command == "fixture-path":
        return _cmd_fixture_path(args.name)
    if args.command == "mine-queries":
        return _cmd_mine_queries(
            positional_url=args.source,
            url_env=args.url_env,
            store_path=args.store_path,
        )
    if args.command == "entities":
        if args.entity_action == "apply":
            return _cmd_entities_apply(
                yaml_path=args.yaml_path,
                positional_url=args.source,
                url_env=args.url_env,
                store_path=args.store_path,
            )
        if args.entity_action == "suggest":
            return _cmd_entities_suggest(
                positional_url=args.source,
                url_env=args.url_env,
                store_path=args.store_path,
                dry_run=args.dry_run,
                out_dir=args.out_dir,
                apply=args.apply,
                top_k=args.top_k,
                provider=args.provider,
                max_cost_usd=args.max_cost_usd,
            )
        # argparse `required=True` on the entity-action subparser
        # prevents reaching here; the branch is structurally
        # unreachable but symmetric with the outer command dispatch.
        parser.error(f"unknown entities action: {args.entity_action}")  # pragma: no cover
    # argparse `required=True` on subparsers prevents reaching here, but
    # leaving an explicit branch is cheaper than a guarded assertion.
    parser.error(f"unknown command: {args.command}")  # pragma: no cover


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="schemabrain",
        description="MCP-ready semantic understanding of any production database.",
    )
    parser.add_argument(
        "--version",
        "-V",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="count",
        default=0,
        help="Increase logging verbosity (stderr only). -v shows INFO, "
        "-vv shows DEBUG. Default is WARNING. For `serve` under Claude "
        "Desktop where CLI flags aren't available, set the "
        "SCHEMABRAIN_LOG_LEVEL environment variable instead.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_index = sub.add_parser("index", help="Index a database into the local SQLite store")
    p_index.add_argument(
        "url",
        nargs="?",
        default=None,
        help="DEPRECATED: source database URL passed as a positional argument. "
        "Embeds credentials in argv (visible to `ps`, shell history, journald) — "
        "use --url-env instead. The positional form still works for backwards "
        "compatibility but will emit a warning when the URL contains a password.",
    )
    p_index.add_argument(
        "--url-env",
        dest="url_env",
        metavar="VARNAME",
        default=None,
        help="Name of the environment variable that holds the source database URL "
        "(e.g. --url-env DATABASE_URL). Preferred over the positional form because "
        "the URL — and any embedded password — never appears in argv.",
    )
    p_index.add_argument(
        "--store-path",
        default=_DEFAULT_STORE_PATH,
        help=f"Path to the local SQLite store (default: {_DEFAULT_STORE_PATH})",
    )
    p_index.add_argument(
        "--no-enrich",
        action="store_true",
        help="Skip the LLM enrichment step. Useful for cost-free dry runs "
        "and for environments without an ANTHROPIC_API_KEY.",
    )
    p_index.add_argument(
        "--max-cost",
        type=float,
        default=_DEFAULT_MAX_COST_USD,
        help=f"Hard cap on USD spend per run (default: ${_DEFAULT_MAX_COST_USD:.2f}). "
        "Aborts cleanly when reached; no effect with --no-enrich. "
        "Use --no-cost-cap to disable entirely.",
    )
    p_index.add_argument(
        "--no-cost-cap",
        action="store_true",
        help="Disable the cost cap entirely. Use only when you've already "
        "previewed cost via `--dry-run` and accept the projected spend. "
        "Overrides --max-cost when both are passed.",
    )
    p_index.add_argument(
        "--enable-sonnet",
        action="store_true",
        help="Route cryptic column names (heavily abbreviated, e.g. "
        "`acct_dim_v3`) to Claude Sonnet 4.6 instead of Haiku 4.5. "
        "Sonnet is ~5x more expensive per call but produces better "
        "descriptions for hard-to-decode names. Default off (Haiku-only) "
        "to keep automatic runs cheap; enable when indexing schemas with "
        "many cryptic identifiers.",
    )
    p_index.add_argument(
        "--no-embed",
        action="store_true",
        help="Skip generating local sentence embeddings for column "
        "descriptions. Embeddings power semantic retrieval via "
        "`EmbeddingRetriever`; skipping them saves ~10ms per column "
        "at index time but disables "
        "semantic retrieval. Default off (embeddings ON). Implied when "
        "--no-enrich is set, since there are no descriptions to embed.",
    )
    p_index.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Suppress the live progress UI. The final one-line summary "
        "still prints to stderr. Useful for CI logs and when stderr is "
        "piped to a file. The CLI also auto-detects non-TTY stderr and "
        "disables the live UI without this flag.",
    )
    p_index.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what `index` would do without doing it: count "
        "tables and columns, compute the diff against the cached "
        "store, and estimate LLM cost from a measured per-column "
        "average (~$0.0003/col on Haiku 4.5). No DB writes, no LLM "
        "calls, no embeddings, no fastembed init. ANTHROPIC_API_KEY "
        "is NOT required. Note: estimate ignores --enable-sonnet "
        "tier routing and reports Haiku pricing only.",
    )

    p_eval = sub.add_parser(
        "eval",
        help="Score a Retriever against the bundled golden set; print recall@1/@3/@10",
    )
    p_eval.add_argument(
        "--source",
        default=None,
        help="The same source URL passed to `index` — used to resolve which "
        "tables in the local store to score against. DEPRECATED when the URL "
        "contains a password; prefer --url-env. One of --source / --url-env "
        "is required.",
    )
    p_eval.add_argument(
        "--url-env",
        dest="url_env",
        metavar="VARNAME",
        default=None,
        help="Name of the environment variable that holds the source URL "
        "(e.g. --url-env DATABASE_URL). Preferred over --source so credentials "
        "never appear in argv. Mutually exclusive with --source.",
    )
    p_eval.add_argument(
        "--store-path",
        default=_DEFAULT_STORE_PATH,
        help=f"Path to the local SQLite store (default: {_DEFAULT_STORE_PATH})",
    )
    p_eval.add_argument(
        "--golden",
        default=str(DEFAULT_GOLDEN_PATH),
        help="Path to a golden-set JSON file. The default is one starter "
        "example (synthetic e-commerce); for your own schema, author a "
        f"matching JSON and pass it here. (default: {DEFAULT_GOLDEN_PATH})",
    )
    p_eval.add_argument(
        "--retriever",
        choices=("embedding", "keyword"),
        default="embedding",
        help="Which Retriever implementation to score. `embedding` uses "
        "stored column embeddings + cosine (requires the store to have "
        "been indexed without --no-embed). `keyword` uses the Week-3 "
        "keyword-overlap baseline. Default: embedding.",
    )
    p_eval.add_argument(
        "--limit",
        type=int,
        default=_DEFAULT_EVAL_LIMIT,
        help=f"Top-K cap passed to the retriever (default: {_DEFAULT_EVAL_LIMIT})",
    )

    p_serve = sub.add_parser(
        "serve",
        help="Run the MCP server on stdio against the local store",
    )
    p_serve.add_argument(
        "--source",
        default=None,
        help="The same source URL passed to `index` — used to resolve which "
        "tables in the local store the MCP tools operate against. DEPRECATED "
        "when the URL contains a password; prefer --url-env. One of --source / "
        "--url-env is required.",
    )
    p_serve.add_argument(
        "--url-env",
        dest="url_env",
        metavar="VARNAME",
        default=None,
        help="Name of the environment variable that holds the source URL "
        "(e.g. --url-env DATABASE_URL). Preferred over --source so credentials "
        "never appear in argv. Mutually exclusive with --source.",
    )
    p_serve.add_argument(
        "--store-path",
        default=_DEFAULT_STORE_PATH,
        help=f"Path to the local SQLite store (default: {_DEFAULT_STORE_PATH})",
    )

    p_mine = sub.add_parser(
        "mine-queries",
        help="Harvest observed SQL from `pg_stat_statements` into the local store",
    )
    p_mine.add_argument(
        "--source",
        default=None,
        help="The same source URL passed to `index` — used to resolve which "
        "tables in the local store the mined SQL should attach to. "
        "DEPRECATED when the URL contains a password; prefer --url-env. "
        "One of --source / --url-env is required.",
    )
    p_mine.add_argument(
        "--url-env",
        dest="url_env",
        metavar="VARNAME",
        default=None,
        help="Name of the environment variable that holds the source URL "
        "(e.g. --url-env DATABASE_URL). Preferred over --source so credentials "
        "never appear in argv. Mutually exclusive with --source.",
    )
    p_mine.add_argument(
        "--store-path",
        default=_DEFAULT_STORE_PATH,
        help=f"Path to the local SQLite store (default: {_DEFAULT_STORE_PATH})",
    )

    p_fixture = sub.add_parser(
        "fixture-path",
        help="Print the absolute path to a bundled fixture (e.g. ecommerce.sql)",
    )
    p_fixture.add_argument(
        "name",
        help="Bundled fixture basename, e.g. `ecommerce.sql` (SQL seed) or "
        "`ecommerce.json` (golden set). The output is paste-clean for "
        "shell substitution, e.g. `psql ... < $(schemabrain fixture-path "
        "ecommerce.sql)`.",
    )

    # `entities` is a subgroup for semantic-layer management.
    # Two actions today: `apply` (file -> store loader) and `suggest`
    # (LLM-suggest pipeline with three output modes).
    p_entities = sub.add_parser(
        "entities",
        help="Manage semantic entity definitions",
    )
    entity_sub = p_entities.add_subparsers(dest="entity_action", required=True)

    p_apply = entity_sub.add_parser(
        "apply",
        help="Load an entity YAML file into the local store",
    )
    p_apply.add_argument(
        "yaml_path",
        help="Path to an entity YAML file (see docs/setup.md for the "
        "grammar; minimum required fields: version, name, binding, "
        "identity).",
    )
    p_apply.add_argument(
        "--source",
        default=None,
        help="The same source URL passed to `index` — used to resolve "
        "which source's entity surface the YAML attaches to. "
        "DEPRECATED when the URL contains a password; prefer --url-env. "
        "One of --source / --url-env is required.",
    )
    p_apply.add_argument(
        "--url-env",
        dest="url_env",
        metavar="VARNAME",
        default=None,
        help="Name of the environment variable that holds the source URL "
        "(e.g. --url-env DATABASE_URL). Preferred over --source so "
        "credentials never appear in argv.",
    )
    p_apply.add_argument(
        "--store-path",
        default=_DEFAULT_STORE_PATH,
        help=f"Path to the local SQLite store (default: {_DEFAULT_STORE_PATH})",
    )

    p_suggest = entity_sub.add_parser(
        "suggest",
        help="LLM-suggest entities for an indexed schema; preview, write to disk, or apply.",
    )
    p_suggest.add_argument(
        "--source",
        default=None,
        help="The same source URL passed to `index`. DEPRECATED when "
        "the URL contains a password; prefer --url-env.",
    )
    p_suggest.add_argument(
        "--url-env",
        dest="url_env",
        metavar="VARNAME",
        default=None,
        help="Name of the environment variable that holds the source URL. "
        "Preferred over --source so credentials never appear in argv.",
    )
    p_suggest.add_argument(
        "--store-path",
        default=_DEFAULT_STORE_PATH,
        help=f"Path to the local SQLite store (default: {_DEFAULT_STORE_PATH})",
    )
    # The three output modes are mutually exclusive; argparse enforces.
    mode_group = p_suggest.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        "--dry-run",
        action="store_true",
        help="Print candidates (entity body + envelope) to stdout. No "
        "files written, no store writes. Best mode for cost/quality "
        "previews.",
    )
    mode_group.add_argument(
        "--out-dir",
        dest="out_dir",
        default=None,
        help="Directory to write one YAML file per candidate "
        "(<entity_name>.yaml) plus a sidecar `_suggestion_metadata.json` "
        "carrying confidence/rationale/pii_hints. The per-entity YAML "
        "is `entities apply`-ready: edit, then apply per file.",
    )
    mode_group.add_argument(
        "--apply",
        action="store_true",
        help="Write candidates directly to the local store with "
        "origin='suggested'. Skips the review step — use --out-dir if "
        "you want a chance to edit before committing.",
    )
    p_suggest.add_argument(
        "--top-k",
        dest="top_k",
        type=int,
        default=_DEFAULT_SUGGEST_TOP_K,
        help=f"Maximum number of candidates to keep (default: "
        f"{_DEFAULT_SUGGEST_TOP_K}). The cap is both communicated to "
        f"the LLM and enforced post-parse.",
    )
    p_suggest.add_argument(
        "--provider",
        choices=["anthropic", "stub"],
        default="anthropic",
        help="LLM provider. `anthropic` is the production default; "
        "`stub` reads the canned response from "
        "$SCHEMABRAIN_STUB_RESPONSE and is intended for CI smoke "
        "tests, not for real schemas.",
    )
    p_suggest.add_argument(
        "--max-cost-usd",
        dest="max_cost_usd",
        type=float,
        default=None,
        help=f"Hard cap on USD spend per run (default: "
        f"${_DEFAULT_SUGGEST_MAX_COST_USD:.2f}). Aborts cleanly when "
        f"reached. Reads SCHEMABRAIN_MAX_LLM_COST_USD if unset; CLI "
        f"flag wins on conflict.",
    )

    return parser


def _cmd_index(
    *,
    positional_url: str | None,
    url_env: str | None,
    store_path: str,
    no_enrich: bool,
    max_cost_usd: float,
    enable_sonnet: bool,
    no_embed: bool,
    quiet: bool = False,
    dry_run: bool = False,
) -> int:
    url = _resolve_url_source(positional=positional_url, url_env=url_env)
    if url is None:
        return 2
    canonical = _resolve_url(url)
    if canonical is None:
        return 2

    # Dry-run dispatch: short-circuit the pipeline + embedder + profiler
    # construction (nothing of theirs runs) and call `dry_run_index`,
    # which walks the same diff loop without side effects and estimates
    # cost from a measured per-column constant.
    if dry_run:
        return _cmd_index_dry_run(
            url=url,
            canonical=canonical,
            store_path=store_path,
            no_enrich=no_enrich,
            no_embed=no_embed,
            quiet=quiet,
        )

    # API key check happens BEFORE the store opens — failing fast on
    # configuration is friendlier than half-initialising the SQLite
    # file and then aborting.
    api_key: str | None = None
    if not no_enrich:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            _render_guided(
                GuidedError(
                    kind="anthropic_api_key_missing",
                    message="ANTHROPIC_API_KEY is not set",
                    why="enrichment uses Claude (Haiku 4.5) to generate column descriptions; the SDK needs a key",
                    fix="export ANTHROPIC_API_KEY=sk-ant-... and re-run, OR re-run with --no-enrich",
                    next_step="get a key at https://console.anthropic.com/settings/keys",
                )
            )
            return 2

    # Build the embedder only if both enrichment AND embedding are
    # active. With no enrichment, there's no description text to embed,
    # so constructing a 70MB ONNX runtime is pure waste.
    embedder: Embedder | None = None
    if not no_enrich and not no_embed:
        embedder = fastembed_default()

    source_id = _make_source_id(url)
    reporter = _build_index_reporter(quiet=quiet)
    started = time.monotonic()
    # The inner `finally reporter.close()` is load-bearing AND order
    # sensitive: rich's live render thread can paint a stale bar over
    # any error message printed while it's still running. We must
    # stop the reporter BEFORE the `except CostCapExceeded` block
    # writes "error: ..." to stderr — otherwise the bar's last frame
    # lands underneath the error and confuses the user. Same logic
    # for KeyboardInterrupt and any other unhandled exception: close
    # the bar first, then let the exception (or error print) surface.
    # `close()` is idempotent — the happy path's on_finish already
    # tore down the widget inside `index()`, so this second call is
    # a no-op there.
    # Lazy import: anthropic SDK ships a chunky dependency tree; only
    # load when we actually need to translate one of its errors.
    try:
        import anthropic
    except ImportError:  # pragma: no cover — anthropic is a hard dep
        anthropic = None  # type: ignore[assignment]
    # psycopg + sqlalchemy variants of "could not connect": catch the
    # SQLAlchemy wrapper at the outer boundary so it fires for both
    # PostgresDataSource and PostgresProfiler context-manager entry.
    # `PostgresDataSource` / `PostgresProfiler` constructors apply
    # `safe_engine_url` internally — no CLI-side filter call needed.
    try:
        try:
            with (
                PostgresDataSource(url) as source,
                PostgresProfiler(url) as profiler,
                SQLiteStore(store_path) as store,
            ):
                # Pipeline construction is moved inside the `with
                # SQLiteStore` block so the cumulative-cost ledger
                # (`store.get_spend_usd`) is readable at construction
                # time. Without this wiring the cost cap is per-process
                # only — a fresh `index` run would reset spend to $0
                # even if previous runs had already exhausted the cap.
                pipeline: EnrichmentPipeline | None = None
                if not no_enrich:
                    # Real guard rather than `assert`: `python -O` strips
                    # `assert` statements, which would let `None` slip
                    # silently to `anthropic_haiku_45_client(api_key=...)`.
                    # The earlier `if not no_enrich:` block returns 2 when
                    # the env var is missing, so reaching here without a
                    # key is a programmer error worth surfacing loudly.
                    if api_key is None:  # pragma: no cover — guard for `python -O`
                        raise RuntimeError(
                            "internal invariant violated: api_key is None inside the enrich branch"
                        )
                    cryptic_client = (
                        anthropic_sonnet_46_client(api_key=api_key) if enable_sonnet else None
                    )
                    pipeline = EnrichmentPipeline(
                        client=anthropic_haiku_45_client(api_key=api_key),
                        cryptic_client=cryptic_client,
                        max_cost_usd=max_cost_usd,
                        default_concurrency=_PIPELINE_DEFAULT_CONCURRENCY,
                        cryptic_concurrency=_PIPELINE_CRYPTIC_CONCURRENCY,
                        store=store,
                        source_connection_id=source_id,
                    )
                result = index(
                    source=source,
                    profiler=profiler,
                    store=store,
                    source_connection_id=source_id,
                    pipeline=pipeline,
                    embedder=embedder,
                    reporter=reporter,
                )
        finally:
            reporter.close()
    except CostCapExceeded as e:
        _render_guided(
            GuidedError(
                kind="cost_cap_exceeded",
                message=str(e),
                why="the --max-cost ceiling is a deliberate safety stop on LLM spend",
                fix=f"re-run with a higher --max-cost (current: ${max_cost_usd:.4f})",
                next_step="or re-run with --no-enrich to index structure without LLM calls",
            )
        )
        return 3
    except OperationalError as e:
        _render_guided(postgres_operational_error(e, url_hint=canonical))
        return 2
    except OSError as e:
        _render_guided(store_path_unwritable(store_path, e))
        return 2
    except Exception as e:
        if anthropic is not None and isinstance(e, anthropic.AuthenticationError):
            _render_guided(anthropic_auth_failed(e))
            return 2
        raise
    elapsed = time.monotonic() - started
    print(
        f"{result.summary()} | source={canonical} store={store_path} in {elapsed:.1f}s",
        file=sys.stderr,
    )
    if result.tables_seen == 0:
        print(
            "warning: no tables indexed (empty database, or all tables are in "
            "system schemas that were skipped)",
            file=sys.stderr,
        )
    return 0


def _cmd_index_dry_run(
    *,
    url: str,
    canonical: str,
    store_path: str,
    no_enrich: bool,
    no_embed: bool,
    quiet: bool,
) -> int:
    """`schemabrain index --dry-run` — preview without doing.

    Skips API key check, embedder construction, and the real `index()`
    side-effecting loop. Calls `dry_run_index()` which walks the diff
    in read-only fashion, emits the same reporter events the live
    progress UI consumes, and returns an `IndexResult` with an
    estimated `llm_cost_usd` from a measured per-column constant.

    The store IS opened (read-only by discipline — `dry_run_index`
    never calls a writing method). Postgres is reached for schema
    introspection (`list_tables` + `get_table`), so connection errors
    still surface through the guided-error translators. That's the
    right behavior: a dry-run that can't reach the source isn't a
    successful dry-run.
    """
    source_id = _make_source_id(url)
    reporter = _build_index_reporter(quiet=quiet)
    will_enrich = not no_enrich
    will_embed = will_enrich and not no_embed
    started = time.monotonic()
    try:
        try:
            with (
                PostgresDataSource(url) as source,
                SQLiteStore(store_path) as store,
            ):
                result = dry_run_index(
                    source=source,
                    store=store,
                    source_connection_id=source_id,
                    will_enrich=will_enrich,
                    will_embed=will_embed,
                    reporter=reporter,
                )
        finally:
            reporter.close()
    except OperationalError as e:
        _render_guided(postgres_operational_error(e, url_hint=canonical))
        return 2
    except OSError as e:
        _render_guided(store_path_unwritable(store_path, e))
        return 2
    elapsed = time.monotonic() - started
    print(
        f"{result.summary(dry_run=True)} | source={canonical} store={store_path} in {elapsed:.1f}s",
        file=sys.stderr,
    )
    if result.tables_seen == 0:
        print(
            "warning: source has no user-visible tables — dry-run produced an empty diff",
            file=sys.stderr,
        )
    return 0


def _cmd_eval(
    *,
    golden_path: str,
    store_path: str,
    positional_url: str | None,
    url_env: str | None,
    limit: int,
    retriever_kind: str,
) -> int:
    source_url = _resolve_url_source(positional=positional_url, url_env=url_env)
    if source_url is None:
        return 2
    if _resolve_url(source_url) is None:
        return 2
    source_id = _make_source_id(source_url)

    try:
        golden = load_golden(golden_path)
    except FileNotFoundError:
        _render_guided(
            GuidedError(
                kind="eval_golden_file_missing",
                message=f"golden file not found: {golden_path}",
                why="--golden must point at a JSON file describing the eval questions + expected tables",
                fix="check the path is correct, or omit --golden to use the bundled ecommerce starter",
                next_step="see schemabrain/eval/golden_sets/ecommerce.json for the expected shape",
            )
        )
        return 2
    except ValueError as e:
        _render_guided(
            GuidedError(
                kind="eval_golden_file_invalid",
                message=f"invalid golden file: {e}",
                why="the golden JSON must match the GoldenSet schema",
                fix="compare your file against schemabrain/eval/golden_sets/ecommerce.json",
                next_step=None,
            )
        )
        return 2

    with SQLiteStore(store_path) as store:
        retriever: Retriever
        if retriever_kind == "embedding":
            # Construct the same default embedder the indexer uses so
            # query and stored vectors are dimension-compatible. fastembed
            # is loaded lazily; the model isn't actually downloaded until
            # the first .embed() call inside the run.
            retriever = EmbeddingRetriever(
                store=store,
                source_connection_id=source_id,
                embedder=fastembed_default(),
            )
        else:
            retriever = KeywordRetriever(store=store, source_connection_id=source_id)
        report = run_eval(golden=golden, retriever=retriever, limit=limit)

    print(format_report(report))
    return 0


def _cmd_serve(
    *,
    positional_url: str | None,
    url_env: str | None,
    store_path: str,
) -> int:
    """Run the MCP server on stdio against the local store.

    Blocks until the client disconnects. The store stays open for the
    lifetime of the process; SQLiteStore is single-process safe and
    handles concurrent reads from FastMCP's async tool dispatch. Tools
    are read-only (no writes occur at MCP call time), so SQLite's
    single-writer limit is never approached.
    """
    source_url = _resolve_url_source(positional=positional_url, url_env=url_env)
    if source_url is None:
        return 2
    if _resolve_url(source_url) is None:
        return 2
    source_id = _make_source_id(source_url)

    # Construct the same default embedder the indexer used so query and
    # stored vectors are dimension-compatible. fastembed loads the ONNX
    # model lazily on first call.
    try:
        with SQLiteStore(store_path) as store:
            run_stdio(
                store=store,
                source_connection_id=source_id,
                embedder=fastembed_default(),
            )
    except OSError as e:
        # Unwritable directory, missing parent, etc. Surface as a
        # guided block instead of a traceback — Claude Desktop config
        # issues are the most common case here.
        _render_guided(store_path_unwritable(store_path, e))
        return 2
    return 0


def _cmd_mine_queries(
    *,
    positional_url: str | None,
    url_env: str | None,
    store_path: str,
) -> int:
    """Harvest `pg_stat_statements` into the local `example_queries` table.

    Source-side requirements (operator's job):
      - `pg_stat_statements` listed in `shared_preload_libraries` so
        the view is populated.
      - `CREATE EXTENSION pg_stat_statements` in the target database.
      - The connecting role can `SELECT` from the view (default for
        superusers; non-super roles need `pg_read_all_stats` grant).

    When the view isn't readable the pipeline soft-skips: the handler
    prints an actionable message and exits 0 (this is operator config,
    not a Schema Brain bug).

    The engine is built with `default_transaction_read_only=on` —
    mining is strictly a read operation and the session-level
    enforcement prevents any future regression that accidentally
    issues a write to the source.
    """
    import sqlalchemy
    from sqlalchemy.pool import NullPool

    source_url = _resolve_url_source(positional=positional_url, url_env=url_env)
    if source_url is None:
        return 2
    if _resolve_url(source_url) is None:
        return 2
    source_id = _make_source_id(source_url)

    import sqlite3

    # `NullPool` eliminates the latent pool-state escape surface the
    # moment any future mining feature touches a shared connection.
    # `safe_engine_url` strips smuggled session-config from the URL
    # query string. The raw `source_url` still seeds `_make_source_id`
    # above so source identity stays stable regardless of smuggle
    # attempts.
    engine = sqlalchemy.create_engine(
        safe_engine_url(source_url),
        poolclass=NullPool,
        connect_args={"options": "-c default_transaction_read_only=on"},
    )
    try:
        with SQLiteStore(store_path) as store:
            report = mine_queries(
                engine=engine,
                store=store,
                source_connection_id=source_id,
            )
    except OperationalError as exc:
        # Connection failures (wrong host, auth failure, timeout)
        # surface here just like every other Postgres-touching
        # subcommand — guided message, not a raw traceback.
        _render_guided(postgres_operational_error(exc, url_hint=source_url))
        return 2
    except OSError as exc:
        _render_guided(store_path_unwritable(store_path, exc))
        return 2
    except sqlite3.DatabaseError as exc:
        # CHECK / FK / UNIQUE / IntegrityError from the store-side
        # batch UPSERT. The mining pipeline filters to indexed tables
        # before writing, so an IntegrityError here is structural —
        # either schema drift mid-run (operator did something to the
        # store file in another process) or a programming error.
        # Surface as a guided message instead of an unhandled
        # traceback.
        print(
            "mine-queries: store write failed.\n"
            f"  why: {exc}\n"
            "  fix: re-run `schemabrain index` to rebuild the store "
            "from scratch; if the error persists, file an issue with "
            "the message above.",
            file=sys.stderr,
        )
        return 2
    finally:
        engine.dispose()

    if report.skipped_unavailable:
        print(
            "mine-queries: pg_stat_statements unavailable on the source "
            "database; no rows written.\n"
            "  why: the extension isn't installed/loaded, or the role "
            "lacks read access.\n"
            "  fix: add `pg_stat_statements` to `shared_preload_libraries` "
            "(requires a Postgres restart), then run "
            "`CREATE EXTENSION pg_stat_statements;` in the target database.\n"
            "  re-run `schemabrain mine-queries` once the view is "
            "readable.",
            file=sys.stderr,
        )
    else:
        print(
            f"mine-queries: scanned {report.statements_read} statements, "
            f"used {report.statements_used}, wrote {report.rows_written} "
            f"example_queries rows.",
            file=sys.stderr,
        )
    return 0


def _cmd_entities_apply(
    *,
    yaml_path: str,
    positional_url: str | None,
    url_env: str | None,
    store_path: str,
) -> int:
    """Load one entity YAML file and write it to the local store.

    Non-interactive by design — this is the deterministic file-to-
    store operation. `entities suggest --apply` is the LLM-suggest
    write path; both share the same `Store.write_entity` call.

    Error surface:
      - exit 1 on parse error, missing file, FK violation
        (unindexed bound table), or dbt-guard refusal
      - exit 2 on URL-source mismatch / unwritable store path
        (mirrors the existing pattern in `serve` / `mine-queries`)
      - exit 0 on successful write (prints `applied entity: <name>`
        to stdout)
    """
    source_url = _resolve_url_source(positional=positional_url, url_env=url_env)
    if source_url is None:
        return 2
    if _resolve_url(source_url) is None:
        return 2
    source_id = _make_source_id(source_url)

    path = Path(yaml_path)
    try:
        entity = parse_entity_yaml_file(path)
    except FileNotFoundError:
        _entity_error(f"entity YAML file not found: {yaml_path}")
        return 1
    except IsADirectoryError:
        _entity_error(f"{yaml_path!r} is a directory, not a file")
        return 1
    except EntityParseError as exc:
        _entity_error(f"parsing {yaml_path}: {exc}")
        return 1

    try:
        with SQLiteStore(store_path) as store:
            try:
                store.write_entity(entity, source_connection_id=source_id)
            except DbtOwnedEntityError as exc:
                _entity_error(str(exc))
                return 1
            except sqlite3.IntegrityError:
                # The bound-table FK is the only IntegrityError this
                # call path can raise — surface as a guided message
                # pointing the user at `schemabrain index`.
                _entity_error(
                    f"entity {entity.name!r} binds to table "
                    f"{entity.qualified_table!r} which isn't indexed "
                    f"for this source. Run `schemabrain index` first to make "
                    f"the table available, then re-run `entities apply`."
                )
                return 1
            except sqlite3.DatabaseError as exc:
                # Catch-all for non-Integrity DB-level errors: disk full,
                # WAL checkpoint failure, CHECK constraint trips on a
                # corrupted store, etc. Returns exit 2 (structural, not
                # user input) and routes through `_render_guided` so
                # the user gets the same shape of message that other
                # CLI commands produce for store-level failures.
                _render_guided(
                    GuidedError(
                        kind="entities_apply_store_error",
                        message=f"store-level error during write: {exc}",
                        why="the SQLite store reported an error other than a foreign-key violation",
                        fix="check the store file integrity, available disk "
                        "space, and that no other Schema Brain process is "
                        "writing to the same store",
                        next_step=f"inspect {store_path} with `sqlite3 .schema`",
                    )
                )
                return 2
    except OSError as e:
        _render_guided(store_path_unwritable(store_path, e))
        return 2

    print(f"applied entity: {entity.name}")
    return 0


def _cmd_entities_suggest(
    *,
    positional_url: str | None,
    url_env: str | None,
    store_path: str,
    dry_run: bool,
    out_dir: str | None,
    apply: bool,
    top_k: int,
    provider: str,
    max_cost_usd: float | None,
) -> int:
    """LLM-suggest entities for an indexed schema.

    Orchestrates: resolve source -> read indexed tables from store ->
    build LLM client (anthropic or stub) wrapped in CostCeilingGuard ->
    run suggest pipeline -> render output per mode (dry-run / out-dir /
    apply). All LLM cost flows through the guard so a runaway run is
    bounded by `--max-cost-usd` (or `SCHEMABRAIN_MAX_LLM_COST_USD`).

    Exit codes:
      0: success
      1: user-input class (empty schema, malformed LLM output, ceiling
         breached, dbt-guard refusal)
      2: structural (missing URL, missing API key, unwritable store)
    """
    source_url = _resolve_url_source(positional=positional_url, url_env=url_env)
    if source_url is None:
        return 2
    if _resolve_url(source_url) is None:
        return 2
    source_id = _make_source_id(source_url)

    # Resolve the cost ceiling: CLI flag > env var > default.
    if max_cost_usd is None:
        env_value = os.environ.get(_SUGGEST_COST_ENV_VAR)
        if env_value is not None:
            try:
                max_cost_usd = float(env_value)
            except ValueError:
                _render_guided(
                    GuidedError(
                        kind="suggest_cost_env_malformed",
                        message=f"{_SUGGEST_COST_ENV_VAR}={env_value!r} is not a valid number",
                        why="cost ceiling must be a positive float (USD)",
                        fix=f"unset {_SUGGEST_COST_ENV_VAR} or set it to a number "
                        f"(e.g. {_SUGGEST_COST_ENV_VAR}=0.50)",
                        next_step="see `schemabrain entities suggest --help`",
                    )
                )
                return 2
        else:
            max_cost_usd = _DEFAULT_SUGGEST_MAX_COST_USD

    # Build the LLM client. Stub reads canned YAML from env (so the
    # multi-line response stays out of argv). Anthropic reads
    # ANTHROPIC_API_KEY — same env source as `index`.
    llm_client: LLMClient
    if provider == "stub":
        canned = os.environ.get(_SUGGEST_STUB_RESPONSE_ENV_VAR)
        if canned is None:
            # `--provider stub` is meaningful only with a canned response.
            # The empty-default would silently exit 0 with no candidates,
            # which masks a misconfigured CI job that forgot to set the
            # env var. Warn loudly to stderr and use the empty default
            # only after the warning fires.
            print(
                f"warning: --provider stub with {_SUGGEST_STUB_RESPONSE_ENV_VAR} "
                f"unset; defaulting to an empty candidate list. Set "
                f"{_SUGGEST_STUB_RESPONSE_ENV_VAR} to provide a canned response.",
                file=sys.stderr,
            )
            canned = "candidates: []"
        llm_client = FakeLLMClient(text_provider=lambda _s, _u: canned)
    else:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            _render_guided(
                GuidedError(
                    kind="anthropic_api_key_missing",
                    message="ANTHROPIC_API_KEY is not set",
                    why="entity suggestion uses Claude (Sonnet 4.6) to "
                    "analyse your schema; the SDK needs a key",
                    fix="export ANTHROPIC_API_KEY=sk-ant-... and re-run, OR "
                    "use --provider stub for offline runs",
                    next_step="get a key at https://console.anthropic.com/settings/keys",
                )
            )
            return 2
        llm_client = anthropic_sonnet_46_client(api_key=api_key)

    guard = CostCeilingGuard(inner=llm_client, max_cost_usd=max_cost_usd)
    pipeline = EntitySuggestionPipeline(llm=guard)

    # Read the indexed schema into Table objects. Bail with a guided
    # error rather than calling the LLM with an empty schema.
    try:
        tables = _load_tables_for_source(store_path=store_path, source_id=source_id)
    except OSError as e:
        _render_guided(store_path_unwritable(store_path, e))
        return 2
    if not tables:
        _render_guided(
            GuidedError(
                kind="suggest_empty_schema",
                message="no tables in the local store for this source",
                why="entity suggestion needs an indexed schema to analyse",
                fix="run `schemabrain index --url-env DATABASE_URL` first, "
                "then re-run `entities suggest`",
                next_step=f"verify with `sqlite3 {store_path} 'select count(*) from tables'`",
            )
        )
        return 1

    try:
        result = pipeline.propose_from_tables(tables, top_k=top_k)
    except CostCeilingExceededError as exc:
        _render_guided(
            GuidedError(
                kind="suggest_cost_ceiling_exceeded",
                message=str(exc),
                why="the suggested prompt would exceed --max-cost-usd",
                fix="re-run with a higher --max-cost-usd (or set "
                f"{_SUGGEST_COST_ENV_VAR} in your environment)",
                next_step="use --provider stub for cost-free smoke testing",
            )
        )
        return 1
    except SuggestionParseError as exc:
        _render_guided(
            GuidedError(
                kind="suggest_llm_output_malformed",
                message=f"LLM returned unparseable YAML: {exc}",
                why="the suggestion grammar requires strict YAML with a "
                "top-level `candidates` list",
                fix="re-run; transient LLM hiccups usually clear on retry. "
                "Repeated failures suggest a prompt issue worth filing.",
                next_step="if reproducible, please open an issue with the LLM response captured",
            )
        )
        return 1

    if dry_run:
        _render_dry_run(result)
        return 0
    if out_dir is not None:
        return _render_to_out_dir(result, Path(out_dir))
    if not apply:  # pragma: no cover — argparse mutex group makes this unreachable
        # `assert` would be stripped under `python -O`, silently
        # returning None (which sys.exit treats as 0). Use an
        # explicit raise so the invariant survives optimization.
        raise RuntimeError(
            "unreachable: argparse mutex group requires --dry-run, --out-dir, or --apply"
        )
    return _render_apply(result, store_path=store_path, source_id=source_id)


def _load_tables_for_source(*, store_path: str, source_id: str) -> list[Table]:
    """Read every indexed Table for `source_id` from the local store.

    Wraps `Store.list_tables` + `get_table` so the caller gets the
    full hydrated Table list in one shot. Returns an empty list if
    the store has no rows for this source (the suggest CLI's
    "did you index yet?" check fires on that).
    """
    with SQLiteStore(store_path) as store:
        names = store.list_tables(source_connection_id=source_id)
        tables: list[Table] = []
        for schema, name in names:
            table = store.get_table(schema, name, source_connection_id=source_id)
            if table is not None:
                tables.append(table)
        return tables


def _render_dry_run(result: SuggestionResult) -> None:
    """Print suggestion candidates to stdout in human-readable form.

    Each candidate is rendered as a YAML body (the apply-ready entity
    grammar) with envelope fields (confidence, rationale, pii_hints)
    as comment lines above. A trailing summary reports total cost and
    the LLM model.
    """
    if not result.candidates:
        print("no candidates suggested.")
        return
    for candidate in result.candidates:
        print(_format_candidate_for_dry_run(candidate))
        print()
    print(
        f"-- {len(result.candidates)} candidate(s) | "
        f"model: {result.llm_model} | "
        f"cost: ${result.total_cost_usd:.4f}"
    )


def _collapse_newlines(value: str) -> str:
    """Collapse newlines to spaces for use inside a `# ...` comment line.

    The dry-run renderer emits `# rationale: <value>` as a single
    comment line. A newline in `value` would break the comment-prefix
    invariant — the next line would lack `# ` and could be interpreted
    as live YAML if the dry-run output is copy-pasted into a file.
    """
    return value.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


def _format_candidate_for_dry_run(candidate: EntityCandidate) -> str:
    """Render one EntityCandidate as YAML body + envelope comments.

    The body matches the canonical entity YAML grammar (so it could be
    copy-pasted into a file and applied verbatim). Envelope fields
    appear as `# <field>: <value>` comments above the body — visible
    to humans, invisible to `parse_entity_yaml`.
    """
    rationale = _collapse_newlines(candidate.rationale or "(no rationale provided)")
    lines: list[str] = [
        f"# confidence: {candidate.confidence}",
        f"# rationale: {rationale}",
    ]
    if candidate.pii_hints:
        lines.append("# pii_hints:")
        for col, sensitivity in sorted(candidate.pii_hints.items()):
            lines.append(f"#   {col}: {sensitivity}")
    lines.extend(_format_entity_yaml_body(candidate).splitlines())
    return "\n".join(lines)


def _format_entity_yaml_body(candidate: EntityCandidate) -> str:
    """Render the canonical entity YAML body — apply-ready, no envelope.

    Uses `yaml.safe_dump` for the description scalar so an LLM-supplied
    value containing colons, newlines, or other YAML-special characters
    is properly quoted/escaped. Manual string concatenation here would
    open the door to YAML injection where a malicious or careless
    description string fragments the document. The dumped value is
    spliced back into a hand-rolled key-order layout so the file
    matches the same field ordering that `entities apply` and the
    bundled fixtures use.
    """
    entity = candidate.entity
    body: dict[str, object] = {
        "version": 1,
        "name": entity.name,
    }
    if entity.description:
        body["description"] = entity.description
    body["binding"] = {"single_table": entity.qualified_table}
    body["identity"] = entity.identity
    body["origin"] = entity.origin
    # `sort_keys=False` preserves the insertion order set above.
    # `allow_unicode=True` keeps non-ASCII (e.g., entity descriptions
    # in any language) human-readable instead of `\u` escaped.
    return yaml.safe_dump(
        body,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    ).rstrip()


def _render_to_out_dir(result: SuggestionResult, out_dir: Path) -> int:
    """Write one apply-ready YAML per candidate plus a metadata sidecar.

    Per-entity YAML is the canonical entity grammar — clean of
    envelope fields. The sidecar `_suggestion_metadata.json` carries
    confidence/rationale/pii_hints keyed by entity name, so a human
    reviewing the directory can see the LLM's reasoning without it
    leaking into the persisted entity rows.

    Refuses to overwrite existing files: a user who has hand-edited a
    previous run's YAML in this directory should not lose that edit
    silently. The conflict check fires before any write, so a partial
    write isn't possible either.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    # Pre-check for conflicts so we either write everything or write
    # nothing — no partial overwrites of user-edited files.
    conflicts: list[str] = []
    for candidate in result.candidates:
        if (out_dir / f"{candidate.entity.name}.yaml").exists():
            conflicts.append(f"{candidate.entity.name}.yaml")
    sidecar = out_dir / "_suggestion_metadata.json"
    if sidecar.exists():
        conflicts.append("_suggestion_metadata.json")
    if conflicts:
        _render_guided(
            GuidedError(
                kind="suggest_out_dir_conflict",
                message=f"{out_dir} already contains: {', '.join(sorted(conflicts))}",
                why="overwriting existing files would lose any hand-edits "
                "made between suggest runs",
                fix="pass --out-dir to a fresh directory, or delete the conflicting files first",
                next_step="for review-then-apply workflows, copy the "
                "edited files elsewhere before re-running suggest",
            )
        )
        return 1

    metadata: dict[str, dict[str, object]] = {}
    for candidate in result.candidates:
        yaml_path = out_dir / f"{candidate.entity.name}.yaml"
        yaml_path.write_text(_format_entity_yaml_body(candidate) + "\n")
        metadata[candidate.entity.name] = {
            "confidence": candidate.confidence,
            "rationale": candidate.rationale,
            "pii_hints": dict(candidate.pii_hints),
        }
    sidecar.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    print(
        f"wrote {len(result.candidates)} candidate(s) to {out_dir} | "
        f"model: {result.llm_model} | "
        f"cost: ${result.total_cost_usd:.4f}"
    )
    return 0


def _render_apply(
    result: SuggestionResult,
    *,
    store_path: str,
    source_id: str,
) -> int:
    """Write suggested candidates to the store with origin='suggested'.

    `store.write_entity` commits per call (each is its own SQLite
    transaction). If candidate N fails (dbt-guard refusal or FK
    violation on the bound table), candidates 0..N-1 are already
    durably committed. The error message names how many entities
    landed before the failure so the user knows the state of the
    store without having to query it manually.
    """
    written: list[str] = []
    total = len(result.candidates)
    try:
        with SQLiteStore(store_path) as store:
            for candidate in result.candidates:
                try:
                    store.write_entity(candidate.entity, source_connection_id=source_id)
                except DbtOwnedEntityError as exc:
                    _entity_error(_partial_write_message(written, total, str(exc)))
                    return 1
                except sqlite3.IntegrityError:
                    _entity_error(
                        _partial_write_message(
                            written,
                            total,
                            f"entity {candidate.entity.name!r} binds to table "
                            f"{candidate.entity.qualified_table!r} which isn't "
                            f"indexed for this source. The LLM proposed a table "
                            f"that doesn't appear in the store — re-run "
                            f"`schemabrain index` first.",
                        )
                    )
                    return 1
                written.append(candidate.entity.name)
    except OSError as e:
        _render_guided(store_path_unwritable(store_path, e))
        return 2
    print(
        f"applied {len(result.candidates)} suggested entity/ies | "
        f"model: {result.llm_model} | "
        f"cost: ${result.total_cost_usd:.4f}"
    )
    return 0


def _partial_write_message(written: list[str], total: int, error: str) -> str:
    """Prefix an apply-mode error with the count of entities that landed.

    `write_entity` commits per call, so a failure mid-loop leaves the
    store in a partial-write state. The user needs to know which
    entities landed and which didn't so they can re-run cleanly.
    """
    if not written:
        return error
    return (
        f"{len(written)} of {total} entities were written before this "
        f"failure ({', '.join(repr(n) for n in written)}). "
        f"Re-running --apply is safe (UPSERT semantics) once the "
        f"underlying issue is fixed. {error}"
    )


def _entity_error(message: str) -> None:
    """Render an `entities apply` user-input-class error to stderr.

    The user-input-class errors (parse failure, dbt-guard refusal,
    bound-table FK violation) are deliberately plain stderr writes
    rather than `_render_guided` panels — they map 1:1 to a YAML field
    or store state the user can directly edit, so the rich panel adds
    visual weight without information. Structural failures (store
    corruption, unwritable path) still use `_render_guided`.
    """
    print(f"error: {message}", file=sys.stderr)


def _cmd_fixture_path(name: str) -> int:
    """Print the absolute path to a bundled fixture, or fail with a
    helpful message.

    Stdout is paste-clean (no decoration, no trailing diagnostic) so the
    command can drop into shell substitution.
    """
    try:
        path = resolve_bundled_path(name)
    except (FileNotFoundError, ValueError) as e:
        _render_guided(
            GuidedError(
                kind="fixture_not_found",
                message=str(e),
                why="`fixture-path` resolves bundled assets shipped inside the wheel",
                fix="see `schemabrain fixture-path --help` for the recognized names",
                next_step="bundled today: `ecommerce.sql` (SQL seed), `ecommerce.json` (golden set)",
            )
        )
        return 2
    print(str(path))
    return 0


def _build_index_reporter(*, quiet: bool) -> IndexReporter:
    """Pick a reporter for `schemabrain index`.

    `--quiet` always returns the no-op reporter. Otherwise we use the
    rich-powered reporter only when stderr is a real terminal; piping
    to a log file falls back to no-op so we don't flood the output
    with cursor-control escape sequences. The final summary line is
    printed by `_cmd_index` regardless.
    """
    if quiet or not sys.stderr.isatty():
        return NullReporter()
    # Lazy import: keeps `rich` off the import path for `serve`, `eval`,
    # and `fixture-path`, and gives a clearer error if rich is missing
    # at runtime (rather than failing every CLI invocation).
    from schemabrain.cli_ui import RichReporter

    return RichReporter()


def _stderr_console():
    """Build a rich Console targeting stderr for guided-error rendering.

    Lazy-imported so subcommands that never error (e.g. `fixture-path`
    on a happy path) don't pay the rich import cost. TTY detection is
    delegated to rich — non-TTY destinations get plain text (markup
    stripped) automatically.
    """
    from rich.console import Console

    return Console(stderr=True)


def _render_guided(err: GuidedError) -> None:
    """Render a `GuidedError` to stderr via a fresh rich Console.

    The only place in the CLI that writes guided errors. Direct
    `print("error: ...")` calls are reserved for cases without a
    translator yet (argparse output, raw-string CostCapExceeded
    fallback).
    """
    render_error(err, console=_stderr_console())


def _resolve_url_source(
    *,
    positional: str | None,
    url_env: str | None,
) -> str | None:
    """Resolve a connection URL from either a positional arg or a named env var.

    Returns the raw URL string on success, or `None` after rendering a
    guided error to stderr. Emits a single-line deprecation warning to
    stderr when `positional` is used AND contains an embedded password,
    nudging the user toward `--url-env`. Env-var resolution is always
    silent — env is the safe path we're nudging users toward.

    Rules:
      - exactly one of {positional, url_env} must be provided
      - `url_env` names an env var; the var must exist and be non-empty
      - the warning does NOT echo the password back at the user (which
        would defeat the point — the warning would itself become a leak
        on a shared terminal or screen-recording)
    """
    if positional is not None and url_env is not None:
        _render_guided(
            GuidedError(
                kind="url_source_conflict",
                message="both a positional URL and --url-env were given",
                why="only one source for the connection URL is allowed per run",
                fix="pass either the positional URL or --url-env VARNAME, not both",
                next_step="prefer --url-env so credentials never appear in argv",
            )
        )
        return None
    if positional is None and url_env is None:
        _render_guided(
            GuidedError(
                kind="url_source_missing",
                message="no connection URL provided",
                why="schemabrain needs a Postgres URL to reach your source database",
                fix="re-run with --url-env VARNAME (where VARNAME holds the URL), "
                "OR pass the URL positionally (less safe — leaks creds to argv)",
                next_step="see docs/setup.md for the canonical URL format",
            )
        )
        return None
    if url_env is not None:
        value = os.environ.get(url_env)
        if value is None:
            _render_guided(
                GuidedError(
                    kind="url_env_unset",
                    message=f"environment variable {url_env!r} is not set",
                    why="--url-env names an env var that must hold the connection URL",
                    fix=f"export {url_env}=postgresql+psycopg://USER:PASSWORD@HOST:5432/DBNAME",
                    next_step="see docs/setup.md for the canonical URL format",
                )
            )
            return None
        if value == "":
            _render_guided(
                GuidedError(
                    kind="url_env_empty",
                    message=f"environment variable {url_env!r} is set but empty",
                    why="--url-env names an env var that must hold the connection URL",
                    fix=f"export {url_env}=postgresql+psycopg://USER:PASSWORD@HOST:5432/DBNAME",
                    next_step="see docs/setup.md for the canonical URL format",
                )
            )
            return None
        return value
    # Positional path. We accept it for backwards compatibility, but if
    # it embeds a non-empty password we warn — that's the exact leak the
    # audit flagged HIGH (argv visible to ps, shell history, journald).
    # We deliberately do NOT echo the password in the warning.
    # Truthiness (not `is not None`) is intentional: `urlparse` returns
    # an empty string for `user:@host`, a valid no-password form used
    # by .pgpass / peer-auth setups. An empty password isn't a leak.
    # By this branch, `positional` is guaranteed non-None (the earlier
    # both-None guard would have returned).
    parsed_password: str | None = None
    try:
        parsed_password = urlparse(positional).password
    except ValueError:
        # Malformed URL — let downstream _resolve_url surface the real
        # error rather than guessing here.
        parsed_password = None
    if parsed_password:
        print(
            "warning: passing a credentialed URL on the command line leaks the "
            "password into shell history, `ps`, and process logs. Use "
            "--url-env VARNAME to read the URL from an environment variable.",
            file=sys.stderr,
        )
    return positional


def _resolve_url(url: str) -> str | None:
    """Validate + canonicalize a connection URL, rendering on failure.

    Returns the canonical (credential-free) URL on success, or `None`
    after rendering a guided error to stderr. CLI commands collapse
    the URL handshake into:

        canonical = _resolve_url(url)
        if canonical is None:
            return 2

    Two failure modes are translated:
      1. Wrong driver scheme (bare `postgresql://`, psycopg2, asyncpg)
         — caught BEFORE `_canonical_url` so the user sees a guided
         "use postgresql+psycopg://..." instead of a downstream
         `ModuleNotFoundError: psycopg2` at SQLAlchemy time.
      2. No-scheme / unsupported-scheme — `_canonical_url`'s
         ValueError is wrapped into a `url_invalid` guided error.
    """
    parsed = urlparse(url)
    wrong = url_wrong_driver(parsed.scheme, url)
    if wrong is not None:
        _render_guided(wrong)
        return None
    try:
        return _canonical_url(url)
    except ValueError as e:
        _render_guided(
            GuidedError(
                kind="url_invalid",
                message=str(e),
                why="Schema Brain needs a Postgres URL to connect to your source database",
                fix="use the form postgresql+psycopg://USER:PASSWORD@HOST:5432/DBNAME",
                next_step="see docs/setup.md for the canonical URL format",
            )
        )
        return None


def _canonical_url(url: str) -> str:
    """Return the credential-free, port-normalized form of a connection URL.

    Raises ValueError if the URL has no scheme or an unsupported one.
    """
    parsed = urlparse(url)
    if not parsed.scheme:
        raise ValueError(f"Invalid connection URL (no scheme): {url!r}")
    if parsed.scheme not in _POSTGRES_SCHEMES:
        raise ValueError(
            f"Unsupported scheme {parsed.scheme!r}; expected one of {sorted(_POSTGRES_SCHEMES)}"
        )
    port = parsed.port or _POSTGRES_SCHEMES[parsed.scheme]
    host = parsed.hostname or ""
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{host}:{port}{path}"


def _make_source_id(url: str) -> str:
    """Stable short identifier for the source DB, derived from its URL.

    Strips credentials and normalizes default port + trailing slash so the
    same database produces the same source ID regardless of which user or
    URL form indexed it.
    """
    canonical = _canonical_url(url)
    return hashlib.sha256(canonical.encode()).hexdigest()[:_SOURCE_ID_LENGTH]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
