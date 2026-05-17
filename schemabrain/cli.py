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
import dataclasses
import hashlib
import json
import os
import sqlite3
import sys
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from urllib.parse import urlparse

import sqlalchemy
import yaml
from sqlalchemy.exc import OperationalError

from schemabrain import __version__
from schemabrain.connectors._url import safe_engine_url
from schemabrain.connectors.base import DataSource
from schemabrain.connectors.postgres import PostgresDataSource
from schemabrain.core.metric import DbtOwnedMetricError
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
from schemabrain.imports.dbt import (
    DbtImportPlan,
    DbtImportResult,
    DbtManifestParseError,
    apply_dbt_import_plan,
    parse_dbt_manifest,
    plan_dbt_import,
)
from schemabrain.imports.dbt_metrics import (
    DbtMetricImportError,
    DbtMetricSkip,
    parse_dbt_metrics,
)
from schemabrain.indexer import IndexReporter, NullReporter, dry_run_index, index
from schemabrain.joins.suggest import (
    JoinCandidate,
    JoinGraphReport,
    detect_cycles_in_join_graph,
    suggest_canonical_joins,
)
from schemabrain.joins.yaml_grammar import (
    CanonicalJoinParseError,
    parse_canonical_join_yaml_file,
)
from schemabrain.logging_config import configure_logging
from schemabrain.mcp.metric_executor import EngineMetricExecutor
from schemabrain.mcp.server import run_stdio
from schemabrain.metrics.yaml_grammar import (
    MetricYamlError,
    parse_metric_yaml_file,
)
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
    if args.command == "import":
        if args.import_action == "dbt":
            return _cmd_import_dbt(
                manifest_path=args.manifest_path,
                positional_url=args.source,
                url_env=args.url_env,
                store_path=args.store_path,
                dry_run=args.dry_run,
                report_path=args.report_path,
                include_metrics=args.include_metrics,
            )
        # Symmetric with the entities branch — argparse subparser
        # `required=True` blocks the fall-through, but a structurally
        # unreachable branch is cheaper than a guarded assertion.
        parser.error(f"unknown import action: {args.import_action}")  # pragma: no cover
    if args.command == "joins":
        if args.joins_action == "suggest":
            return _cmd_joins_suggest(
                positional_url=args.source,
                url_env=args.url_env,
                store_path=args.store_path,
                dry_run=args.dry_run,
                out_dir=args.out_dir,
                apply=args.apply,
                top_k=args.top_k,
                report_path=args.report_path,
            )
        if args.joins_action == "apply":
            return _cmd_joins_apply(
                yaml_path=args.yaml_path,
                positional_url=args.source,
                url_env=args.url_env,
                store_path=args.store_path,
            )
        if args.joins_action == "list":
            return _cmd_joins_list(
                store_path=args.store_path,
                positional_url=args.source,
                url_env=args.url_env,
            )
        parser.error(f"unknown joins action: {args.joins_action}")  # pragma: no cover
    if args.command == "doctor":
        return _cmd_doctor(
            positional_url=args.source,
            url_env=args.url_env,
            store_path=args.store_path,
            host=args.host,
            json_output=args.json,
        )
    if args.command == "init":
        return _cmd_init(
            positional_url=args.source,
            url_env=args.url_env,
            store_path=args.store_path,
            host=args.host,
            env_var=args.env_var,
            skip_index=args.skip_index,
            assume_yes=args.assume_yes,
            print_only=args.print_only,
        )
    if args.command == "metrics":
        if args.metrics_action == "apply":
            return _cmd_metrics_apply(
                yaml_path=args.yaml_path,
                positional_url=args.source,
                url_env=args.url_env,
                store_path=args.store_path,
            )
        if args.metrics_action == "list":
            return _cmd_metrics_list(
                store_path=args.store_path,
                positional_url=args.source,
                url_env=args.url_env,
            )
        parser.error(f"unknown metrics action: {args.metrics_action}")  # pragma: no cover
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

    # `import` is a subgroup for external semantic-source ingestion.
    # Today: dbt manifest.json import. Future: Cube YAML, OSI semantic
    # models drop in alongside as new sub-actions.
    p_import = sub.add_parser(
        "import",
        help="Import semantic definitions from an external source (dbt, etc.)",
    )
    import_sub = p_import.add_subparsers(dest="import_action", required=True)

    p_import_dbt = import_sub.add_parser(
        "dbt",
        help="Import entities from a dbt manifest.json (read-only, no export).",
    )
    p_import_dbt.add_argument(
        "manifest_path",
        help="Path to your dbt project's compiled `target/manifest.json`. "
        "Run `dbt compile` in your dbt project to produce it. Remote "
        "(dbt Cloud) manifests aren't supported at v1; download "
        "locally and pass the path.",
    )
    p_import_dbt.add_argument(
        "--source",
        default=None,
        help="The same source URL passed to `index`. DEPRECATED when the "
        "URL contains a password; prefer --url-env.",
    )
    p_import_dbt.add_argument(
        "--url-env",
        dest="url_env",
        metavar="VARNAME",
        default=None,
        help="Name of the environment variable that holds the source URL. "
        "Preferred over --source so credentials never appear in argv.",
    )
    p_import_dbt.add_argument(
        "--store-path",
        default=_DEFAULT_STORE_PATH,
        help=f"Path to the local SQLite store (default: {_DEFAULT_STORE_PATH})",
    )
    p_import_dbt.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute the plan (added / updated / ownership-transferred / "
        "skipped / orphans) and print the summary, but write nothing. "
        "Best mode for CI previews.",
    )
    p_import_dbt.add_argument(
        "--report",
        dest="report_path",
        default=None,
        help="Optional path. If set, the run writes a JSON report of the "
        "plan (bucket counts + per-model details) to this path. Works "
        "with both dry-run and apply.",
    )
    p_import_dbt.add_argument(
        "--include-metrics",
        dest="include_metrics",
        action="store_true",
        help="Also import dbt metrics (type=simple only) anchored on "
        "the entities imported in this run. Skips ratio/derived/"
        "cumulative metrics with structured reasons. Off by default "
        "to preserve backwards-compatible behaviour from earlier "
        "releases; on by default in a future release.",
    )

    # `joins` — canonical-join-graph commands. Mirrors `entities` shape:
    # `suggest` (3 modes: dry-run / out-dir / apply, plus --report),
    # `apply` (single file OR directory of YAMLs), `list` (verification
    # path after `apply`). No `joins inspect` at v1 — that's Q15
    # (`schemabrain inspect`) territory.
    p_joins = sub.add_parser(
        "joins",
        help="Manage canonical-join definitions (the canonical-join semantic-layer graph).",
    )
    joins_sub = p_joins.add_subparsers(dest="joins_action", required=True)

    p_joins_suggest = joins_sub.add_parser(
        "suggest",
        help="Mine FK + query-log evidence; print, write, or apply candidates.",
    )
    p_joins_suggest.add_argument(
        "--source",
        default=None,
        help="The same source URL passed to `index`. DEPRECATED when "
        "the URL contains a password; prefer --url-env.",
    )
    p_joins_suggest.add_argument(
        "--url-env",
        dest="url_env",
        metavar="VARNAME",
        default=None,
        help="Name of the environment variable that holds the source URL. "
        "Preferred over --source so credentials never appear in argv.",
    )
    p_joins_suggest.add_argument(
        "--store-path",
        default=_DEFAULT_STORE_PATH,
        help=f"Path to the local SQLite store (default: {_DEFAULT_STORE_PATH})",
    )
    # Three output modes, mutually exclusive (argparse enforces). Same
    # shape as `entities suggest` so users learn the pattern once.
    joins_mode = p_joins_suggest.add_mutually_exclusive_group(required=True)
    joins_mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Print ranked candidates (provenance + on-columns) to "
        "stdout. No files written, no store writes. Best mode for "
        "previewing what the suggester sees.",
    )
    joins_mode.add_argument(
        "--out-dir",
        dest="out_dir",
        default=None,
        help="Directory to write one YAML file per candidate "
        "(<candidate_name>.yaml). Each file is `joins apply`-ready: "
        "edit description / name, then apply per file or as a "
        "directory.",
    )
    joins_mode.add_argument(
        "--apply",
        action="store_true",
        help="Write candidates directly to the local store with "
        "origin='suggested'. Skips the review step — use --out-dir if "
        "you want a chance to edit before committing.",
    )
    p_joins_suggest.add_argument(
        "--top-k",
        dest="top_k",
        type=int,
        default=None,
        help="Maximum number of candidates to keep (default: unlimited). "
        "Ranked by (confidence DESC, query-log frequency DESC, name ASC).",
    )
    p_joins_suggest.add_argument(
        "--report",
        dest="report_path",
        default=None,
        help="Optional path. If set, the run writes a JSON report "
        "covering bucket counts + structural cycle analysis (per "
        "the design) to this path. Works with every mode.",
    )

    p_joins_apply = joins_sub.add_parser(
        "apply",
        help="Load a canonical-join YAML file (or directory) into the local store.",
    )
    p_joins_apply.add_argument(
        "yaml_path",
        help="Path to a canonical-join YAML file, OR a directory of "
        "YAML files (each file ending in `.yaml`/`.yml`). Multi-file "
        "apply lands each file independently; an error in one file "
        "skips that file and reports it in the summary.",
    )
    p_joins_apply.add_argument(
        "--source",
        default=None,
        help="The same source URL passed to `index`. DEPRECATED when "
        "the URL contains a password; prefer --url-env.",
    )
    p_joins_apply.add_argument(
        "--url-env",
        dest="url_env",
        metavar="VARNAME",
        default=None,
        help="Name of the environment variable that holds the source URL.",
    )
    p_joins_apply.add_argument(
        "--store-path",
        default=_DEFAULT_STORE_PATH,
        help=f"Path to the local SQLite store (default: {_DEFAULT_STORE_PATH})",
    )

    p_joins_list = joins_sub.add_parser(
        "list",
        help="List canonical joins in the local store. The verification path after `joins apply`.",
    )
    p_joins_list.add_argument(
        "--store-path",
        default=_DEFAULT_STORE_PATH,
        help=f"Path to the local SQLite store (default: {_DEFAULT_STORE_PATH})",
    )
    p_joins_list.add_argument(
        "--source",
        default=None,
        help="Filter listing to one source (the same URL passed to "
        "`index`). Without this flag, lists across every source.",
    )
    p_joins_list.add_argument(
        "--url-env",
        dest="url_env",
        metavar="VARNAME",
        default=None,
        help="Name of the environment variable that holds the source URL.",
    )

    # ----- metrics -----
    #
    # Same shape as `entities apply` + `joins apply`. No `metrics
    # suggest` at v1 — metrics are business decisions, so the
    # LLM-suggest path defers to post-v1. Producers at v1 are hand-
    # authored YAML (this `apply`) + dbt-metrics import (in
    # `schemabrain import dbt --include-metrics`).
    p_metrics = sub.add_parser(
        "metrics",
        help="Manage metric definitions (entity-anchored business measures).",
    )
    metrics_sub = p_metrics.add_subparsers(dest="metrics_action", required=True)

    p_metrics_apply = metrics_sub.add_parser(
        "apply",
        help="Load a metric YAML file (or directory) into the local store.",
    )
    p_metrics_apply.add_argument(
        "yaml_path",
        help="Path to a metric YAML file, OR a directory of YAML files "
        "(each file ending in `.yaml`/`.yml`). Multi-file apply lands "
        "each file independently; an error in one file skips that file "
        "and reports it in the summary.",
    )
    p_metrics_apply.add_argument(
        "--source",
        default=None,
        help="The same source URL passed to `index`. DEPRECATED when "
        "the URL contains a password; prefer --url-env.",
    )
    p_metrics_apply.add_argument(
        "--url-env",
        dest="url_env",
        metavar="VARNAME",
        default=None,
        help="Name of the environment variable that holds the source URL.",
    )
    p_metrics_apply.add_argument(
        "--store-path",
        default=_DEFAULT_STORE_PATH,
        help=f"Path to the local SQLite store (default: {_DEFAULT_STORE_PATH})",
    )

    p_metrics_list = metrics_sub.add_parser(
        "list",
        help="List metrics in the local store. The verification path after `metrics apply`.",
    )
    p_metrics_list.add_argument(
        "--store-path",
        default=_DEFAULT_STORE_PATH,
        help=f"Path to the local SQLite store (default: {_DEFAULT_STORE_PATH})",
    )
    p_metrics_list.add_argument(
        "--source",
        default=None,
        help="Filter listing to one source (the same URL passed to "
        "`index`). Without this flag, lists across every source.",
    )
    p_metrics_list.add_argument(
        "--url-env",
        dest="url_env",
        metavar="VARNAME",
        default=None,
        help="Name of the environment variable that holds the source URL.",
    )

    p_doctor = sub.add_parser(
        "doctor",
        help="Run health checks against the host config, store, and (optionally) source",
    )
    p_doctor.add_argument(
        "--source",
        default=None,
        help="Source URL to probe (SELECT 1 + read-only session check on Postgres). "
        "DEPRECATED when the URL contains a password; prefer --url-env. "
        "Optional — if neither --source nor --url-env is given, source checks are skipped.",
    )
    p_doctor.add_argument(
        "--url-env",
        dest="url_env",
        metavar="VARNAME",
        default=None,
        help="Name of the environment variable that holds the source URL "
        "(e.g. --url-env DATABASE_URL). Preferred over --source so credentials "
        "never appear in argv. Mutually exclusive with --source.",
    )
    p_doctor.add_argument(
        "--store-path",
        default=_DEFAULT_STORE_PATH,
        help=f"Path to the local SQLite store (default: {_DEFAULT_STORE_PATH})",
    )
    p_doctor.add_argument(
        "--host",
        choices=("claude-desktop", "claude-code", "manual"),
        default="claude-desktop",
        help="Which host config to check. Use `manual` to skip host-config checks "
        "(default: claude-desktop)",
    )
    p_doctor.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON to stdout instead of the human-readable "
        "report to stderr. Useful for CI/monitoring scripts.",
    )

    p_init = sub.add_parser(
        "init",
        help="Wire schemabrain into an MCP host (Claude Desktop, Claude Code, or print snippet)",
    )
    p_init.add_argument(
        "--source",
        default=None,
        help="Source URL (e.g. postgresql+psycopg://...). DEPRECATED when the URL "
        "contains a password; prefer --url-env. One of --source / --url-env is required.",
    )
    p_init.add_argument(
        "--url-env",
        dest="url_env",
        metavar="VARNAME",
        default=None,
        help="Name of the environment variable that holds the source URL "
        "(e.g. --url-env DATABASE_URL). Preferred over --source so credentials "
        "never appear in argv. Mutually exclusive with --source.",
    )
    p_init.add_argument(
        "--store-path",
        default=_DEFAULT_STORE_PATH,
        help=f"Path to the local SQLite store (default: {_DEFAULT_STORE_PATH})",
    )
    p_init.add_argument(
        "--host",
        choices=("claude-desktop", "claude-code", "manual"),
        default="claude-desktop",
        help="Which host to wire. `manual` prints the snippet without writing "
        "anywhere (default: claude-desktop)",
    )
    p_init.add_argument(
        "--env-var",
        dest="env_var",
        default="SCHEMABRAIN_DATABASE_URL",
        help="Name of the env var the host will set when launching the MCP server "
        "(default: SCHEMABRAIN_DATABASE_URL). The DB URL goes into this env var, "
        "not into argv.",
    )
    p_init.add_argument(
        "--skip-index",
        dest="skip_index",
        action="store_true",
        help="Don't require the store to have any entities indexed. Pass this when "
        "you've indexed in a different session or plan to index later.",
    )
    p_init.add_argument(
        "--yes",
        "-y",
        dest="assume_yes",
        action="store_true",
        help="Overwrite an existing schemabrain entry in the host config without "
        "prompting. Only the schemabrain entry is touched; other entries are preserved.",
    )
    p_init.add_argument(
        "--print-only",
        dest="print_only",
        action="store_true",
        help="Alias for --host manual: print the snippet, write nothing.",
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
    # Build a read-only SQLAlchemy engine for `get_metric` to execute
    # compiled SQL against. Same posture as `PostgresProfiler` from
    # PR #9: `default_transaction_read_only=on` is defense-in-depth on
    # top of the read-only role we already enforce at index time.
    try:
        engine = sqlalchemy.create_engine(
            safe_engine_url(source_url),
            connect_args={"options": "-c default_transaction_read_only=on"},
        )
    except (sqlalchemy.exc.ArgumentError, ValueError) as exc:  # pragma: no cover — defensive
        print(f"error: cannot construct read-only engine: {exc}", file=sys.stderr)
        return 2

    metric_executor = EngineMetricExecutor(engine)

    try:
        with SQLiteStore(store_path) as store:
            run_stdio(
                store=store,
                source_connection_id=source_id,
                embedder=fastembed_default(),
                metric_executor=metric_executor,
            )
    except OSError as e:
        # Unwritable directory, missing parent, etc. Surface as a
        # guided block instead of a traceback — Claude Desktop config
        # issues are the most common case here.
        _render_guided(store_path_unwritable(store_path, e))
        return 2
    finally:
        engine.dispose()
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
    if _resolve_url(source_url) is None:  # pragma: no cover — defensive
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
        llm_client = anthropic_sonnet_46_client(
            api_key=api_key
        )  # pragma: no cover — needs real ANTHROPIC_API_KEY

    guard = CostCeilingGuard(inner=llm_client, max_cost_usd=max_cost_usd)
    pipeline = EntitySuggestionPipeline(llm=guard)

    # Read the indexed schema into Table objects. Bail with a guided
    # error rather than calling the LLM with an empty schema.
    try:
        tables = _load_tables_for_source(store_path=store_path, source_id=source_id)
    except OSError as e:  # pragma: no cover — disk/permission failure not simulatable in CI
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
                next_step=f"verify with `sqlite3 {store_path} 'select count(*) from tables'`",  # nosec B608 — guided-error help text, not executable SQL
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
    except OSError as e:  # pragma: no cover — disk/permission failure not simulatable in CI
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


def _cmd_import_dbt(
    *,
    manifest_path: str,
    positional_url: str | None,
    url_env: str | None,
    store_path: str,
    dry_run: bool,
    report_path: str | None,
    include_metrics: bool = False,
    _source_factory: Callable[[str], AbstractContextManager[DataSource]] | None = None,
) -> int:
    """Read a dbt manifest.json and import its models as entities.

    Default mode writes through `Store.write_entity` with
    `origin="dbt_import"`. `--dry-run` computes the plan but writes
    nothing. `--report` writes a JSON summary for CI consumption.

    Error surface mirrors the rest of the entity CLI:
      - exit 1 on manifest parse error / missing file / unsupported
        version
      - exit 2 on URL-source mismatch / unwritable store path
      - exit 0 on successful run (even if some models were skipped —
        skips are part of normal flow, not user error)

    `_source_factory` is a documented private test seam: a callable
    taking a URL and returning a `DataSource`. Production callers
    leave it `None` (uses `PostgresDataSource`); CLI tests inject a
    fake to avoid a real Postgres dependency.
    """
    source_url = _resolve_url_source(positional=positional_url, url_env=url_env)
    if source_url is None:
        return 2
    if _resolve_url(source_url) is None:
        return 2
    source_id = _make_source_id(source_url)

    try:
        manifest = parse_dbt_manifest(Path(manifest_path))
    except DbtManifestParseError as exc:
        _entity_error(str(exc))
        return 1

    factory = _source_factory or (lambda url: PostgresDataSource(url))
    metric_summary: tuple[int, tuple] | None = None
    try:
        with SQLiteStore(store_path) as store, factory(source_url) as source:
            plan = plan_dbt_import(manifest, source, store, source_connection_id=source_id)
            if dry_run:
                result = None
            else:
                result = apply_dbt_import_plan(plan, store, source_connection_id=source_id)
            if include_metrics:
                metric_summary = _apply_dbt_metrics(
                    manifest_path=Path(manifest_path),
                    plan=plan,
                    apply_result=result,
                    store=store,
                    source_connection_id=source_id,
                    dry_run=dry_run,
                )
    except OperationalError as exc:
        # Postgres connection failure (wrong host, bad password,
        # timeout) — same handler shape as `_cmd_index` / `_cmd_serve`
        # / `_cmd_mine_queries` for symmetry across Postgres-touching
        # commands.
        _render_guided(postgres_operational_error(exc, url_hint=source_url))
        return 2
    except OSError as e:
        _render_guided(store_path_unwritable(store_path, e))
        return 2

    _render_import_dbt_summary(plan, result=result, dry_run=dry_run)
    _render_import_dbt_breadcrumbs(plan, result=result)
    if metric_summary is not None:
        _render_dbt_metric_summary(metric_summary, dry_run=dry_run)
    if report_path is not None:
        try:
            _write_import_dbt_report(plan, result=result, path=Path(report_path))
        except OSError as exc:
            # The write phase has already committed to the store at
            # this point. Surface the report failure on stderr but
            # return exit 2 so CI can distinguish "report missing"
            # from "import failed."
            print(
                f"error: could not write report to {report_path!r}: {exc}",
                file=sys.stderr,
            )
            return 2
    # Skipped models are part of normal flow (exit 0); WRITE failures
    # are not — the planner classified the entity as writable but the
    # store rejected it, so a CI consumer checking exit codes needs to
    # know a planned entity is absent from the store.
    if result is not None and result.write_failures:
        return 1
    # If --include-metrics was requested but the metric-import phase
    # errored out at the parse layer (manifest schema-version, JSON
    # decode), `metric_summary` is None. The error is already on
    # stderr; surface it as exit 1 so CI doesn't see a green run.
    # NB: in practice the entity parser rejects malformed manifests
    # BEFORE the metric parser sees them — both rely on the same
    # underlying JSON. This branch is reachable only when the entity
    # parser is lenient about a shape the metric parser refuses
    # (e.g., entity import succeeds on a v12-shaped JSON whose
    # `dbt_schema_version` field is hand-tampered to be < v11).
    if (
        include_metrics and metric_summary is None
    ):  # pragma: no cover — entity-parser-rejects-first invariant
        return 1
    return 0


def _apply_dbt_metrics(
    *,
    manifest_path: Path,
    plan: DbtImportPlan,
    apply_result: DbtImportResult | None,
    store: SQLiteStore,
    source_connection_id: str,
    dry_run: bool,
) -> tuple[int, tuple[DbtMetricSkip, ...]] | None:
    """Run the dbt-metric import alongside the entity import.

    Returns `(applied_count, skipped)` on success, `None` if metric
    import was attempted but errored out at the parse layer (the
    error is already printed to stderr).

    The set of "imported entity names" is the union of the plan's
    write buckets — any entity that exists in the store after this
    run, whether newly added, updated, or ownership-transferred.
    Skipped entities aren't included.
    """
    imported_entity_names: set[str] = set()
    for imported in plan.to_add:
        imported_entity_names.add(imported.entity.name)
    for imported in plan.to_update:
        imported_entity_names.add(imported.entity.name)
    for imported, _prev_origin in plan.to_take_ownership:
        imported_entity_names.add(imported.entity.name)

    try:
        metrics, skipped = parse_dbt_metrics(
            manifest_path, imported_entity_names=imported_entity_names
        )
    except DbtMetricImportError as exc:
        print(f"error: dbt metric import failed: {exc}", file=sys.stderr)
        return None

    if dry_run:
        # Dry-run mode skips the actual `write_metric` calls; we still
        # surface the count + skip reasons so the operator sees what
        # would happen on a real apply.
        return len(metrics), skipped

    applied_count = 0
    failures: list[tuple[str, str]] = []
    for metric in metrics:
        try:
            store.write_metric(metric, source_connection_id=source_connection_id)
            applied_count += 1
        except DbtOwnedMetricError as exc:  # pragma: no cover — the importer writes origin=dbt_import, so dbt_import→dbt_import is the idempotent path and the guard can't fire from this code path
            failures.append((metric.name, f"dbt-owned guard: {exc}"))
        except sqlite3.IntegrityError as exc:  # pragma: no cover — entity-import-first invariant makes FK violation here a store-corruption / race-only path
            # FK violation — should not happen because the entity
            # import ran first and the `parse_dbt_metrics`
            # `anchor_entity_not_imported` skip catches the gap.
            failures.append((metric.name, f"anchor entity FK violation: {exc}"))
        except Exception as exc:  # pragma: no cover — defense-in-depth catch for unanticipated store / library failures
            # Unexpected exception — surface type info so a real bug
            # is distinguishable from FK/dbt-guard at triage time.
            failures.append((metric.name, f"{type(exc).__name__}: {exc}"))
    for metric_name, message in failures:
        print(
            f"error: failed to write dbt metric {metric_name!r}: {message}",
            file=sys.stderr,
        )
    return applied_count, skipped


def _render_dbt_metric_summary(
    summary: tuple[int, tuple[DbtMetricSkip, ...]],
    *,
    dry_run: bool,
) -> None:
    """Print the metric-import portion of the end-of-run breadcrumb.

    Skips are bucketed by reason so the operator can see (at a glance)
    why each metric was rejected.
    """
    applied, skipped = summary
    verb = "would import" if dry_run else "imported"
    print(f"dbt metrics: {verb} {applied}, skipped {len(skipped)}")
    if skipped:
        # Group by reason for the breadcrumb.
        by_reason: dict[str, list[str]] = {}
        for skip in skipped:
            by_reason.setdefault(skip.reason, []).append(skip.metric_name)
        for reason, names in sorted(by_reason.items()):
            preview = ", ".join(names[:5])
            extra = f", +{len(names) - 5} more" if len(names) > 5 else ""
            print(f"  skipped[{reason}]: {preview}{extra}")


def _render_import_dbt_summary(
    plan: DbtImportPlan, *, result: DbtImportResult | None, dry_run: bool
) -> None:
    """Print a stdout summary of the plan + result.

    Keeps the surface plain text (rich panels are reserved for
    structural errors per `_render_guided`). The summary names the
    project + bucket counts; orphans + skips are rendered on stderr
    by `_render_import_dbt_breadcrumbs`.
    """
    mode = "dry-run (no writes)" if dry_run else "applied"
    write_failures = len(result.write_failures) if result is not None else 0
    written = (
        len(plan.to_add) + len(plan.to_update) + len(plan.to_take_ownership) - write_failures
        if result is not None
        else 0
    )
    lines = [
        f"dbt import: {plan.dbt_project_name} ({mode})",
        f"  added: {len(plan.to_add)}",
        f"  updated: {len(plan.to_update)}",
        f"  ownership-transferred: {len(plan.to_take_ownership)}",
        f"  orphans: {len(plan.orphans)}",
        f"  skipped: {len(plan.skipped)}",
    ]
    if result is not None:
        lines.append(f"  written: {written}")
        if write_failures:
            lines.append(f"  write_failures: {write_failures}")
    # Single dict-driven loop so a future field added to `DbtSkipCounts`
    # only needs editing here, not in two parallel places.
    skip_fields = {
        "metrics": plan.skip_counts.metrics,
        "snapshots": plan.skip_counts.snapshots,
        "seeds": plan.skip_counts.seeds,
        "analyses": plan.skip_counts.analyses,
        "operations": plan.skip_counts.operations,
        "exposures": plan.skip_counts.exposures,
        "other": plan.skip_counts.other,
    }
    non_zero = [f"{name}={count}" for name, count in skip_fields.items() if count]
    if non_zero:
        lines.append(f"  non-model resources deferred: {', '.join(non_zero)}")
    print("\n".join(lines))


def _render_import_dbt_breadcrumbs(plan: DbtImportPlan, *, result: DbtImportResult | None) -> None:
    """Print per-model orphan + skip + write-failure breadcrumbs to stderr.

    Orphans, skipped models, and write failures are bucketed by the
    driver. Each line names the item so the user can act on it
    without re-running with `--dry-run`.
    """
    for name in plan.orphans:
        print(
            f"warning: entity {name!r} exists in the store with origin=dbt_import "
            "but is no longer in the manifest; left untouched (no auto-delete at v1).",
            file=sys.stderr,
        )
    for skip in plan.skipped:
        print(
            f"warning: skipped dbt model {skip.dbt_unique_id!r} "
            f"(reason={skip.reason}): {skip.message}",
            file=sys.stderr,
        )
    if result is not None:
        for failure in result.write_failures:
            print(
                f"error: write failed for entity {failure.entity_name!r}: {failure.message}",
                file=sys.stderr,
            )


def _write_import_dbt_report(
    plan: DbtImportPlan, *, result: DbtImportResult | None, path: Path
) -> None:
    """Write a JSON report of the plan + apply result.

    Shape is intentionally CI-friendly: counts at the top level,
    per-model detail in nested arrays. Same field names as the
    Python dataclasses so a CI consumer that already knows the
    driver shapes can read it without translation.
    """
    report = {
        "dbt_project_name": plan.dbt_project_name,
        "counts": {
            "to_add": len(plan.to_add),
            "to_update": len(plan.to_update),
            "to_take_ownership": len(plan.to_take_ownership),
            "orphans": len(plan.orphans),
            "skipped": len(plan.skipped),
        },
        "to_add": [e.entity.name for e in plan.to_add],
        "to_update": [e.entity.name for e in plan.to_update],
        "to_take_ownership": [
            {"name": env.entity.name, "previous_origin": prior}
            for env, prior in plan.to_take_ownership
        ],
        "orphans": list(plan.orphans),
        "skipped": [
            {
                "dbt_unique_id": s.dbt_unique_id,
                "reason": s.reason,
                "message": s.message,
            }
            for s in plan.skipped
        ],
        "skip_counts": {
            "metrics": plan.skip_counts.metrics,
            "snapshots": plan.skip_counts.snapshots,
            "seeds": plan.skip_counts.seeds,
            "analyses": plan.skip_counts.analyses,
            "operations": plan.skip_counts.operations,
            "exposures": plan.skip_counts.exposures,
            "other": plan.skip_counts.other,
        },
    }
    if result is not None:
        report["write_failures"] = [
            {"entity_name": f.entity_name, "message": f.message} for f in result.write_failures
        ]
    path.write_text(json.dumps(report, indent=2))


def _cmd_joins_suggest(
    *,
    positional_url: str | None,
    url_env: str | None,
    store_path: str,
    dry_run: bool,
    out_dir: str | None,
    apply: bool,
    top_k: int | None,
    report_path: str | None,
) -> int:
    """Mine canonical-join candidates from FK + query-log evidence.

    Three output modes — exactly one must be true (argparse enforces
    via `add_mutually_exclusive_group(required=True)`):

      - `dry_run`: print ranked candidates to stdout, no writes
      - `out_dir`: write one `.yaml` file per candidate to a directory
        (each file is `joins apply`-ready)
      - `apply`: write candidates straight to the store with
        `origin='suggested'`

    `--report PATH` works alongside any mode — emits a JSON report
    with bucket counts + structural cycle analysis (per the design).

    Exit codes:
      0: success
      1: user-input class (parse error in store, FK violation)
      2: structural (missing URL, unwritable store, unwritable report)
    """
    source_url = _resolve_url_source(positional=positional_url, url_env=url_env)
    if source_url is None:
        return 2
    if _resolve_url(source_url) is None:  # pragma: no cover — defensive
        return 2
    source_id = _make_source_id(source_url)

    try:
        with SQLiteStore(store_path) as store:
            candidates = suggest_canonical_joins(store=store, source_connection_id=source_id)
            if top_k is not None:
                candidates = candidates[:top_k]

            apply_summary: dict[str, int] = {"written": 0, "skipped": 0}
            apply_failures: list[tuple[str, str]] = []
            if apply:
                for candidate in candidates:
                    try:
                        store.write_canonical_join(
                            candidate.to_canonical_join(),
                            source_connection_id=source_id,
                        )
                        apply_summary["written"] += 1
                    except sqlite3.IntegrityError as exc:  # pragma: no cover — suggester drops entity-less candidates upstream; this catches a TOCTOU entity-delete race
                        apply_summary["skipped"] += 1
                        apply_failures.append((candidate.name, str(exc)))

            existing_joins = store.list_canonical_joins(source_connection_id=source_id)
            # Pass the full entity-name set so the cycle report's
            # `isolated_entities` field reflects real isolation (entities
            # that exist but don't appear in any canonical join), rather
            # than the always-empty set the analyser computes from the
            # join list alone.
            all_entity_names = {e.name for e in store.list_entities(source_connection_id=source_id)}
            cycle_report = detect_cycles_in_join_graph(
                existing_joins, all_entity_names=all_entity_names
            )

    except OSError as e:  # pragma: no cover — store-path-unwritable path is covered by `joins list` OSError test below
        _render_guided(store_path_unwritable(store_path, e))
        return 2

    # Render per mode.
    if dry_run:
        _render_joins_suggest_dry_run(candidates)
    elif out_dir is not None:
        if not candidates:
            # Mirror the dry-run diagnostic — without this guard the
            # `--out-dir` path silently creates an empty directory
            # with a `_suggestion_metadata.json` containing `{}`,
            # leaving the operator with no signal about why zero
            # files landed.
            print(
                "(no canonical-join candidates surfaced; check that "
                "entities are defined and FK / query-log evidence "
                "exists — `--out-dir` not written)",
                file=sys.stderr,
            )
        else:
            try:
                _write_joins_out_dir(candidates, out_dir=out_dir)
            except OSError as e:
                # Partial write: the loop in `_write_joins_out_dir`
                # writes files one-at-a-time and may leave some on
                # disk before raising. Flag the inconsistency so the
                # operator doesn't run `joins apply` on a half-written
                # directory.
                print(
                    f"error: cannot write candidates to {out_dir!r} "
                    f"(directory may contain a partial set — DO NOT "
                    f"run `joins apply` on it): {e}",
                    file=sys.stderr,
                )
                return 2
    elif (
        apply
    ):  # pragma: no branch — argparse mutex group enforces exactly one of (dry_run, out_dir, apply)
        _render_joins_apply_summary(
            candidates, apply_summary=apply_summary, failures=apply_failures
        )

    if report_path is not None:
        try:
            _write_joins_suggest_report(
                Path(report_path),
                candidates=candidates,
                cycle_report=cycle_report,
                apply_summary=apply_summary if apply else None,
            )
        except OSError as e:
            print(
                f"error: cannot write report to {report_path!r}: {e}",
                file=sys.stderr,
            )
            return 2

    # Cycles are NOT a refusal at v1 (per the design) — surface as a
    # stderr note so the operator sees them without forcing a decision.
    if cycle_report.cycles:
        print(
            f"note: {len(cycle_report.cycles)} cycle(s) detected in the "
            f"canonical-join graph (legal but worth reviewing). Run "
            f"`schemabrain joins suggest --report PATH` for details.",
            file=sys.stderr,
        )

    return 1 if apply_failures else 0


def _cmd_joins_apply(
    *,
    yaml_path: str,
    positional_url: str | None,
    url_env: str | None,
    store_path: str,
) -> int:
    """Load canonical-join YAML files into the local store.

    `yaml_path` may be a single file OR a directory. Directory mode
    loads every `*.yaml`/`*.yml` in the immediate children, applies
    each, and surfaces a summary. A parse/FK error in one file does
    NOT block the rest — failures aggregate into the summary; exit
    code is 1 if any file failed.

    Exit codes:
      0: every file applied cleanly
      1: at least one file failed (parse / FK violation / etc.)
      2: structural (URL missing, unwritable store)
    """
    source_url = _resolve_url_source(positional=positional_url, url_env=url_env)
    if source_url is None:
        return 2
    if _resolve_url(source_url) is None:  # pragma: no cover — defensive
        return 2
    source_id = _make_source_id(source_url)

    path = Path(yaml_path)
    if path.is_dir():
        yaml_files = sorted(
            p for p in path.iterdir() if p.is_file() and p.suffix.lower() in (".yaml", ".yml")
        )
        if not yaml_files:
            print(
                f"error: no `.yaml`/`.yml` files found in directory {yaml_path!r}",
                file=sys.stderr,
            )
            return 1
    elif path.is_file():
        yaml_files = [path]
    else:
        print(
            f"error: {yaml_path!r} is not a file or directory",
            file=sys.stderr,
        )
        return 1

    applied: list[str] = []
    failures: list[tuple[str, str]] = []

    try:
        with SQLiteStore(store_path) as store:
            for yaml_file in yaml_files:
                try:
                    join = parse_canonical_join_yaml_file(yaml_file)
                except (
                    FileNotFoundError,
                    IsADirectoryError,
                ) as exc:  # pragma: no cover — directory listing already filters non-files; race-only path
                    failures.append((str(yaml_file), str(exc)))
                    continue
                except CanonicalJoinParseError as exc:
                    failures.append((str(yaml_file), str(exc)))
                    continue

                # Force origin to "manual" for the apply path — even if
                # the YAML carries origin: suggested. The hand-author
                # who runs `joins apply` is overriding any prior
                # suggestion provenance with explicit confirmation.
                manual_join = dataclasses.replace(join, origin="manual")
                try:
                    store.write_canonical_join(manual_join, source_connection_id=source_id)
                    applied.append(manual_join.name)
                except sqlite3.IntegrityError as exc:
                    # The likely cause is the FK to `entities` failing
                    # because one endpoint isn't defined. The unlikely
                    # case is a CHECK constraint violation (e.g.,
                    # invalid `origin` value) — but the YAML parser
                    # rejects those before this point. Include the
                    # raw SQLite error so an operator can distinguish
                    # FK violation from CHECK violation if a future
                    # code path bypasses the YAML guard.
                    failures.append(
                        (
                            str(yaml_file),
                            f"entity {manual_join.source_entity!r} or "
                            f"{manual_join.target_entity!r} not present "
                            f"in the store for this source (or a database "
                            f"constraint was violated: {exc}). Run "
                            f"`schemabrain entities apply` first.",
                        )
                    )
    except OSError as e:  # pragma: no cover — store-path-unwritable variant covered via `joins list` OSError test
        _render_guided(store_path_unwritable(store_path, e))
        return 2

    for name in applied:
        print(f"applied canonical join: {name}")
    for file_str, message in failures:
        print(f"error in {file_str}: {message}", file=sys.stderr)

    return 1 if failures else 0


def _cmd_joins_list(
    *,
    store_path: str,
    positional_url: str | None,
    url_env: str | None,
) -> int:
    """List canonical joins in the store, pretty-printed.

    With `--source` / `--url-env` filter to one source. Without
    either, lists every join across every source. The verification
    path after `joins apply`.

    Exit codes:
      0: success (empty list is success, not an error)
      2: structural (unwritable store path or URL-source mismatch)
    """
    source_id: str | None = None
    if positional_url is not None or url_env is not None:
        source_url = _resolve_url_source(positional=positional_url, url_env=url_env)
        if source_url is None:
            return 2
        # _resolve_url defensively re-validates; never None when
        # _resolve_url_source returned non-None.
        if _resolve_url(source_url) is None:  # pragma: no cover
            return 2  # pragma: no cover
        source_id = _make_source_id(source_url)

    try:
        with SQLiteStore(store_path) as store:
            joins = store.list_canonical_joins(source_connection_id=source_id)
    except OSError as e:
        _render_guided(store_path_unwritable(store_path, e))
        return 2

    if not joins:
        print("(no canonical joins in the store)")
        return 0

    for join in joins:
        on_summary = ", ".join(f"{p.source_column} ↔ {p.target_column}" for p in join.on)
        print(
            f"{join.name}  "
            f"{join.source_entity} → {join.target_entity}  "
            f"[{on_summary}]  origin={join.origin}"
        )
    return 0


# ----- metrics CLI commands --------------------------------------------------
#
# Mirrors `_cmd_entities_apply` / `_cmd_joins_apply`. Single-file + directory
# modes for apply; the dbt-owned-metric guard surfaces as a user-facing
# exit-1 message naming the metric.


def _cmd_metrics_apply(
    *,
    yaml_path: str,
    positional_url: str | None,
    url_env: str | None,
    store_path: str,
) -> int:
    """Load metric YAML files into the local store.

    `yaml_path` may be a single file OR a directory. Directory mode
    loads every `*.yaml`/`*.yml` in the immediate children, applies
    each, and surfaces a summary. A parse/FK/dbt-guard error in one
    file does NOT block the rest — failures aggregate into the
    summary; exit code is 1 if any file failed.

    Exit codes:
      0: every file applied cleanly
      1: at least one file failed (parse / FK violation / dbt-owned)
      2: structural (URL missing, unwritable store)
    """
    source_url = _resolve_url_source(positional=positional_url, url_env=url_env)
    if source_url is None:
        return 2
    if _resolve_url(source_url) is None:  # pragma: no cover — defensive
        return 2
    source_id = _make_source_id(source_url)

    path = Path(yaml_path)
    if path.is_dir():
        yaml_files = sorted(
            p for p in path.iterdir() if p.is_file() and p.suffix.lower() in (".yaml", ".yml")
        )
        if not yaml_files:
            print(
                f"error: no `.yaml`/`.yml` files found in directory {yaml_path!r}",
                file=sys.stderr,
            )
            return 1
    elif path.is_file():
        yaml_files = [path]
    else:
        print(
            f"error: {yaml_path!r} is not a file or directory",
            file=sys.stderr,
        )
        return 1

    applied: list[str] = []
    failures: list[tuple[str, str]] = []

    try:
        with SQLiteStore(store_path) as store:
            for yaml_file in yaml_files:
                try:
                    metric = parse_metric_yaml_file(yaml_file)
                except (
                    FileNotFoundError,
                    IsADirectoryError,
                ) as exc:  # pragma: no cover — directory listing already filters non-files; race-only path
                    failures.append((str(yaml_file), str(exc)))
                    continue
                except MetricYamlError as exc:
                    failures.append((str(yaml_file), str(exc)))
                    continue

                # Force origin to "manual" for the apply path — even if
                # the YAML carries origin: suggested. The hand-author
                # who runs `metrics apply` is overriding any prior
                # suggestion provenance with explicit confirmation.
                # `origin: dbt_import` would have been refused at YAML
                # parse-time (per `MetricYamlError`-reservation), so
                # the only surviving origins here are manual/suggested.
                manual_metric = dataclasses.replace(metric, origin="manual")
                try:
                    store.write_metric(manual_metric, source_connection_id=source_id)
                    applied.append(manual_metric.name)
                except DbtOwnedMetricError as exc:
                    failures.append((str(yaml_file), str(exc)))
                except sqlite3.IntegrityError:
                    # FK violation — the anchor entity doesn't exist
                    # for this source. CHECK violations are ruled out
                    # by the YAML parser + dataclass invariants. The
                    # message intentionally drops the raw SQLite text
                    # ("FOREIGN KEY constraint failed") so the user
                    # sees the actionable fix, not the database lingo.
                    failures.append(
                        (
                            str(yaml_file),
                            f"anchor entity {manual_metric.entity!r} is not "
                            f"present in the store for this source. Run "
                            f"`schemabrain entities apply` first.",
                        )
                    )
    except OSError as e:  # pragma: no cover — store-path-unwritable variant covered via `metrics list` OSError test
        _render_guided(store_path_unwritable(store_path, e))
        return 2

    for name in applied:
        print(f"applied metric: {name}")
    for file_str, message in failures:
        print(f"error in {file_str}: {message}", file=sys.stderr)

    return 1 if failures else 0


def _cmd_metrics_list(
    *,
    store_path: str,
    positional_url: str | None,
    url_env: str | None,
) -> int:
    """List metrics in the store, pretty-printed.

    With `--source` / `--url-env` filter to one source. Without
    either, lists every metric across every source. The verification
    path after `metrics apply`.

    Exit codes:
      0: success (empty list is success, not an error)
      2: structural (unwritable store path or URL-source mismatch)
    """
    source_id: str | None = None
    if positional_url is not None or url_env is not None:
        source_url = _resolve_url_source(positional=positional_url, url_env=url_env)
        if source_url is None:
            return 2
        if _resolve_url(source_url) is None:  # pragma: no cover — defensive
            return 2  # pragma: no cover
        source_id = _make_source_id(source_url)

    try:
        with SQLiteStore(store_path) as store:
            metrics = store.list_metrics(source_connection_id=source_id)
    except OSError as e:
        _render_guided(store_path_unwritable(store_path, e))
        return 2
    except ValueError as exc:
        # `_row_to_metric` re-runs the dataclass invariants — a corrupt
        # row (e.g., hand-edited `time_grains` out of canonical order,
        # invalid grain string) surfaces as a plain `ValueError` from
        # the constructor. Wrap with store-path context so the user
        # sees "your store file is corrupt, here's how" instead of a
        # bare traceback.
        print(
            f"error: failed to read metrics from {store_path!r}: store appears corrupt ({exc})",
            file=sys.stderr,
        )
        return 2

    if not metrics:
        print("(no metrics in the store)")
        return 0

    for metric in metrics:
        grains = ",".join(metric.time_grains) if metric.time_grains else "(non-temporal)"
        time_dim = metric.time_dimension or "—"
        print(
            f"{metric.name}  "
            f"entity={metric.entity}  "
            f"{metric.measure.agg}({metric.measure.column})  "
            f"time_dim={time_dim}  "
            f"grains={grains}  "
            f"origin={metric.origin}"
        )
    return 0


def _render_joins_suggest_dry_run(
    candidates: list[JoinCandidate],
) -> None:
    """Print one candidate per stanza to stdout — paste-clean format
    that survives shell pipes.

    The output is a sequence of YAML-like blocks (one per candidate)
    with provenance fields prefixed `# ` so the body remains
    `joins apply`-compatible if a user dumps the output to a file.
    """
    if not candidates:
        print(
            "(no canonical-join candidates surfaced; check that "
            "entities are defined and FK / query-log evidence exists)"
        )
        return
    for candidate in candidates:
        _print_candidate_yaml(candidate)


def _print_candidate_yaml(candidate: JoinCandidate) -> None:
    """Emit one candidate as a YAML stanza with provenance comments."""
    print("---")
    print(f"# confidence: {candidate.confidence}")
    print(f"# evidence: {list(candidate.evidence)}")
    if candidate.fk_name is not None:
        print(f"# fk_name: {candidate.fk_name}")
    print(f"# query_log_frequency: {candidate.query_log_frequency}")
    print(f"# rationale: {candidate.rationale}")
    print("version: 1")
    print(f"name: {candidate.name}")
    print('description: ""')
    print(f"source_entity: {candidate.source_entity}")
    print(f"target_entity: {candidate.target_entity}")
    print('"on":')  # quoted to dodge YAML 1.1 bool coercion when re-parsed
    for pair in candidate.on:
        print(f"  - source: {pair.source_column}")
        print(f"    target: {pair.target_column}")


def _write_joins_out_dir(
    candidates: list[JoinCandidate],
    *,
    out_dir: str,
) -> None:
    """Write one YAML file per candidate to `out_dir`.

    Filenames are `<candidate_name>.yaml`. Each file is
    `joins apply`-ready (clean YAML body); the provenance metadata
    rides in a sibling `_suggestion_metadata.json` that the apply
    path doesn't read but a reviewer can.

    Raises `OSError` if the directory can't be created or any file
    can't be written.
    """
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    metadata: dict[str, dict[str, object]] = {}
    for candidate in candidates:
        # Clean YAML — no provenance comments (those rode in --dry-run
        # output). The body is what `joins apply` expects.
        file_path = out_path / f"{candidate.name}.yaml"
        body_lines = [
            "version: 1",
            f"name: {candidate.name}",
            'description: ""',
            f"source_entity: {candidate.source_entity}",
            f"target_entity: {candidate.target_entity}",
            '"on":',
        ]
        for pair in candidate.on:
            body_lines.append(f"  - source: {pair.source_column}")
            body_lines.append(f"    target: {pair.target_column}")
        file_path.write_text("\n".join(body_lines) + "\n", encoding="utf-8")
        metadata[candidate.name] = {
            "confidence": candidate.confidence,
            "evidence": list(candidate.evidence),
            "fk_name": candidate.fk_name,
            "query_log_frequency": candidate.query_log_frequency,
            "rationale": candidate.rationale,
        }
    metadata_path = out_path / "_suggestion_metadata.json"
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")


def _render_joins_apply_summary(
    candidates: list[JoinCandidate],
    *,
    apply_summary: dict[str, int],
    failures: list[tuple[str, str]],
) -> None:
    """Print a terse summary after `joins suggest --apply`."""
    print(
        f"applied {apply_summary['written']} canonical join(s) "
        f"({apply_summary['skipped']} skipped) of {len(candidates)} candidate(s)"
    )
    for (
        name,
        message,
    ) in failures:  # pragma: no cover — suggester drops entity-less candidates upstream; this loop body only fires under TOCTOU race
        print(f"  skipped {name}: {message}", file=sys.stderr)


def _write_joins_suggest_report(
    path: Path,
    *,
    candidates: list[JoinCandidate],
    cycle_report: JoinGraphReport,
    apply_summary: dict[str, int] | None,
) -> None:
    """Write a JSON report covering candidates + cycles + apply summary."""
    report: dict[str, object] = {
        "candidates": [
            {
                "name": c.name,
                "source_entity": c.source_entity,
                "target_entity": c.target_entity,
                "on": [{"source": p.source_column, "target": p.target_column} for p in c.on],
                "confidence": c.confidence,
                "evidence": list(c.evidence),
                "fk_name": c.fk_name,
                "query_log_frequency": c.query_log_frequency,
                "rationale": c.rationale,
            }
            for c in candidates
        ],
        "graph_analysis": {
            "cycles": [list(c) for c in cycle_report.cycles],
            "isolated_entities": list(cycle_report.isolated_entities),
            "max_path_length": cycle_report.max_path_length,
        },
    }
    if apply_summary is not None:
        report["apply_summary"] = dict(apply_summary)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")


def _cmd_doctor(
    *,
    positional_url: str | None,
    url_env: str | None,
    store_path: str,
    host: str,
    json_output: bool,
) -> int:
    """Run `schemabrain doctor` and render the result.

    Source URL is OPTIONAL — when both `positional_url` and `url_env`
    are None, source-related checks are skipped (config-only mode).
    When one is supplied, the standard `_resolve_url_source` helper
    refuses on conflict or unset env var with the same guided errors
    every other source-using subcommand emits, returning exit code 2.

    Exit code semantics:
      - 0: doctor ran; no `fail` outcomes
      - 1: doctor ran; at least one `fail` outcome
      - 2: operational refusal before doctor could run (e.g. --source
        + --url-env conflict, --url-env names an unset variable)
    """
    from schemabrain.setup.doctor_flow import doctor, render_doctor, render_doctor_json

    source_url: str | None = None
    if positional_url is not None or url_env is not None:
        source_url = _resolve_url_source(positional=positional_url, url_env=url_env)
        if source_url is None:
            # Guided error already rendered to stderr.
            return 2
    result = doctor(
        source_url=source_url,
        store_path=Path(store_path),
        host=host,  # type: ignore[arg-type]
    )
    if json_output:
        # JSON to stdout — clean pipe target.
        sys.stdout.write(render_doctor_json(result))
    else:
        # Human-readable to stderr so users can pipe stdout cleanly
        # in mixed-output scripts.
        render_doctor(result, console=_stderr_console())
    return result.exit_code


def _cmd_init(
    *,
    positional_url: str | None,
    url_env: str | None,
    store_path: str,
    host: str,
    env_var: str,
    skip_index: bool,
    assume_yes: bool,
    print_only: bool,
) -> int:
    """Run `schemabrain init` and render the outcome.

    Exit codes:
      - 0: snippet written / shell-out succeeded / printed (manual)
      - 1: claude-code shell-out failed (the snippet IS still
        printed so the user can fall back to running the command
        themselves)
      - 2: operational refusal (URL flag conflict, source unreachable,
        store empty without --skip-index, host config dir missing, etc.)

    `--print-only` is an alias for `--host manual` — when either is
    set, init never writes; the snippet renders to stdout.
    """
    from schemabrain.setup.init_flow import InitRefusal, init

    effective_host = "manual" if print_only else host
    source_url = _resolve_url_source(positional=positional_url, url_env=url_env)
    if source_url is None:
        # _resolve_url_source already rendered a guided error.
        return 2
    effective_skip_index = skip_index
    effective_assume_yes = assume_yes
    interactive = _stderr_is_interactive_tty()
    while True:
        try:
            result = init(
                source_url=source_url,
                store_path=Path(store_path),
                host=effective_host,  # type: ignore[arg-type]
                env_var_name=env_var,
                skip_index=effective_skip_index,
                assume_yes=effective_assume_yes,
            )
        except InitRefusal as refusal:
            # Two refusal kinds have an interactive recovery: the
            # entry-exists case (user can confirm overwrite) and the
            # empty-store case (user can confirm "skip indexing for
            # now, I'll do it later"). Anything else surfaces as a
            # plain guided error and exits 2.
            if interactive and refusal.error.kind == "init_entry_exists":
                if _prompt_yes_no(
                    "Overwrite the existing schemabrain entry?",
                    default=False,
                ):
                    effective_assume_yes = True
                    continue
                _stderr_console().print("[yellow]cancelled[/] no changes made.")
                return 0
            if interactive and refusal.error.kind == "init_store_empty":
                if _prompt_yes_no(
                    "Continue without indexing now? "
                    "(you'll need to run `schemabrain index` before agents can query)",
                    default=False,
                ):
                    effective_skip_index = True
                    continue
                _render_guided(refusal.error)
                return 2
            _render_guided(refusal.error)
            return 2
        break
    _render_init_result(result)
    if result.state == "shell_out_failed":
        return 1
    return 0


def _stderr_is_interactive_tty() -> bool:
    """True iff init can safely prompt — both stdin AND stderr are TTYs.

    Wrapped so tests can monkeypatch this one function instead of
    patching `sys.stdin.isatty` and `sys.stderr.isatty` separately.
    """
    return sys.stdin.isatty() and sys.stderr.isatty()


def _prompt_yes_no(question: str, *, default: bool) -> bool:
    """Ask the user a yes/no question via rich.prompt.Confirm.

    Lazy-imported so the cli's import-cost path isn't affected
    when no subcommand needs interactive input.
    """
    from rich.prompt import Confirm

    return Confirm.ask(question, default=default, console=_stderr_console())


def _redact_env_args(cmd: tuple[str, ...]) -> list[str]:
    """Return a copy of `cmd` with each `-e KEY=VALUE` value redacted.

    Used when printing a `claude mcp add` argv to stderr after the
    shell-out failed. The KEY=VALUE tokens carry the live DB URL
    (including any password) — printing them verbatim would land
    credentials on stderr / terminal scrollback / screen recordings,
    which the project keeps out of argv-visible surfaces. Renders
    as `KEY=<redacted>`.
    """
    out: list[str] = []
    skip_next = False
    for token in cmd:
        if skip_next:
            key, sep, _value = token.partition("=")
            out.append(f"{key}{sep}<redacted>" if sep else token)
            skip_next = False
        elif token == "-e":
            out.append(token)
            skip_next = True
        else:
            out.append(token)
    return out


def _render_init_result(result: object) -> None:
    """Render the outcome of a successful init run.

    Caller is `_cmd_init`, which has already validated the type.
    Typed as `object` here so the cli module doesn't import the
    init_flow types at parse time (preserves the lazy-init-flow-
    import discipline the rest of `_cmd_init` follows).
    """
    from schemabrain.setup.init_flow import InitResult

    if not isinstance(result, InitResult):
        raise TypeError(f"_render_init_result expected InitResult, got {type(result).__name__}")
    console = _stderr_console()
    if result.state == "written":
        console.print(f"[green]✓[/] wrote schemabrain entry to {result.config_path}")
        if result.backup_made:
            backup = (
                result.config_path.parent / (result.config_path.name + ".bak")
                if result.config_path is not None
                else None
            )
            console.print(f"  [dim]backup:[/] {backup}")
        console.print()
        if result.skip_index:
            # Store was empty + user opted in (--skip-index or interactive
            # acceptance); the "list entities" question would return nothing
            # without an index run first.
            console.print(
                "  [dim]Before querying:[/] run "
                "`schemabrain index --url-env $VAR --store-path $PATH`"
            )
            console.print(
                "  [dim]Then:[/] restart Claude Desktop and ask:  "
                '"list the entities Schema Brain knows about"'
            )
        else:
            console.print(
                "  [dim]Next:[/] restart Claude Desktop, then ask:  "
                '"list the entities Schema Brain knows about"'
            )
    elif result.state == "unchanged":
        console.print(
            f"[green]✓[/] schemabrain entry already configured in {result.config_path}; no changes"
        )
    elif result.state == "shell_out_succeeded":
        console.print("[green]✓[/] registered schemabrain with Claude Code")
        console.print()
        console.print(
            "  [dim]Next:[/] restart Claude Code, then ask:  "
            '"list the entities Schema Brain knows about"'
        )
    elif result.state == "shell_out_failed":
        console.print("[red]✗[/] `claude mcp add` failed; you can run it manually:")
        if result.shell_out_command:
            console.print()
            console.print("  " + " ".join(_redact_env_args(result.shell_out_command)))
            console.print()
            console.print(
                "  [dim]Note:[/] env values are redacted above. To re-run with real "
                "credentials, prefer `schemabrain init` (which reads them from your env)."
            )
        if result.shell_out_stderr:
            console.print(f"\n  [dim]stderr:[/] {result.shell_out_stderr}")
    else:
        # state == "printed_only" — manual / --print-only
        import json as _json

        # Snippet to stdout — the user wants a paste-ready JSON block,
        # so this lands on stdout not stderr.
        entry = {"mcpServers": {"schemabrain": result.snippet.to_mcp_entry()}}
        sys.stdout.write(_json.dumps(entry, indent=2))
        sys.stdout.write("\n")
        console.print()
        console.print(
            "  [dim]Common config paths:[/]\n"
            "    Claude Desktop (macOS):   ~/Library/Application Support/Claude/claude_desktop_config.json\n"
            "    Claude Desktop (Windows): %APPDATA%\\Claude\\claude_desktop_config.json\n"
            "    Cursor:                   ~/.cursor/mcp.json\n"
            "    Continue:                 ~/.continue/config.json\n"
            "    Windsurf:                 ~/.codeium/windsurf/mcp_config.json"
        )
        console.print()
        console.print(
            "  [dim]After saving the file, restart your host and ask:[/]  "
            '"list the entities Schema Brain knows about"'
        )


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
