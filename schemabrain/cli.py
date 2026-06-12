"""SchemaBrain CLI.

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
import contextlib
import dataclasses
import json
import os
import sqlite3
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final
from urllib.parse import urlparse

import sqlalchemy
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool

from schemabrain import __version__
from schemabrain._env import resolve_positive_float_env, resolve_positive_int_env
from schemabrain.connectors._url import safe_engine_url
from schemabrain.connectors.base import DataSource
from schemabrain.connectors.postgres import PostgresDataSource
from schemabrain.core.entity import Entity
from schemabrain.core.join import CanonicalJoin
from schemabrain.core.metric import DbtOwnedMetricError, Metric
from schemabrain.core.models import Table
from schemabrain.core.store import DbtOwnedEntityError, SchemaVersionMismatchError, SQLiteStore
from schemabrain.core.store_protocol import Store
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
    silent_rewrite_to_psycopg,
    store_path_unwritable,
    url_wrong_driver,
)
from schemabrain.errors_render import (
    cause_from_llm_error,
    classify_llm_failure,
    render_llm_failure,
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
from schemabrain.indexer import IndexReporter, IndexResult, NullReporter, dry_run_index, index
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
from schemabrain.metrics.suggest import (
    MetricCandidate,
    MetricSuggestionParseError,
    MetricSuggestionPipeline,
    MetricSuggestionResult,
)
from schemabrain.metrics.yaml_grammar import (
    MetricYamlError,
    parse_metric_yaml_file,
)
from schemabrain.mining.pipeline import mine_queries
from schemabrain.positioning import SHORT_DESCRIPTION
from schemabrain.profiler.postgres import PostgresProfiler

if TYPE_CHECKING:
    # Forward-only imports for type annotations on private helpers
    # whose runtime bodies still use lazy imports — keeps Rich and
    # the wizard module out of `cli.py`'s import graph for
    # subcommands that never enter the wizard renderer.
    from rich.table import Table
    from rich.text import Text

    from schemabrain.setup.wizard import WizardResult


_DEFAULT_STORE_PATH = "./schemabrain.db"
_DEFAULT_EVENTS_PATH = "~/.schemabrain/events.jsonl"
# Conventional location for the operator-editable pii_policy.yaml,
# sibling to the entities/ metrics/ joins/ subdirectories produced by
# `init --emit-yaml-dir`. `serve` reads `block:` from this path at
# startup unless `--pii-block` is explicitly passed; `apply` reads
# both `block:` and `column_overrides:` to populate the store.
_DEFAULT_POLICY_PATH = "./schemabrain/pii_policy.yaml"
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
# Module-level constants rather than locals so test fixtures can
# monkeypatch them to `1` for deterministic cap enforcement — under
# default concurrency, the per-task cap check races and a cap-trip
# test would need >= 9 columns to land deterministically.
#
# Operators can override at runtime via
# `SCHEMABRAIN_PIPELINE_DEFAULT_CONCURRENCY` /
# `SCHEMABRAIN_PIPELINE_CRYPTIC_CONCURRENCY`. The env-var resolution
# happens at `_cmd_index` call time (not module import) via
# `resolve_positive_int_env`, falling back to these constants as
# defaults. The two-layer resolution keeps both the test
# monkeypatching pattern and the operator-override pattern working
# at the same time.
_PIPELINE_DEFAULT_CONCURRENCY = 8
_PIPELINE_CRYPTIC_CONCURRENCY = 4
_PIPELINE_DEFAULT_CONCURRENCY_ENV = "SCHEMABRAIN_PIPELINE_DEFAULT_CONCURRENCY"
_PIPELINE_CRYPTIC_CONCURRENCY_ENV = "SCHEMABRAIN_PIPELINE_CRYPTIC_CONCURRENCY"

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
    # D4: load `.env` in CWD before any subcommand reads env vars.
    # Shell exports always win — `load_env_file_into_environ` only
    # sets keys NOT already present. Silent no-op if `.env` is
    # absent. We don't catch exceptions because the loader itself
    # is malformed-line-tolerant and the only failure modes are
    # filesystem I/O issues that the operator needs to see.
    from schemabrain.setup.env_file import load_env_file_into_environ

    load_env_file_into_environ(Path.cwd() / ".env")
    try:
        return _dispatch(argv)
    except (KeyboardInterrupt, EOFError):
        # Catch KeyboardInterrupt + EOFError at the entry point so
        # every subcommand gets the same clean exit-130 + "aborted."
        # on stderr, regardless of which interactive prompt or which
        # wizard stage the user was sitting in when they hit Ctrl-C
        # (or when stdin dropped). Without this, a Ctrl-C mid-wizard
        # would produce a raw Python traceback while the same Ctrl-C
        # at stage 0 produced a clean abort — inconsistency that
        # makes the UX feel broken. The catch is at main() rather
        # than per-
        # subcommand because the wizard's spinner-bearing context
        # managers handle their own cleanup via __exit__; main()'s
        # job is just to translate the signal into a clean exit code.
        print("\naborted.", file=sys.stderr)
        return 130


def _dispatch(argv: list[str] | None) -> int:
    """Parse args and route to the right subcommand handler.

    Split out of `main` so the top-level KeyboardInterrupt / EOFError
    catch lives in a focused wrapper. The dispatch body never catches
    those exceptions itself — it raises (which the wrapper translates
    into exit-130) or returns an exit code.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    # Configure stderr-only logging before any subcommand runs. Reads
    # `-v`/`-vv` from the parsed args; falls back to the
    # `SCHEMABRAIN_LOG_LEVEL` env var when no flag is passed.
    configure_logging(verbosity=args.verbose)
    if args.command == "index":
        # `--source URL` and the positional `URL` form are surface-equivalent
        # (both leak credentials into argv; both are deprecated in favor of
        # --url-env). If both are supplied, error rather than guess.
        if args.url is not None and args.source is not None:
            print(
                "error: pass the URL via --source OR positionally, not both",
                file=sys.stderr,
            )
            return 2
        return _cmd_index(
            positional_url=args.source or args.url,
            url_env=args.url_env,
            store_path=args.store_path,
            no_enrich=args.no_enrich,
            max_cost_usd=_resolve_max_cost(args),
            enable_sonnet=args.enable_sonnet,
            no_embed=args.no_embed,
            quiet=args.quiet,
            dry_run=args.dry_run,
            since=args.since,
            no_pii_classify=args.no_pii_classify,
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
            events_path=args.events_path,
            no_events=args.no_events,
            no_audit=args.no_audit,
            pii_block_csv=args.pii_block,
            policy_path=args.policy_path,
            statement_timeout_ms=args.statement_timeout_ms,
            max_rows_per_result=args.max_rows_per_result,
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
                yaml_paths=args.yaml_path,
                positional_url=args.source,
                url_env=args.url_env,
                store_path=args.store_path,
            )
        if args.entity_action == "list":
            return _cmd_entities_list(
                store_path=args.store_path,
                positional_url=args.source,
                url_env=args.url_env,
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
        if args.entity_action == "export":
            return _cmd_entities_export(
                name=args.name,
                out=args.out,
                store_path=args.store_path,
                positional_url=args.source,
                url_env=args.url_env,
            )
        if args.entity_action == "export-all":
            return _cmd_entities_export_all(
                out_dir=args.out_dir,
                store_path=args.store_path,
                positional_url=args.source,
                url_env=args.url_env,
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
                yaml_paths=args.yaml_path,
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
        if args.joins_action == "export":
            return _cmd_joins_export(
                name=args.name,
                out=args.out,
                store_path=args.store_path,
                positional_url=args.source,
                url_env=args.url_env,
            )
        if args.joins_action == "export-all":
            return _cmd_joins_export_all(
                out_dir=args.out_dir,
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
            verify=args.verify,
        )
    if args.command == "apply":
        return _cmd_apply_project(
            project_dir=args.project_dir,
            positional_url=args.source,
            url_env=args.url_env,
            store_path=args.store_path,
        )
    if args.command == "diff":
        return _cmd_diff_project(
            project_dir=args.project_dir,
            positional_url=args.source,
            url_env=args.url_env,
            store_path=args.store_path,
        )
    if args.command == "init":
        return _cmd_init(
            positional_url=args.source,
            url_env=args.url_env,
            store_path=args.store_path,
            host=args.host,
            env_var=args.env_var,
            skip_index=args.skip_index,
            no_entities=args.no_entities,
            no_metrics=args.no_metrics,
            no_joins=args.no_joins,
            no_embed=args.no_embed,
            enrich=args.enrich,
            entities_max_cost_usd=args.entities_max_cost_usd,
            metrics_max_cost_usd=args.metrics_max_cost_usd,
            assume_yes=args.assume_yes,
            print_only=args.print_only,
            from_dbt=args.from_dbt,
            skip_llm_confirm=args.skip_llm_confirm,
            pii_block_csv=args.pii_block,
            emit_yaml_dir=args.emit_yaml_dir,
            enable_sonnet=args.enable_sonnet,
        )
    if args.command == "tail":
        return _cmd_tail(
            since=args.since,
            follow=args.follow,
            json_mode=args.json_mode,
            events_path=args.events_path,
            store_path=args.store_path,
        )
    if args.command == "audit":
        if args.audit_action == "verify":
            return _cmd_audit_verify(
                store_path=args.store_path,
                full=args.full,
                since=args.since,
            )
        if args.audit_action == "list":
            return _cmd_audit_list(
                store_path=args.store_path,
                since=args.since,
                status=args.status,
                tool=args.tool,
                limit=args.limit,
                json_mode=args.json_mode,
            )
        parser.error(f"unknown audit action: {args.audit_action}")  # pragma: no cover
    if args.command == "check":
        return _cmd_check(
            positional_url=args.source,
            url_env=args.url_env,
            store_path=args.store_path,
            json_mode=args.json_mode,
        )
    if args.command == "inspect":
        return _cmd_inspect(
            name=args.name,
            positional_url=args.source,
            url_env=args.url_env,
            store_path=args.store_path,
        )
    if args.command == "docs":
        return _cmd_docs(
            fmt=args.format,
            out=args.out,
            positional_url=args.source,
            url_env=args.url_env,
            store_path=args.store_path,
        )
    if args.command == "metrics":
        if args.metrics_action == "apply":
            return _cmd_metrics_apply(
                yaml_paths=args.yaml_path,
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
        if args.metrics_action == "suggest":
            return _cmd_metrics_suggest(
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
        if args.metrics_action == "audit":
            return _cmd_metrics_audit(
                store_path=args.store_path,
                positional_url=args.source,
                url_env=args.url_env,
                fix=args.fix,
            )
        if args.metrics_action == "export":
            return _cmd_metrics_export(
                name=args.name,
                out=args.out,
                store_path=args.store_path,
                positional_url=args.source,
                url_env=args.url_env,
            )
        if args.metrics_action == "export-all":
            return _cmd_metrics_export_all(
                out_dir=args.out_dir,
                store_path=args.store_path,
                positional_url=args.source,
                url_env=args.url_env,
            )
        if args.metrics_action == "show":
            return _cmd_metrics_show(
                name=args.name,
                store_path=args.store_path,
                positional_url=args.source,
                url_env=args.url_env,
            )
        parser.error(f"unknown metrics action: {args.metrics_action}")  # pragma: no cover
    if args.command == "dashboard":
        return _cmd_dashboard(
            store_path=args.store_path,
            port=args.port,
            open_browser=args.open_browser,
        )
    if args.command == "demo":
        return _cmd_demo(
            action=args.demo_action,
            store_path=args.store_path,
            host=args.host,
            port=args.port,
            open_browser=args.open_browser,
        )
    if args.command == "policy":
        if args.policy_action == "show":
            return _cmd_policy_show(
                store_path=args.store_path,
                policy_path=args.policy_path,
                positional_url=args.source,
                url_env=args.url_env,
            )
        if args.policy_action == "apply":
            return _cmd_policy_apply(
                yaml_path=args.yaml_path,
                store_path=args.store_path,
                positional_url=args.source,
                url_env=args.url_env,
                force_catastrophic_downgrade=args.force_catastrophic_downgrade,
            )
        if args.policy_action == "tag":
            if args.policy_tag_action == "override":
                return _cmd_policy_tag_override(
                    qualified_column=args.qualified_column,
                    sensitivity=args.sensitivity,
                    categories_csv=args.categories,
                    store_path=args.store_path,
                    positional_url=args.source,
                    url_env=args.url_env,
                    force_catastrophic_downgrade=args.force_catastrophic_downgrade,
                )
            if args.policy_tag_action == "clear":
                return _cmd_policy_tag_clear(
                    qualified_column=args.qualified_column,
                    store_path=args.store_path,
                    positional_url=args.source,
                    url_env=args.url_env,
                    force_catastrophic_downgrade=args.force_catastrophic_downgrade,
                )
            if args.policy_tag_action == "list":
                return _cmd_policy_tag_list(
                    origin=args.origin,
                    store_path=args.store_path,
                    positional_url=args.source,
                    url_env=args.url_env,
                )
    # argparse `required=True` on subparsers prevents reaching here, but
    # leaving an explicit branch is cheaper than a guarded assertion.
    parser.error(f"unknown command: {args.command}")  # pragma: no cover


def _positive_float(value: str) -> float:
    """argparse `type=` converter for "must be a positive float".

    `float(value)` alone would accept `0` and negatives, which then
    crash deep inside `CostCeilingGuard.__init__`. Reject at the
    argparse layer so the user sees a clean usage error instead of a
    traceback.
    """
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be a number; got {value!r}") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive value; got {parsed}")
    return parsed


def _nonneg_int(value: str) -> int:
    """argparse `type=` converter for "must be a non-negative int".

    Bare `type=int` silently accepts negatives, which on `audit list`
    bleeds through to SQLite's `LIMIT -1` (SQLite treats any negative
    LIMIT as "unlimited"). The user asked for one row, the store
    returned all of them. Gate at argparse so the error fires before
    SQL ever sees the value.
    """
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be an integer; got {value!r}") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError(f"must be non-negative; got {parsed!r}")
    return parsed


class _GroupedInitHelpAction(argparse.Action):
    """Replacement ``-h/--help`` action for the ``init`` subparser.

    Suppresses argparse's plain-text help dump in favour of the
    design's grouped help surface (handoff bundle
    ``cli/operator.jsx:InitHelp``). The rendered output reads
    the parser's argument groups so the visual layout stays in
    sync with the wire-up declarations.

    Built as an ``argparse.Action`` (not a wrapper around
    ``parser.exit``) so it composes cleanly with argparse's
    standard help-flag conventions: ``-h``/``--help`` short-
    circuits parsing the moment it appears, before any value-
    bearing flag forces a missing-argument error.

    nargs=0 — the help flag takes no argument value, matching
    argparse's built-in ``_HelpAction``.
    """

    def __init__(
        self,
        option_strings: list[str],
        dest: str = argparse.SUPPRESS,
        default: object = argparse.SUPPRESS,
        help: str | None = None,
        **kwargs: object,
    ) -> None:
        # ``**kwargs`` forwards any future argparse-introduced
        # action kwargs (eg ``deprecated`` on Python 3.12+) so
        # this subclass keeps working as the stdlib evolves —
        # without forwarding, a stdlib change that began injecting
        # a new kwarg at registration time would raise ``TypeError``
        # at parser-build time.
        super().__init__(
            option_strings=option_strings,
            dest=dest,
            default=default,
            nargs=0,
            help=help,
            **kwargs,
        )

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: object,
        option_string: str | None = None,
    ) -> None:
        # Lazy import to keep ``init_help_render`` (Rich-dependent)
        # out of the import path for every other subcommand.
        from schemabrain.init_help_render import render_init_help

        render_init_help(parser, console=_stderr_console())
        parser.exit(0)


_CLI_EPILOG = """\
First hour:
  demo                  Zero-setup showcase — sample data + firewall + dashboard (no API key)
  init                  Wire SchemaBrain into a Claude Desktop / Cursor / Windsurf host
  doctor                Verify the wiring (--verify for a no-API-key mock-agent smoke)
  dashboard             Serve the local read-only UI (requires `pip install schemabrain[ui]`)
  inspect               Show what was curated (entities, metrics, joins)
  docs                  Generate a data dictionary (markdown/html) from the store
  tail                  Stream MCP tool calls live

Operate:
  serve                 Run the MCP server (Claude Desktop spawns this for you)
  audit                 List + verify the tamper-evident audit chain
  check                 Detect schema drift since the last `index`
  index                 (Re-)introspect a database into the local store

Author the semantic layer:
  entities · metrics · joins · apply · diff · import
                        See `schemabrain <cmd> --help`. Full reference at
                        https://schemabrain.mintlify.app/reference/cli/overview.

Developer:
  eval · mine-queries · fixture-path
                        Bench harness + query-log mining.

Get started: `uvx schemabrain demo` to see it in action  ·  `uvx schemabrain init` (or `pipx run schemabrain init`) to wire your own database.
"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="schemabrain",
        description=SHORT_DESCRIPTION,
        epilog=_CLI_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
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
        "--source",
        dest="source",
        default=None,
        help="DEPRECATED: source database URL passed as a named flag. Same "
        "argv-leakage trade-off as the positional form; prefer --url-env. "
        "Added for surface parity with `check` / `inspect` / `init` / `serve` "
        "which already accept --source.",
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
        "--no-pii-classify",
        action="store_true",
        help="Skip the heuristic PII classifier and wipe any existing "
        "PII tags for tables touched this run. With classification "
        "ON (default), `get_metric` populates the audit row's "
        "`pii_categories` column and the `--pii-block` policy on "
        "`serve` has data to act on. With classification OFF, audit "
        "rows record `pii_categories=''` and `--pii-block` blocks "
        "nothing. Use only when local tag inference itself is "
        "unwanted (privacy-paranoid environments).",
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
    p_index.add_argument(
        "--since",
        default=None,
        help="Only meaningful with --dry-run. Adds a freshness audit "
        "line to the preview: count of cached columns whose owning "
        "table was last indexed before this point in time, with an "
        "estimated cost to refresh them. Accepts a compact duration "
        "('30s', '5m', '2h', '14d') or an ISO 8601 timestamp with "
        "timezone ('2026-05-01T00:00:00Z'). Lets operators preview "
        "the cost of catching up after a long pause before committing "
        "to a real re-index.",
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
    p_serve.add_argument(
        "--events-path",
        dest="events_path",
        default=None,
        help=f"Path to the JSONL events file the bus appends to "
        f"(default: $SCHEMABRAIN_EVENTS_PATH or {_DEFAULT_EVENTS_PATH}). "
        f"Use `schemabrain tail` to read it.",
    )
    p_serve.add_argument(
        "--no-events",
        dest="no_events",
        action="store_true",
        help="Disable event emission entirely (no JSONL file is written, "
        "no server_start/stop events). Useful for CI runs that don't want "
        "a stray events file in $HOME.",
    )
    p_serve.add_argument(
        "--no-audit",
        dest="no_audit",
        action="store_true",
        help="Disable the mcp_audit table writer for this `serve` "
        "process. No audit rows land; the table stays as it was. The "
        "tools still respond — only the durable per-call record is "
        "suppressed. Use for CI runs or test contexts where audit "
        "writes would clutter a shared store.",
    )
    p_serve.add_argument(
        "--pii-block",
        dest="pii_block",
        default=None,
        help="Comma-separated PIICategory list whose presence in a "
        "compiled get_metric plan triggers a refused envelope "
        "(refusal_reason='pii_blocked'). Example: "
        "--pii-block contact,health. Unknown category names abort "
        "startup with an error listing the 12 valid values. "
        "Omitted (no flag) reads `block:` from --policy-path if the "
        "file exists, otherwise defaults to credential,payment_card,"
        "government_id (the catastrophic-leak categories). Pass "
        "--pii-block '' to explicitly disable enforcement (overrides "
        "the YAML file; PII tags still flow to the audit row).",
    )
    p_serve.add_argument(
        "--policy-path",
        dest="policy_path",
        default=_DEFAULT_POLICY_PATH,
        help=f"Path to a pii_policy.yaml file. When present, its "
        f"`block:` field is read at startup as the default --pii-block "
        f"set. Default: {_DEFAULT_POLICY_PATH}. Explicit --pii-block "
        f"always wins. Per-column overrides live in the store and are "
        f"populated by `schemabrain policy apply`.",
    )
    p_serve.add_argument(
        "--statement-timeout-ms",
        dest="statement_timeout_ms",
        type=int,
        default=30000,
        metavar="MS",
        help="Postgres-level statement_timeout (milliseconds) applied "
        "to every get_metric query. Caps query runtime at the source "
        "DB; a runaway query aborts with a clear `OperationalError` "
        "rather than blocking the MCP server's process pool. Injected "
        "into `connect_args.options` so it CAN'T be overridden via "
        "URL query params. Default 30000 (30s) — generous headroom "
        "for analytical queries while bounding pathological ones. "
        "Pass `0` to disable (Postgres treats `statement_timeout=0` "
        "as unbounded).",
    )
    p_serve.add_argument(
        "--max-rows-per-result",
        dest="max_rows_per_result",
        type=int,
        default=10000,
        metavar="N",
        help="Application-level cap on rows returned by each "
        "get_metric call. Counts after the SQL executes (the source DB "
        "still does the full scan), so this is a payload-size guard, "
        "not a query-cost guard — use `--statement-timeout-ms` for "
        "the latter. Default 10000 — well past any context window an "
        "LLM can reason over, while bounding accidental `SELECT *` "
        "blowups. Pass `0` to disable the cap.",
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
    # Three actions today: `apply` (file -> store loader), `list` (the
    # verification path after apply — mirrors `joins list` and
    # `metrics list`), and `suggest` (LLM-suggest pipeline with three
    # output modes).
    p_entities = sub.add_parser(
        "entities",
        help="Manage semantic entity definitions",
    )
    entity_sub = p_entities.add_subparsers(dest="entity_action", required=True)

    p_entities_list = entity_sub.add_parser(
        "list",
        help="List entities in the local store. The verification path after `entities apply`.",
    )
    p_entities_list.add_argument(
        "--store-path",
        default=_DEFAULT_STORE_PATH,
        help=f"Path to the local SQLite store (default: {_DEFAULT_STORE_PATH})",
    )
    p_entities_list.add_argument(
        "--source",
        default=None,
        help="Filter listing to one source (the same URL passed to "
        "`index`). Without this flag, lists across every source.",
    )
    p_entities_list.add_argument(
        "--url-env",
        dest="url_env",
        metavar="VARNAME",
        default=None,
        help="Name of the environment variable that holds the source URL.",
    )

    p_apply = entity_sub.add_parser(
        "apply",
        help="Load entity YAML file(s) or directory(ies) into the local store.",
    )
    p_apply.add_argument(
        "yaml_path",
        nargs="+",
        help="One or more entity YAML files OR directories of YAML files "
        "(each file ending in `.yaml`/`.yml`). Shell globs work — "
        "`entities apply dir/*.yaml` and `entities apply dir/` both apply "
        "every YAML in the directory. Multi-file apply lands each file "
        "independently; an error in one file skips that file and reports "
        "it in the summary.",
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

    # `entities export <name>` — single entity store → YAML. Inverse of
    # `entities apply`. Writes to stdout (default) so `entities export X
    # | tee X.yaml` is the natural workflow; `--out PATH` writes to disk
    # directly. Cross-source posture mirrors `metrics show`: without
    # `--source`/`--url-env` the handler walks every source the store
    # knows about, errors if the same name lives in multiple sources.
    p_entities_export = entity_sub.add_parser(
        "export",
        help="Render one entity from the local store as apply-ready YAML on stdout (or --out PATH).",
    )
    p_entities_export.add_argument(
        "name",
        help="Entity name to export. Without --source/--url-env, the handler "
        "errors if the same name is present in multiple sources.",
    )
    p_entities_export.add_argument(
        "--out",
        dest="out",
        default=None,
        help="Optional output path. Without this flag, writes to stdout.",
    )
    p_entities_export.add_argument(
        "--source",
        default=None,
        help="Filter to one source (the same URL passed to `index`). "
        "Without this flag, walks every source. DEPRECATED when the "
        "URL contains a password; prefer --url-env.",
    )
    p_entities_export.add_argument(
        "--url-env",
        dest="url_env",
        metavar="VARNAME",
        default=None,
        help="Env var holding the source URL.",
    )
    p_entities_export.add_argument(
        "--store-path",
        default=_DEFAULT_STORE_PATH,
        help=f"Path to the local SQLite store (default: {_DEFAULT_STORE_PATH})",
    )

    # `entities export-all --dir PATH` — every entity as one YAML per
    # row. Pairs with `schemabrain apply <dir>` for the dbt-shaped
    # store ↔ YAML round-trip. Refuses if `--dir` already contains a
    # `<entity>.yaml` so prior hand-edits are not silently overwritten;
    # also refuses on cross-source name collisions when no `--source`
    # is passed (two sources with the same entity name would clobber).
    p_entities_export_all = entity_sub.add_parser(
        "export-all",
        help="Write one apply-ready YAML per entity into --dir.",
    )
    p_entities_export_all.add_argument(
        "--dir",
        dest="out_dir",
        required=True,
        help="Output directory. Created if missing. Refuses to overwrite "
        "existing `<entity>.yaml` files to preserve hand-edits.",
    )
    p_entities_export_all.add_argument(
        "--source",
        default=None,
        help="Filter to one source. Without this flag, exports across "
        "every source — the handler refuses on entity-name collisions.",
    )
    p_entities_export_all.add_argument(
        "--url-env",
        dest="url_env",
        metavar="VARNAME",
        default=None,
        help="Env var holding the source URL.",
    )
    p_entities_export_all.add_argument(
        "--store-path",
        default=_DEFAULT_STORE_PATH,
        help=f"Path to the local SQLite store (default: {_DEFAULT_STORE_PATH})",
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
        help="Load canonical-join YAML file(s) or directory(ies) into the local store.",
    )
    p_joins_apply.add_argument(
        "yaml_path",
        nargs="+",
        help="One or more canonical-join YAML files OR directories of "
        "YAML files (each file ending in `.yaml`/`.yml`). Shell globs "
        "work — `joins apply dir/*.yaml` and `joins apply dir/` both "
        "apply every YAML in the directory. Multi-file apply lands each "
        "file independently; an error in one file skips that file and "
        "reports it in the summary.",
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

    # `joins export <name>` / `joins export-all --dir` — same shape as
    # the entities + metrics export commands.
    p_joins_export = joins_sub.add_parser(
        "export",
        help="Render one canonical join from the local store as apply-ready YAML on stdout (or --out PATH).",
    )
    p_joins_export.add_argument(
        "name",
        help="Canonical-join name to export. Without --source/--url-env, "
        "the handler errors if the same name is present in multiple sources.",
    )
    p_joins_export.add_argument(
        "--out",
        dest="out",
        default=None,
        help="Optional output path. Without this flag, writes to stdout.",
    )
    p_joins_export.add_argument(
        "--source",
        default=None,
        help="Filter to one source. Without this flag, walks every source.",
    )
    p_joins_export.add_argument(
        "--url-env",
        dest="url_env",
        metavar="VARNAME",
        default=None,
        help="Env var holding the source URL.",
    )
    p_joins_export.add_argument(
        "--store-path",
        default=_DEFAULT_STORE_PATH,
        help=f"Path to the local SQLite store (default: {_DEFAULT_STORE_PATH})",
    )

    p_joins_export_all = joins_sub.add_parser(
        "export-all",
        help="Write one apply-ready YAML per canonical join into --dir.",
    )
    p_joins_export_all.add_argument(
        "--dir",
        dest="out_dir",
        required=True,
        help="Output directory. Refuses to overwrite existing `<join>.yaml` files.",
    )
    p_joins_export_all.add_argument(
        "--source",
        default=None,
        help="Filter to one source. Without this flag, refuses on cross-source name collisions.",
    )
    p_joins_export_all.add_argument(
        "--url-env",
        dest="url_env",
        metavar="VARNAME",
        default=None,
        help="Env var holding the source URL.",
    )
    p_joins_export_all.add_argument(
        "--store-path",
        default=_DEFAULT_STORE_PATH,
        help=f"Path to the local SQLite store (default: {_DEFAULT_STORE_PATH})",
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
        help="Load metric YAML file(s) or directory(ies) into the local store.",
    )
    p_metrics_apply.add_argument(
        "yaml_path",
        nargs="+",
        help="One or more metric YAML files OR directories of YAML files "
        "(each file ending in `.yaml`/`.yml`). Shell globs work — "
        "`metrics apply dir/*.yaml` and `metrics apply dir/` both apply "
        "every YAML in the directory. Multi-file apply lands each file "
        "independently; an error in one file skips that file and reports "
        "it in the summary.",
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

    p_metrics_audit = metrics_sub.add_parser(
        "audit",
        help="Scan applied metrics for anti-pattern descriptions and "
        "optionally remove them. Counterpart to the suggest-time "
        "anti-pattern filter for stores written before that filter shipped.",
    )
    p_metrics_audit.add_argument(
        "--store-path",
        default=_DEFAULT_STORE_PATH,
        help=f"Path to the local SQLite store (default: {_DEFAULT_STORE_PATH})",
    )
    p_metrics_audit.add_argument(
        "--source",
        default=None,
        help="Filter audit to one source. Without this flag, audits "
        "across every source in the store.",
    )
    p_metrics_audit.add_argument(
        "--url-env",
        dest="url_env",
        metavar="VARNAME",
        default=None,
        help="Name of the environment variable that holds the source URL.",
    )
    p_metrics_audit.add_argument(
        "--fix",
        action="store_true",
        help="Delete every flagged metric (excluding dbt-owned ones). "
        "Without this flag, audit is read-only — lists findings and "
        "exits non-zero if any were found.",
    )

    p_metrics_show = metrics_sub.add_parser(
        "show",
        help="Drill into one metric by name. Namespaced shortcut for "
        "`schemabrain inspect <name>` that resolves only against the "
        "metrics namespace — useful when a name collides with an "
        "entity or join.",
    )
    p_metrics_show.add_argument(
        "name",
        help="Metric name to drill into. Run `schemabrain metrics list` to see what's available.",
    )
    p_metrics_show.add_argument(
        "--store-path",
        default=_DEFAULT_STORE_PATH,
        help=f"Path to the local SQLite store (default: {_DEFAULT_STORE_PATH})",
    )
    p_metrics_show.add_argument(
        "--source",
        default=None,
        help="Filter to one source. Without this flag, walks every "
        "source the store knows about and renders each match.",
    )
    p_metrics_show.add_argument(
        "--url-env",
        dest="url_env",
        metavar="VARNAME",
        default=None,
        help="Name of the environment variable that holds the source URL.",
    )

    # `metrics export <name>` — mirrors `entities export`. Writes to
    # stdout by default; `--out PATH` writes the body to disk.
    p_metrics_export = metrics_sub.add_parser(
        "export",
        help="Render one metric from the local store as apply-ready YAML on stdout (or --out PATH).",
    )
    p_metrics_export.add_argument(
        "name",
        help="Metric name to export. Without --source/--url-env, the handler "
        "errors if the same name is present in multiple sources.",
    )
    p_metrics_export.add_argument(
        "--out",
        dest="out",
        default=None,
        help="Optional output path. Without this flag, writes to stdout.",
    )
    p_metrics_export.add_argument(
        "--source",
        default=None,
        help="Filter to one source. Without this flag, walks every source.",
    )
    p_metrics_export.add_argument(
        "--url-env",
        dest="url_env",
        metavar="VARNAME",
        default=None,
        help="Env var holding the source URL.",
    )
    p_metrics_export.add_argument(
        "--store-path",
        default=_DEFAULT_STORE_PATH,
        help=f"Path to the local SQLite store (default: {_DEFAULT_STORE_PATH})",
    )

    p_metrics_export_all = metrics_sub.add_parser(
        "export-all",
        help="Write one apply-ready YAML per metric into --dir.",
    )
    p_metrics_export_all.add_argument(
        "--dir",
        dest="out_dir",
        required=True,
        help="Output directory. Refuses to overwrite existing `<metric>.yaml` files.",
    )
    p_metrics_export_all.add_argument(
        "--source",
        default=None,
        help="Filter to one source. Without this flag, refuses on cross-source name collisions.",
    )
    p_metrics_export_all.add_argument(
        "--url-env",
        dest="url_env",
        metavar="VARNAME",
        default=None,
        help="Env var holding the source URL.",
    )
    p_metrics_export_all.add_argument(
        "--store-path",
        default=_DEFAULT_STORE_PATH,
        help=f"Path to the local SQLite store (default: {_DEFAULT_STORE_PATH})",
    )

    p_metrics_suggest = metrics_sub.add_parser(
        "suggest",
        help="LLM-suggest metric candidates anchored on existing entities.",
    )
    p_metrics_suggest.add_argument(
        "--source",
        default=None,
        help="The same source URL passed to `index`. DEPRECATED when "
        "the URL contains a password; prefer --url-env.",
    )
    p_metrics_suggest.add_argument(
        "--url-env",
        dest="url_env",
        metavar="VARNAME",
        default=None,
        help="Name of the environment variable that holds the source URL. "
        "Preferred over --source so credentials never appear in argv.",
    )
    p_metrics_suggest.add_argument(
        "--store-path",
        default=_DEFAULT_STORE_PATH,
        help=f"Path to the local SQLite store (default: {_DEFAULT_STORE_PATH})",
    )
    # The three output modes are mutually exclusive; argparse enforces.
    # Mirrors `entities suggest` and `joins suggest` so users learn the
    # pattern once.
    metrics_mode_group = p_metrics_suggest.add_mutually_exclusive_group(required=True)
    metrics_mode_group.add_argument(
        "--dry-run",
        action="store_true",
        help="Print candidates (metric body + envelope) to stdout. No "
        "files written, no store writes. Best mode for cost/quality "
        "previews.",
    )
    metrics_mode_group.add_argument(
        "--out-dir",
        dest="out_dir",
        default=None,
        help="Directory to write one YAML file per candidate "
        "(<metric_name>.yaml) plus a sidecar `_suggestion_metadata.json` "
        "carrying confidence/rationale. The per-metric YAML is "
        "`metrics apply`-ready: edit, then apply per file.",
    )
    metrics_mode_group.add_argument(
        "--apply",
        action="store_true",
        help="Write candidates directly to the local store with "
        "origin='suggested'. Skips the review step — use --out-dir if "
        "you want a chance to edit before committing.",
    )
    p_metrics_suggest.add_argument(
        "--top-k",
        dest="top_k",
        type=int,
        default=_DEFAULT_SUGGEST_TOP_K,
        help=f"Maximum number of candidates to keep (default: "
        f"{_DEFAULT_SUGGEST_TOP_K}). The cap is both communicated to "
        f"the LLM and enforced post-parse.",
    )
    p_metrics_suggest.add_argument(
        "--provider",
        choices=["anthropic", "stub"],
        default="anthropic",
        help="LLM provider. `anthropic` is the production default; "
        "`stub` reads the canned response from "
        "$SCHEMABRAIN_STUB_RESPONSE and is intended for CI smoke "
        "tests, not for real schemas.",
    )
    p_metrics_suggest.add_argument(
        "--max-cost-usd",
        dest="max_cost_usd",
        type=float,
        default=None,
        help=f"Hard cap on USD spend per run (default: "
        f"${_DEFAULT_SUGGEST_MAX_COST_USD:.2f}). Aborts cleanly when "
        f"reached. Reads {_SUGGEST_COST_ENV_VAR} if unset; CLI "
        f"flag wins on conflict.",
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
        choices=("claude-desktop", "claude-code", "cursor", "windsurf", "manual"),
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
    p_doctor.add_argument(
        "--verify",
        action="store_true",
        help="Run a mock-agent end-to-end smoke against the substrate "
        "instead of the config-health report. Simulates one MCP tool "
        "turn (list_entities → describe_entity → find_relevant_entities "
        "→ get_metric) without needing an LLM key or a running MCP "
        "host. Exits 0 if all required stages pass, 2 if any fail.",
    )

    # `schemabrain apply [PROJECT_DIR]` — walk a project tree
    # (entities/, metrics/, joins/ subdirs) and apply each YAML to the
    # store. Pairs with `init --emit-yaml-dir` and the per-resource
    # `export[-all]` commands for the full store ↔ YAML round-trip
    # workflow. The directory order is deliberate (entities first,
    # then metrics + joins which reference entities); the per-
    # resource apply commands enforce FK invariants internally so a
    # broken reference fails the inner command, not the outer walker.
    p_apply_root = sub.add_parser(
        "apply",
        help="Apply a project tree of entity / metric / join YAMLs against a source.",
    )
    p_apply_root.add_argument(
        "project_dir",
        nargs="?",
        default="./schemabrain",
        help="Path to the project tree. Expected layout: "
        "<dir>/entities/*.yaml, <dir>/metrics/*.yaml, <dir>/joins/*.yaml. "
        "Missing subdirs are skipped cleanly. Default: ./schemabrain",
    )
    p_apply_root.add_argument(
        "--source",
        default=None,
        help="The source URL the YAMLs attach to. DEPRECATED when the "
        "URL contains a password; prefer --url-env.",
    )
    p_apply_root.add_argument(
        "--url-env",
        dest="url_env",
        metavar="VARNAME",
        default=None,
        help="Env var holding the source URL.",
    )
    p_apply_root.add_argument(
        "--store-path",
        default=_DEFAULT_STORE_PATH,
        help=f"Path to the local SQLite store (default: {_DEFAULT_STORE_PATH})",
    )

    # `schemabrain diff [PROJECT_DIR]` — drift check. Reports
    # only-on-disk / only-in-store / value-mismatch per resource type
    # by round-tripping both sides through the YAML serialiser
    # (matching the round-trip semantics of `export[-all]` ↔ `apply`).
    # Trust-signal fields are deliberately excluded from comparison
    # because the YAML grammar does not carry them.
    p_diff = sub.add_parser(
        "diff",
        help="Show drift between a project YAML tree and the store. CI-friendly exit codes (0 in-sync, 1 drift, 2 error).",
    )
    p_diff.add_argument(
        "project_dir",
        nargs="?",
        default="./schemabrain",
        help="Path to the project tree. Same layout as `schemabrain apply`. Default: ./schemabrain",
    )
    p_diff.add_argument(
        "--source",
        default=None,
        help="The source URL the YAMLs attach to.",
    )
    p_diff.add_argument(
        "--url-env",
        dest="url_env",
        metavar="VARNAME",
        default=None,
        help="Env var holding the source URL.",
    )
    p_diff.add_argument(
        "--store-path",
        default=_DEFAULT_STORE_PATH,
        help=f"Path to the local SQLite store (default: {_DEFAULT_STORE_PATH})",
    )

    p_init = sub.add_parser(
        "init",
        help="Wire schemabrain into an MCP host (Claude Desktop, Claude Code, or print snippet)",
        add_help=False,
    )
    # Custom ``-h/--help`` action so we can render the design's
    # grouped help surface (handoff bundle ``cli/operator.jsx:InitHelp``).
    # ``add_help=False`` above suppresses argparse's default
    # ``-h/--help`` action; this replacement lives inside the
    # ``Behavior`` group below so it does not leak into a sixth
    # render-block in the help screen.
    p_init.add_argument(
        "-h",
        "--help",
        action=_GroupedInitHelpAction,
        help="show this help message and exit",
    )

    # Argument groups carry the design's group labels (title) and
    # one-line purposes (description). The grouped help renderer
    # walks `parser._action_groups` and emits one design block per
    # group; argparse's default ``--help`` would print the same
    # groups as plain-text headers, so an operator's fallback
    # experience (eg redirected to a file) still gets a structured
    # help screen. The groups also surface in shell completions
    # generated from ``argparse``'s introspection.
    g_source = p_init.add_argument_group(
        "Source",
        description="where does the schema come from?",
    )
    g_source.add_argument(
        "--source",
        metavar="URL",
        default=None,
        help="Postgres URL · prefer --url-env when the URL contains a password.",
    )
    g_source.add_argument(
        "--url-env",
        dest="url_env",
        metavar="VARNAME",
        default=None,
        help="Env var holding the source URL (eg DATABASE_URL) · keeps creds out of argv.",
    )
    g_source.add_argument(
        "--from-dbt",
        dest="from_dbt",
        metavar="PATH",
        default=None,
        help="Import entities + metrics from a dbt manifest · auto-detected from "
        "$DBT_PROJECT_DIR when omitted.",
    )

    g_stages = p_init.add_argument_group(
        "Stages",
        description="turn individual wizard stages on or off",
    )
    g_stages.add_argument(
        "--skip-index",
        dest="skip_index",
        action="store_true",
        help="Skip the wizard's index stage. Pass this when you've already "
        "indexed in a different session or plan to index later.",
    )
    g_stages.add_argument(
        "--enrich",
        dest="enrich",
        action="store_true",
        help="LLM column descriptions (Haiku) during index · needs ANTHROPIC_API_KEY · "
        "~$0.10-$2.00 for a 50-table schema.",
    )
    g_stages.add_argument(
        "--no-entities",
        dest="no_entities",
        action="store_true",
        help="Skip the wizard's entity-suggestion stage. The wizard still "
        "wires the MCP host; you can curate entities later via "
        "`schemabrain entities suggest --apply`.",
    )
    g_stages.add_argument(
        "--no-metrics",
        dest="no_metrics",
        action="store_true",
        help="Skip the wizard's metric-suggestion stage. The wizard still "
        "wires the MCP host; you can curate metrics later via "
        "`schemabrain metrics suggest --apply`.",
    )
    g_stages.add_argument(
        "--no-joins",
        dest="no_joins",
        action="store_true",
        help="Skip the wizard's canonical-join suggestion stage. The wizard "
        "still wires the MCP host; you can curate joins later via "
        "`schemabrain joins suggest --apply`. The join suggester is "
        "deterministic (FK + query-log mining) — no LLM cost, no API key.",
    )
    g_stages.add_argument(
        "--no-embed",
        dest="no_embed",
        action="store_true",
        help="Skip local sentence-embedding generation during the index "
        "stage. Required to run the wizard on Apple Silicon + Python "
        "3.12+ where `fastembed`'s `onnxruntime` dependency has no wheel. "
        "Degrades `find_relevant_entities` from vector similarity to "
        "keyword/substring matching; everything else works unchanged.",
    )
    g_stages.add_argument(
        "--enable-sonnet",
        dest="enable_sonnet",
        action="store_true",
        help="During the index stage (needs --enrich + ANTHROPIC_API_KEY), "
        "route cryptic column names (heavily abbreviated, e.g. `acct_dim_v3`) "
        "to Claude Sonnet 4.6 instead of Haiku 4.5. Sonnet is ~5x more "
        "expensive per call but produces better descriptions for hard-to-decode "
        "names. Default off (Haiku-only) to keep runs cheap; same opt-in as "
        "`schemabrain index --enable-sonnet`.",
    )

    g_host = p_init.add_argument_group(
        "Host",
        description="which AI agent to wire up + where the store lives",
    )
    g_host.add_argument(
        "--host",
        choices=("claude-desktop", "claude-code", "cursor", "windsurf", "manual"),
        default=None,
        help="Which host to wire. Omit to auto-detect (interactive menu shows "
        "detected hosts; non-TTY / --yes paths use the priority winner from "
        "detect_host). `manual` prints the snippet without writing.",
    )
    g_host.add_argument(
        "--store-path",
        metavar="PATH",
        default=_DEFAULT_STORE_PATH,
        help=f"Path to the local SQLite store (default: {_DEFAULT_STORE_PATH})",
    )
    g_host.add_argument(
        "--env-var",
        dest="env_var",
        metavar="VARNAME",
        default="SCHEMABRAIN_DATABASE_URL",
        help="Env var the host sets to the DB URL (default: SCHEMABRAIN_DATABASE_URL).",
    )

    g_cost = p_init.add_argument_group(
        "Cost",
        description="spend ceilings · enforced before each LLM call",
    )
    g_cost.add_argument(
        "--entities-max-cost-usd",
        dest="entities_max_cost_usd",
        metavar="USD",
        type=_positive_float,
        default=None,
        help="Stage 03 cost cap · positive · defaults to $SCHEMABRAIN_MAX_LLM_COST_USD.",
    )
    g_cost.add_argument(
        "--metrics-max-cost-usd",
        dest="metrics_max_cost_usd",
        metavar="USD",
        type=_positive_float,
        default=None,
        help="Stage 04 cost cap · positive · defaults to $SCHEMABRAIN_MAX_LLM_COST_USD.",
    )

    g_behavior = p_init.add_argument_group(
        "Behavior",
        description="how the wizard runs",
    )
    g_behavior.add_argument(
        "--yes",
        "-y",
        dest="assume_yes",
        action="store_true",
        help="Accept every prompt · for CI · implies --skip-llm-confirm + host-overwrite confirm.",
    )
    g_behavior.add_argument(
        "--skip-llm-confirm",
        dest="skip_llm_confirm",
        action="store_true",
        help="Skip the Enter-to-continue pause before LLM stages · auto-on in non-TTY.",
    )
    g_behavior.add_argument(
        "--print-only",
        dest="print_only",
        action="store_true",
        help="Alias for --host manual: print the snippet, write nothing.",
    )
    g_behavior.add_argument(
        "--pii-block",
        dest="pii_block",
        default=None,
        help="Comma-separated PIICategory list to write into the host "
        "snippet as `serve --pii-block`. Omitted defaults to "
        "credential,payment_card,government_id under --yes (with a "
        "stderr confirmation) or to the interactive prompt otherwise. "
        "Pass '' (empty) to explicitly disable enforcement.",
    )
    g_behavior.add_argument(
        "--emit-yaml-dir",
        dest="emit_yaml_dir",
        default=None,
        metavar="PATH",
        help="After the wizard completes, write one YAML per applied "
        "entity / metric / canonical join into PATH/entities/, "
        "PATH/metrics/, PATH/joins/. Gives the operator on-disk "
        "definitions to edit and re-apply alongside the SQLite store. "
        "Refuses if any target file already exists.",
    )

    p_tail = sub.add_parser(
        "tail",
        help="Stream MCP tool-call events from a running schemabrain serve process",
    )
    p_tail.add_argument(
        "--since",
        default="5m",
        help="Replay events newer than this point. Accepts a compact duration "
        "like '30s' / '5m' / '2h' / '1d', or an ISO 8601 timestamp with "
        "timezone like '2026-05-17T10:00:00Z'. Default: 5m.",
    )
    follow_group = p_tail.add_mutually_exclusive_group()
    follow_group.add_argument(
        "--follow",
        dest="follow",
        action="store_true",
        default=True,
        help="Keep the process attached and print new events as they arrive (default).",
    )
    follow_group.add_argument(
        "--no-follow",
        dest="follow",
        action="store_false",
        help="Print history matching --since and exit.",
    )
    p_tail.add_argument(
        "--json",
        dest="json_mode",
        action="store_true",
        help="Emit raw JSON lines instead of the colored two-line record. "
        "Pipe-friendly for jq / awk.",
    )
    p_tail.add_argument(
        "--events-path",
        dest="events_path",
        default=None,
        help=f"Path to the JSONL events file written by `schemabrain serve`. "
        f"Default: $SCHEMABRAIN_EVENTS_PATH or {_DEFAULT_EVENTS_PATH}.",
    )
    # Operators reflexively pass `--store-path` to `tail` (every other
    # subcommand accepts it). Accept it here so the CLI doesn't
    # surface a hostile `unrecognized arguments` error.
    # The events JSONL is decoupled from the SQLite store by default
    # (events go to `~/.schemabrain/events.jsonl`, store goes wherever
    # the operator chose), so `--store-path` is only used as a
    # convenience hint: if the operator wrote events alongside the
    # store via `--events-path`, this flag lets `tail` discover them
    # without re-typing the path. See `_resolve_tail_events_path`
    # for the resolution order.
    p_tail.add_argument(
        "--store-path",
        dest="store_path",
        default=None,
        help="Path to the SQLite store. Accepted for surface parity with "
        "every other subcommand. `tail` reads from the events JSONL, "
        "not the store, so this is only used as a convenience: when "
        "`--events-path` is omitted AND a file named `events.jsonl` "
        "exists in the store's directory, that file is preferred over "
        "the default. Pass `--events-path PATH` to override directly.",
    )

    p_audit = sub.add_parser(
        "audit",
        help="Inspect or verify the local mcp_audit table",
    )
    audit_sub = p_audit.add_subparsers(dest="audit_action", required=True)

    p_audit_verify = audit_sub.add_parser(
        "verify",
        help="Recompute the chain hash for every mcp_audit row; report mismatches",
    )
    p_audit_verify.add_argument(
        "--store-path",
        default=_DEFAULT_STORE_PATH,
        help=f"Path to the local SQLite store (default: {_DEFAULT_STORE_PATH})",
    )
    p_audit_verify.add_argument(
        "--full",
        action="store_true",
        help="Walk every row and report all mismatches. Default stops "
        "at the first mismatch for a fast yes/no integrity check.",
    )
    p_audit_verify.add_argument(
        "--since",
        default=None,
        help="Anchor the walk to a known-good cursor row instead of "
        "walking from genesis. Accepts: a leading hex prefix (≥8 "
        "chars) of a previously-archived chain_hash; a compact "
        "duration like '7d' / '2h' / '30s' / '5m'; or an ISO 8601 "
        "timestamp with timezone like '2026-05-17T10:00:00Z'. The "
        "cursor row's own integrity is NOT re-verified (operator "
        "must have archived a trusted copy externally) — only rows "
        "after it are. Default: walk from genesis.",
    )

    p_audit_list = audit_sub.add_parser(
        "list",
        help="List recent mcp_audit rows with optional filters",
    )
    p_audit_list.add_argument(
        "--store-path",
        default=_DEFAULT_STORE_PATH,
        help=f"Path to the local SQLite store (default: {_DEFAULT_STORE_PATH})",
    )
    p_audit_list.add_argument(
        "--since",
        default=None,
        help="Show rows newer than this point. Accepts a compact "
        "duration like '30s' / '5m' / '2h' / '1d', or an ISO 8601 "
        "timestamp with timezone like '2026-05-17T10:00:00Z'. Default: all.",
    )
    p_audit_list.add_argument(
        "--status",
        default=None,
        choices=["success", "empty", "partial", "degraded", "error", "refused"],
        help="Filter by Charter envelope status.",
    )
    p_audit_list.add_argument(
        "--tool",
        default=None,
        help="Filter by tool_name (exact match, e.g. `describe_table`).",
    )
    p_audit_list.add_argument(
        "--limit",
        type=_nonneg_int,
        default=100,
        help="Maximum rows to return. Default 100.",
    )
    p_audit_list.add_argument(
        "--json",
        dest="json_mode",
        action="store_true",
        help="Emit JSON lines instead of the rich-rendered table. Pipe-friendly for jq / awk.",
    )

    p_check = sub.add_parser(
        "check",
        help="Detect drift between persisted definitions and the live source schema",
    )
    p_check.add_argument(
        "--source",
        default=None,
        help="Source URL (e.g. postgresql+psycopg://...). DEPRECATED when the URL "
        "contains a password; prefer --url-env. One of --source / --url-env is required.",
    )
    p_check.add_argument(
        "--url-env",
        dest="url_env",
        metavar="VARNAME",
        default=None,
        help="Name of the environment variable that holds the source URL "
        "(e.g. --url-env DATABASE_URL). Preferred over --source so credentials "
        "never appear in argv. Mutually exclusive with --source.",
    )
    p_check.add_argument(
        "--store-path",
        default=_DEFAULT_STORE_PATH,
        help=f"Path to the local SQLite store (default: {_DEFAULT_STORE_PATH})",
    )
    p_check.add_argument(
        "--json",
        dest="json_mode",
        action="store_true",
        help="Emit JSON to stdout instead of the rich-rendered report to stderr. "
        "Pipe-friendly for CI / monitoring scripts.",
    )

    p_inspect = sub.add_parser(
        "inspect",
        help="Browse the indexed schema + semantic-layer surface (no LLM, no source)",
    )
    p_inspect.add_argument(
        "name",
        nargs="?",
        default=None,
        help="Optional entity name to drill into. Omit for a summary of the whole store.",
    )
    p_inspect.add_argument(
        "--source",
        default=None,
        help="Source URL — DEPRECATED when the URL contains a password; "
        "prefer --url-env. Optional: omit to operate across every source "
        "in the store. Required only when the store carries entities "
        "from multiple sources AND you're drilling by name.",
    )
    p_inspect.add_argument(
        "--url-env",
        dest="url_env",
        metavar="VARNAME",
        default=None,
        help="Name of the environment variable that holds the source URL. "
        "Mutually exclusive with --source.",
    )
    p_inspect.add_argument(
        "--store-path",
        default=_DEFAULT_STORE_PATH,
        help=f"Path to the local SQLite store (default: {_DEFAULT_STORE_PATH})",
    )

    # `docs` renders a data dictionary from the store — every indexed
    # table, column, type, PII classification, semantic join, and metric.
    # Store-only reader (no LLM, no live source), same shape as `inspect`.
    p_docs = sub.add_parser(
        "docs",
        help="Generate a data dictionary (markdown/html) from the indexed store (no LLM)",
    )
    p_docs.add_argument(
        "--format",
        choices=["markdown", "html"],
        default="markdown",
        help="Output format (default: markdown).",
    )
    p_docs.add_argument(
        "--out",
        dest="out",
        default=None,
        help="Write the dictionary to this file. Without --out, writes to stdout.",
    )
    p_docs.add_argument(
        "--source",
        default=None,
        help="Scope to one indexed source. Optional — required only when the "
        "store carries more than one source. Prefer --url-env when the URL "
        "contains a password.",
    )
    p_docs.add_argument(
        "--url-env",
        dest="url_env",
        metavar="VARNAME",
        default=None,
        help="Name of the environment variable that holds the source URL. "
        "Mutually exclusive with --source.",
    )
    p_docs.add_argument(
        "--store-path",
        default=_DEFAULT_STORE_PATH,
        help=f"Path to the local SQLite store (default: {_DEFAULT_STORE_PATH})",
    )

    # `policy` is the operator-facing surface for PII enforcement.
    # Five actions: `show` (read-only state inspection), `apply`
    # (load a pii_policy.yaml file into the store + emit a confirmation),
    # `tag override` (single-column upsert with `origin='operator'`),
    # `tag clear` (single-column delete of an operator override), and
    # `tag list` (provenance-filterable listing).
    p_policy = sub.add_parser(
        "policy",
        help="View and edit PII enforcement policy (block set + per-column overrides)",
    )
    policy_sub = p_policy.add_subparsers(dest="policy_action", required=True)

    p_policy_show = policy_sub.add_parser(
        "show",
        help="Print the active PII policy: block set + per-column tag listing",
    )
    p_policy_show.add_argument(
        "--store-path",
        default=_DEFAULT_STORE_PATH,
        help=f"Path to the local SQLite store (default: {_DEFAULT_STORE_PATH})",
    )
    p_policy_show.add_argument(
        "--policy-path",
        default=_DEFAULT_POLICY_PATH,
        help=f"Path to the pii_policy.yaml file (default: {_DEFAULT_POLICY_PATH}). "
        "If absent, falls back to the catastrophic-leak default block set.",
    )
    p_policy_show.add_argument(
        "--source",
        default=None,
        help="The same source URL passed to `index`. DEPRECATED when the URL "
        "contains a password; prefer --url-env.",
    )
    p_policy_show.add_argument(
        "--url-env",
        dest="url_env",
        metavar="VARNAME",
        default=None,
        help="Name of the environment variable that holds the source URL.",
    )

    p_policy_apply = policy_sub.add_parser(
        "apply",
        help="Load a pii_policy.yaml file into the local store "
        "(persists column_overrides; block set is read by `serve` from the YAML)",
    )
    p_policy_apply.add_argument(
        "yaml_path",
        nargs="?",
        default=_DEFAULT_POLICY_PATH,
        help=f"Path to a pii_policy.yaml file (default: {_DEFAULT_POLICY_PATH}). "
        "Errors are surfaced; nothing is written if the YAML fails to parse.",
    )
    p_policy_apply.add_argument(
        "--store-path",
        default=_DEFAULT_STORE_PATH,
        help=f"Path to the local SQLite store (default: {_DEFAULT_STORE_PATH})",
    )
    p_policy_apply.add_argument(
        "--source",
        default=None,
        help="The same source URL passed to `index`. DEPRECATED when "
        "the URL contains a password; prefer --url-env.",
    )
    p_policy_apply.add_argument(
        "--url-env",
        dest="url_env",
        metavar="VARNAME",
        default=None,
        help="Name of the environment variable that holds the source URL.",
    )
    p_policy_apply.add_argument(
        "--force-catastrophic-downgrade",
        dest="force_catastrophic_downgrade",
        action="store_true",
        help="Permit a column_override that strips a column's always-on "
        "catastrophic-leak protection (credential / payment_card / "
        "government_id). Refused by default; NOT recommended for production.",
    )

    # `policy tag` is a noun group inside `policy` — three actions
    # against the per-column override surface. Operators who edit
    # YAML get the round-trip; operators who prefer one-off CLI
    # commands use these.
    p_policy_tag = policy_sub.add_parser(
        "tag",
        help="Per-column PII tag overrides (operator-asserted)",
    )
    policy_tag_sub = p_policy_tag.add_subparsers(dest="policy_tag_action", required=True)

    p_policy_tag_override = policy_tag_sub.add_parser(
        "override",
        help="Upsert one operator-asserted PII tag override for a column",
    )
    p_policy_tag_override.add_argument(
        "qualified_column",
        help="The column to override in `schema.table.column` form "
        "(e.g. `public.users.email`). Identifier-shape per part.",
    )
    p_policy_tag_override.add_argument(
        "--sensitivity",
        required=True,
        choices=["public", "internal", "confidential", "pii"],
        help="The new sensitivity level. `public` and `internal` are "
        "common downgrade targets for over-tagged columns "
        "(e.g. `card_number_last4` per PCI-DSS Q&A).",
    )
    p_policy_tag_override.add_argument(
        "--categories",
        default="",
        help="Comma-separated PII category list (empty string = none). "
        "Example: --categories=contact,location. Unknown category "
        "names abort with an error.",
    )
    p_policy_tag_override.add_argument(
        "--store-path",
        default=_DEFAULT_STORE_PATH,
        help=f"Path to the local SQLite store (default: {_DEFAULT_STORE_PATH})",
    )
    p_policy_tag_override.add_argument(
        "--source",
        default=None,
        help="The same source URL passed to `index`. DEPRECATED when "
        "the URL contains a password; prefer --url-env.",
    )
    p_policy_tag_override.add_argument(
        "--url-env",
        dest="url_env",
        metavar="VARNAME",
        default=None,
        help="Name of the environment variable that holds the source URL.",
    )
    p_policy_tag_override.add_argument(
        "--force-catastrophic-downgrade",
        dest="force_catastrophic_downgrade",
        action="store_true",
        help="Permit an override that strips a column's always-on "
        "catastrophic-leak protection (credential / payment_card / "
        "government_id). Refused by default; NOT recommended for production.",
    )

    p_policy_tag_clear = policy_tag_sub.add_parser(
        "clear",
        help="Delete an operator-asserted PII tag override for one column. "
        "Does NOT touch the heuristic row; next `schemabrain index` will "
        "re-classify the column from scratch.",
    )
    p_policy_tag_clear.add_argument(
        "qualified_column",
        help="The column whose override to clear in `schema.table.column` form.",
    )
    p_policy_tag_clear.add_argument(
        "--store-path",
        default=_DEFAULT_STORE_PATH,
        help=f"Path to the local SQLite store (default: {_DEFAULT_STORE_PATH})",
    )
    p_policy_tag_clear.add_argument(
        "--source",
        default=None,
        help="The same source URL passed to `index`. DEPRECATED when "
        "the URL contains a password; prefer --url-env.",
    )
    p_policy_tag_clear.add_argument(
        "--url-env",
        dest="url_env",
        metavar="VARNAME",
        default=None,
        help="Name of the environment variable that holds the source URL.",
    )
    p_policy_tag_clear.add_argument(
        "--force-catastrophic-downgrade",
        dest="force_catastrophic_downgrade",
        action="store_true",
        help="Permit clearing an override that would leave a catastrophic "
        "column (credential / payment_card / government_id) untagged. "
        "Refused by default; NOT recommended for production.",
    )

    p_policy_tag_list = policy_tag_sub.add_parser(
        "list",
        help="List PII tag rows with provenance (heuristic vs operator)",
    )
    p_policy_tag_list.add_argument(
        "--origin",
        choices=["heuristic", "operator"],
        default=None,
        help="Filter listing to one origin. Default lists both.",
    )
    p_policy_tag_list.add_argument(
        "--store-path",
        default=_DEFAULT_STORE_PATH,
        help=f"Path to the local SQLite store (default: {_DEFAULT_STORE_PATH})",
    )
    p_policy_tag_list.add_argument(
        "--source",
        default=None,
        help="The same source URL passed to `index`. DEPRECATED when "
        "the URL contains a password; prefer --url-env.",
    )
    p_policy_tag_list.add_argument(
        "--url-env",
        dest="url_env",
        metavar="VARNAME",
        default=None,
        help="Name of the environment variable that holds the source URL.",
    )

    # `dashboard` boots a read-only FastAPI sidecar that serves the
    # bundled Next.js static export. Importing DEFAULT_PORT from the
    # sidecar module is safe here — sidecar.py defers its fastapi /
    # uvicorn imports to call time, so the base wheel can build this
    # parser without the [ui] extra installed.
    from schemabrain.dashboard.sidecar import DEFAULT_PORT as _DASHBOARD_DEFAULT_PORT

    p_dashboard = sub.add_parser(
        "dashboard",
        help="Serve the local read-only dashboard UI (requires `pip install schemabrain[ui]`)",
    )
    p_dashboard.add_argument(
        "--store-path",
        default=_DEFAULT_STORE_PATH,
        help=f"Path to the local SQLite store (default: {_DEFAULT_STORE_PATH}). "
        "The sidecar auto-resolves the canonical source_id from the store.",
    )
    p_dashboard.add_argument(
        "--port",
        type=int,
        default=_DASHBOARD_DEFAULT_PORT,
        help=f"Port to bind on 127.0.0.1 (default: {_DASHBOARD_DEFAULT_PORT}). "
        "The bind host is hardcoded — no public-network exposure flag exists.",
    )
    p_dashboard.add_argument(
        "--no-open",
        dest="open_browser",
        action="store_false",
        default=True,
        help="Skip auto-opening the default browser. CI / headless setups should pass this.",
    )

    p_demo = sub.add_parser(
        "demo",
        help="Zero-setup showcase: sample SaaS data + firewall + dashboard (no API key, "
        "no Docker for the dashboard / CLI paths)",
    )
    g_demo_action = p_demo.add_mutually_exclusive_group()
    g_demo_action.add_argument(
        "--dashboard",
        dest="demo_action",
        action="store_const",
        const="dashboard",
        help="Skip the menu and open the dashboard directly.",
    )
    g_demo_action.add_argument(
        "--showcase",
        dest="demo_action",
        action="store_const",
        const="showcase",
        help="Skip the menu and run the terminal firewall showcase.",
    )
    g_demo_action.add_argument(
        "--wire",
        dest="demo_action",
        action="store_const",
        const="wire",
        help="Skip the menu and wire an MCP host (starts the demo Postgres — needs Docker).",
    )
    p_demo.set_defaults(demo_action=None)
    p_demo.add_argument(
        "--host",
        choices=("claude-desktop", "claude-code", "cursor", "windsurf", "manual"),
        default=None,
        help="Host to wire with --wire (default: auto-detect). `manual` prints the snippet.",
    )
    p_demo.add_argument(
        "--store-path",
        default=None,
        help="Where to build the demo store (default: ~/.schemabrain/demo.db). "
        "Rebuilt fresh on every run.",
    )
    p_demo.add_argument(
        "--port",
        type=int,
        default=_DASHBOARD_DEFAULT_PORT,
        help=f"Dashboard port on 127.0.0.1 (default: {_DASHBOARD_DEFAULT_PORT}).",
    )
    p_demo.add_argument(
        "--no-open",
        dest="open_browser",
        action="store_false",
        default=True,
        help="Skip auto-opening the browser for the dashboard. CI / headless should pass this.",
    )

    return parser


def _refresh_graph_projection(store: SQLiteStore, source_id: str) -> None:
    """Rebuild the v15 graph read-model after a semantic-layer change.

    Idempotent and cheap; safe to call whenever a source's entities /
    joins / PII tags / row-count estimates may have changed (ADR 0010), so
    `GET /api/graph` serves a current projection. Lazy import keeps the
    projection + compiler off the import path of CLI commands that never
    touch the graph.
    """
    from schemabrain.semantic.graph_projection import rebuild_graph_projection

    rebuild_graph_projection(store, source_connection_id=source_id)


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
    since: str | None = None,
    no_pii_classify: bool = False,
) -> int:
    if since is not None and not dry_run:
        print(
            "error: --since requires --dry-run (it is meaningful only "
            "when previewing, not when actually indexing)",
            file=sys.stderr,
        )
        return 2
    if no_pii_classify:
        # One-shot startup warning: operators who opted out of
        # classification need a visible reminder that downstream
        # `get_metric` audit rows will record `pii_categories=''`
        # and `--pii-block` enforcement has nothing to act on.
        print(
            "warning: PII classification disabled — get_metric audit rows "
            "will record pii_categories=''. Use 'schemabrain index' without "
            "--no-pii-classify to populate tags.",
            file=sys.stderr,
        )
    url = _resolve_url_source(
        positional=positional_url,
        url_env=url_env,
        allow_interactive=True,
        interactive_purpose="to index your database",
    )
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
            since=since,
        )

    # API key check happens BEFORE the store opens — failing fast on
    # configuration is friendlier than half-initialising the SQLite
    # file and then aborting. With `allow_interactive=True` a TTY user
    # missing the env var sees a cost-disclosure prompt instead of the
    # guided error; pressing Enter at the prompt falls through to the
    # guided error so the recovery hint still lands.
    api_key: str | None = None
    if not no_enrich:
        api_key = _resolve_anthropic_key_source(
            allow_interactive=True,
            interactive_purpose="enrich column descriptions",
            interactive_cost_estimate_usd=0.02,
            interactive_cap_usd=max_cost_usd,
            interactive_skip_hint="press Enter to abort (or re-run with --no-enrich)",
        )
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
                        # Env-var resolution at call time: operator override
                        # > module-level constant default. `resolve_positive_int_env`
                        # rejects underscore/leading-zero/scientific/negative
                        # footguns; bad env values raise with a clear message
                        # rather than silently mis-tuning concurrency (which
                        # would trigger cascading 429s under tier-1 rate limits).
                        default_concurrency=resolve_positive_int_env(
                            _PIPELINE_DEFAULT_CONCURRENCY_ENV,
                            _PIPELINE_DEFAULT_CONCURRENCY,
                        ),
                        cryptic_concurrency=resolve_positive_int_env(
                            _PIPELINE_CRYPTIC_CONCURRENCY_ENV,
                            _PIPELINE_CRYPTIC_CONCURRENCY,
                        ),
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
                    no_pii_classify=no_pii_classify,
                )
                # Refresh the v15 graph read-model so GET /api/graph picks
                # up the freshly-indexed row-counts + PII snapshot (ADR
                # 0010). Entities/joins are written by `apply`, not `index`,
                # so this is an empty projection until a project is applied.
                _refresh_graph_projection(store, source_id)
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
        if _try_render_llm_failure(
            e,
            retry_command="schemabrain index",
            fallback_command="schemabrain index --no-enrich",
        ):
            return 2
        raise
    elapsed = time.monotonic() - started
    _render_index_done(
        result=result,
        canonical=canonical,
        store_path=store_path,
        elapsed_s=elapsed,
        quiet=quiet,
    )
    if result.tables_seen == 0:
        print(
            "warning: no tables indexed (empty database, or all tables are in "
            "system schemas that were skipped)",
            file=sys.stderr,
        )
    return 0


def _render_index_done(
    *,
    result: IndexResult,
    canonical: str,
    store_path: str,
    elapsed_s: float,
    quiet: bool,
) -> None:
    """Render the post-`index` summary as a styled completion block.

    Under `--quiet` keeps the legacy single-line pipe-delimited format
    so CI scripts that grep stderr for `"Indexed N table(s)"` plus
    `source=... store=... in Ns` machine fields continue to parse.

    Otherwise renders a styled checkmark line on top of the existing
    `IndexResult.summary()` phrasing. The summary string is preserved
    verbatim because every existing test that asserts on this output
    matches substrings of it (`"Indexed N table(s): M changed"` regex,
    etc.).
    """
    if quiet:
        print(
            f"{result.summary()} | source={canonical} store={store_path} in {elapsed_s:.1f}s",
            file=sys.stderr,
        )
        return

    from rich.console import Console

    console = Console(stderr=True)
    console.print(f"[green]✓[/] Indexed [bold]{canonical}[/] in [bold]{elapsed_s:.1f}s[/]")
    # Plain `print` for the canonical summary so the line never
    # soft-wraps mid-substring under capsys/non-TTY rendering — the
    # "Indexed N table(s): M changed, ..." regex tests assert on this
    # phrasing as a contiguous substring, which Rich's word-wrap
    # would otherwise break across visual rows.
    print(f"  {result.summary()}", file=sys.stderr)
    console.print(f"  [dim]Store:[/] {store_path}")


def _cmd_index_dry_run(
    *,
    url: str,
    canonical: str,
    store_path: str,
    no_enrich: bool,
    no_embed: bool,
    quiet: bool,
    since: str | None = None,
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

    When `since` is supplied, a separate freshness-audit line follows
    the standard dry-run summary. The audit counts cached columns
    whose owning table was last indexed before the cutoff and
    estimates the cost to refresh them.
    """
    from schemabrain.errors_render import render_bad_argument_error
    from schemabrain.observability import parse_since

    since_ts: int | None = None
    if since is not None:
        try:
            since_ts = int(parse_since(since).timestamp())
        except ValueError as exc:
            # Caret-underline + alternatives surface (design shape A,
            # handoff bundle ``cli/errors.jsx:ErrBadInput``).
            # ``parse_since`` distinguishes two failure modes:
            # (a) the value is neither a duration nor parseable as
            # ISO 8601 at all; (b) the value IS valid ISO 8601 but
            # has no timezone. The caret leader must reflect WHICH
            # one fired — collapsing both into one "not a duration
            # · not a date" leader actively misleads users who
            # passed a timezone-less ISO timestamp.
            if "must include a timezone" in str(exc):
                reason = "ISO 8601 needs a timezone (e.g. trailing Z)"
            else:
                reason = "not a duration · not a date"
            render_bad_argument_error(
                arg_name="--since",
                raw_value=since,
                reason=reason,
                expected_summary=(
                    "a duration like 14d or an ISO 8601 timestamp "
                    "with timezone like 2026-05-01T00:00:00Z"
                ),
                suggestions=[
                    ("schemabrain index --dry-run --since 7d", "last 7 days"),
                    (
                        "schemabrain index --dry-run --since 2026-05-13T00:00:00Z",
                        "since that ISO timestamp",
                    ),
                ],
                command_prefix="schemabrain index --dry-run",
                console=_stderr_console(),
            )
            return 2

    source_id = _make_source_id(url)
    reporter = _build_index_reporter(quiet=quiet)
    will_enrich = not no_enrich
    will_embed = will_enrich and not no_embed
    started = time.monotonic()
    # Pre-init so the post-try render path can read `freshness` even
    # if a future maintainer adds a catch-all except above; today the
    # only matching `except` clauses return before reaching the render.
    freshness: dict[str, object] | None = None
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
                if since_ts is not None:
                    freshness = _compute_freshness_audit(
                        store=store,
                        source_connection_id=source_id,
                        since_ts=since_ts,
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
    _render_dry_run_report(
        result=result,
        canonical=canonical,
        store_path=store_path,
        elapsed_s=elapsed,
        freshness=freshness,
        since=since,
        quiet=quiet,
    )
    if result.tables_seen == 0:
        print(
            "warning: source has no user-visible tables — dry-run produced an empty diff",
            file=sys.stderr,
        )
    return 0


def _compose_dry_run_panel_body(
    *,
    result: IndexResult,
    store_path: str,
    elapsed_s: float,
) -> Table:
    """Build the body of the dry-run's ``cost estimate`` Panel.

    Returns a Rich ``Table.grid`` laying out the 5-row k/v shape
    the design specifies (tables / columns / cost / embeddings /
    elapsed). Rows for zero-valued LLM / embedding counts are
    omitted so a cost-free dry-run doesn't render hollow rows.
    The ``store`` row's path renders through ``_ui.short_path``
    so a user-home prefix collapses to ``~/`` — consistent with
    the doctor renderer's brand line and the inspect store
    brand line.

    Lives outside ``_render_dry_run_report`` so the body builder
    is independently testable — the renderer just composes the
    returned Table into the surrounding Panel chrome.
    """
    from rich.table import Table

    from schemabrain._ui import short_path

    grid = Table.grid(padding=(0, 2))
    grid.add_column(width=14, style="dim")  # label column (k)
    grid.add_column()  # value column (v)
    grid.add_column(style="bright_black")  # trailer column (meta)

    grid.add_row(
        "tables",
        (
            f"{result.tables_seen} seen "
            f"({result.tables_changed} changed, "
            f"{result.tables_unchanged} unchanged, "
            f"{result.tables_removed} removed)"
        ),
        "",
    )
    grid.add_row(
        "columns",
        f"+{result.columns_added} / ~{result.columns_changed} / -{result.columns_removed}",
        "",
    )
    if result.descriptions_generated > 0:
        grid.add_row(
            "est. cost",
            f"[bold]${result.llm_cost_usd:.4f}[/]",
            f"{result.descriptions_generated} description"
            f"{'' if result.descriptions_generated == 1 else 's'}",
        )
    if result.embeddings_generated > 0:
        grid.add_row(
            "embeddings",
            f"{result.embeddings_generated} estimated",
            "",
        )
    grid.add_row("store", short_path(store_path), "")
    grid.add_row("elapsed", f"{elapsed_s:.1f}s", "")
    return grid


def _render_dry_run_report(
    *,
    result: IndexResult,
    canonical: str,
    store_path: str,
    elapsed_s: float,
    freshness: dict[str, object] | None,
    since: str | None,
    quiet: bool,
) -> None:
    """Render the `index --dry-run` summary.

    Under `--quiet` keeps the legacy single-line pipe-delimited format
    so scripts and CI parsers built against pre-v0.3.x output don't
    break. Otherwise upgrades to a Rich-rendered horizontal rule +
    labelled grid that matches the v1 demo's "Dry-run: <source>"
    visual.

    `result.summary(dry_run=True)` is reused verbatim as the headline
    line so existing test assertions on "Would index N table(s): M
    changed" and "No changes made to the store." stay green. The
    detail rows below are pure additions — they surface the same
    counts the summary already encodes, with whitespace and labels
    so the eye reads them straight down.
    """
    if quiet:
        # Legacy machine-readable line. Kept intact for CI / scripts
        # that grep the dry-run output; the pipe-delimited
        # `source=... store=... in Ns` suffix is the implicit
        # contract those callers rely on.
        print(
            f"{result.summary(dry_run=True)} | source={canonical} store={store_path} in {elapsed_s:.1f}s",
            file=sys.stderr,
        )
        if freshness is not None:
            print(
                f"Stale since {since}: {freshness['stale_columns']} columns "
                f"across {freshness['stale_tables']} tables "
                f"(estimated refresh ${freshness['cost_usd']:.4f})",
                file=sys.stderr,
            )
        return

    # Lazy imports: this Rich-rendered path is only entered when
    # ``--quiet`` is OFF and the dry-run completes. Importing at
    # module top would charge every non-dry-run subcommand for
    # nothing, since the rest of cli.py reaches Rich through the
    # other lazy import sites established across the arc.
    from rich.console import Console
    from rich.panel import Panel
    from rich.text import Text

    from schemabrain._ui import GLYPH_ARROW, GLYPH_BRAND, GLYPH_OK, GLYPH_SEP

    console = Console(stderr=True)

    # Brand line — replaces the old ``console.rule()`` cyan rule with
    # the design's ``◆ plan · <since> · M of N tables`` shape (handoff
    # bundle ``cli/operator.jsx:IndexDryRun``). Source URL is shown
    # in the body because the canonical form already redacts
    # credentials.
    brand = Text()
    brand.append(GLYPH_BRAND, style="cyan")
    brand.append(" ")
    brand.append("plan", style="bold")
    brand.append(f" {GLYPH_SEP} ", style="bright_black")
    if since is not None:
        brand.append(f"--since {since}", style="dim")
        brand.append(f" {GLYPH_SEP} ", style="bright_black")
    brand.append(f"{result.tables_seen} table", style="dim")
    brand.append(f"{'' if result.tables_seen == 1 else 's'}", style="dim")
    console.print(brand)
    console.print(Text(f"  source: {canonical}", style="dim"))
    console.print()

    # Cost-estimate Panel — the design's hero block. 5-row k/v grid
    # capturing what the dry-run actually computed; rows only render
    # when their values are non-zero / meaningful so a no-LLM
    # dry-run shows just the schema-level rows.
    #
    # Title adapts to the run's mode: ``plan summary`` for cost-free
    # dry-runs (the body has no $ rows so a ``cost estimate`` title
    # would mislead), ``cost estimate · haiku`` for ``--enrich``
    # runs where the LLM cost is the load-bearing signal. Matches
    # the design's mock (handoff bundle ``cli/operator.jsx:211``).
    panel_body = _compose_dry_run_panel_body(
        result=result,
        store_path=store_path,
        elapsed_s=elapsed_s,
    )
    panel_title = Text()
    panel_title.append(f"{GLYPH_OK} ", style="green")
    if result.descriptions_generated > 0:
        panel_title.append("cost estimate", style="bold")
        panel_title.append(f" {GLYPH_SEP} ", style="bright_black")
        panel_title.append("haiku", style="dim")
    else:
        panel_title.append("plan summary", style="bold")
    console.print(
        Panel(
            panel_body,
            title=panel_title,
            title_align="left",
            border_style="bright_black",
            padding=(0, 1),
        )
    )
    console.print()
    # Safety affirmation kept as its own line — the "no side-effects"
    # promise is the load-bearing signal of dry-run mode.
    console.print("[dim]No changes made to the store.[/]")
    if freshness is not None:
        console.print()
        # The design's freshness-audit line uses the ``→`` arrow glyph
        # so the eye reads it as the next-action breadcrumb the dry-
        # run is suggesting (refresh stale columns) rather than as
        # a continuation of the cost panel above. Plain `print` so
        # the "estimated refresh $X.XXXX" substring stays contiguous
        # under non-TTY rendering — CI / scripts grep for this
        # exact suffix.
        print(
            f"  {GLYPH_ARROW} freshness audit: stale since {since} · "
            f"{freshness['stale_columns']} columns "
            f"across {freshness['stale_tables']} tables "
            f"(estimated refresh ${freshness['cost_usd']:.4f})",
            file=sys.stderr,
        )


# Per-column estimated refresh cost when computing `--since` freshness
# audit. Same constant the indexer's `dry_run_index` uses for its main
# cost line; kept here as a local alias to avoid importing a private
# module symbol from the indexer.
_FRESHNESS_AVG_COST_PER_COLUMN_USD = 0.0003


def _compute_freshness_audit(
    *,
    store: Store,
    source_connection_id: str,
    since_ts: int,
) -> dict[str, object]:
    """Count cached columns belonging to tables last indexed before `since_ts`.

    Returns a dict with `stale_tables`, `stale_columns`, `cost_usd`.
    Typed against the `Store` Protocol so any future backend (in-memory
    mock, hosted backend) participates without a CLI change. The
    underlying query answers a different question than `dry_run_index`
    ("what is stale in the cache" vs "what has drifted in the source")
    and the two outputs can disagree (a stale cached table may not
    have any source-side drift).
    """
    stale_tables, stale_cols = store.count_stale_tables_and_columns(
        source_connection_id=source_connection_id,
        since_ts=since_ts,
    )
    return {
        "stale_tables": stale_tables,
        "stale_columns": stale_cols,
        "cost_usd": stale_cols * _FRESHNESS_AVG_COST_PER_COLUMN_USD,
    }


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


def _serve_policy_mtime_sentinel_path() -> Path:
    """Return the CWD-relative sentinel path. Sources from
    ``schemabrain.dashboard.sidecar`` so writer (cli) and reader
    (sidecar) share one source of truth — a typo in one site can
    no longer silently disable drift detection."""
    from schemabrain.dashboard.sidecar import SERVE_POLICY_MTIME_SENTINEL_PATH

    return SERVE_POLICY_MTIME_SENTINEL_PATH


def _record_serve_policy_mtime(policy_path: str) -> None:
    """Write a JSON sentinel describing the policy file serve resolved
    against at startup.

    The sidecar reads this sentinel on every ``/api/pii/policy``
    request to detect drift: when the YAML mtime on disk diverges
    from what serve recorded, the dashboard surfaces a "restart
    required" banner. Same posture ``schemabrain policy show``
    already takes — both read the live YAML, but the running
    firewall is frozen at startup.

    The sentinel lives at ``./schemabrain/.serve_policy_mtime``
    (CWD-relative, sibling of ``pii_policy.yaml``). Write is
    best-effort: any ``OSError`` is logged to stderr and serve
    continues — drift detection is observability, not safety.

    Payload schema (JSON object with stable keys):

        {
            "policy_path": "/abs/path/to/pii_policy.yaml",
            "recorded_at_mtime": 1717112233.456 | null,
            "recorded_at_iso": "2026-05-31T12:30:33+00:00",
            "yaml_existed_at_boot": true | false
        }

    ``recorded_at_mtime`` is null when the YAML did not exist at
    boot (the sidecar still surfaces a drift signal if a YAML
    appears later).
    """
    sentinel = _serve_policy_mtime_sentinel_path()
    try:
        sentinel.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(
            f"schemabrain serve: cannot create sentinel directory "
            f"{sentinel.parent}: {exc}. Drift detection disabled.",
            file=sys.stderr,
        )
        return

    path = Path(policy_path).expanduser()
    try:
        resolved_path = str(path.resolve())
    except OSError as exc:
        print(
            f"schemabrain serve: cannot resolve {policy_path}: {exc}. Drift detection disabled.",
            file=sys.stderr,
        )
        return

    try:
        stat = path.stat()
        yaml_existed = True
        mtime: float | None = stat.st_mtime
    except FileNotFoundError:
        yaml_existed = False
        mtime = None
    except OSError as exc:
        print(
            f"schemabrain serve: cannot stat {policy_path}: {exc}. Drift detection disabled.",
            file=sys.stderr,
        )
        return

    payload = {
        "policy_path": resolved_path,
        "recorded_at_mtime": mtime,
        "recorded_at_iso": datetime.now(UTC).isoformat(timespec="seconds"),
        "yaml_existed_at_boot": yaml_existed,
    }
    try:
        sentinel.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    except OSError as exc:
        print(
            f"schemabrain serve: cannot write sentinel {sentinel}: {exc}. "
            f"Drift detection disabled.",
            file=sys.stderr,
        )


def _delete_stale_serve_policy_sentinel() -> None:
    """Remove the sentinel from a prior serve boot.

    Called when the current serve is using the ``--pii-block`` CLI
    flag (default OR explicit), so YAML drift is meaningless — the
    YAML isn't being read, and a leftover sentinel from an earlier
    YAML-driven serve would fire a misleading banner. Best-effort.
    """
    import contextlib

    with contextlib.suppress(OSError):
        _serve_policy_mtime_sentinel_path().unlink(missing_ok=True)


def _try_load_policy_yaml_block(
    policy_path: str | None,
) -> frozenset[PIICategory] | None:  # type: ignore[name-defined]  # noqa: F821 — runtime import
    """Try to load `block:` from `policy_path`. Returns None if the
    file is absent (legitimate — operator may not have created one
    yet). A malformed file is loud — surfaces the parse error to
    stderr and returns None so serve falls back to the default; the
    operator can then either fix the YAML or pass --pii-block
    explicitly to override.

    Lives at module level so the test surface can exercise it
    independently of the full `_cmd_serve` startup.
    """
    from schemabrain.pii.categories import PIICategory  # noqa: F401 — used in return type
    from schemabrain.pii.policy_yaml import (
        PolicyYamlError,
        parse_policy_yaml_file,
    )

    if not policy_path:
        return None
    path = Path(policy_path).expanduser()
    if not path.exists():
        return None
    try:
        policy = parse_policy_yaml_file(path)
    except PolicyYamlError as exc:
        print(
            f"schemabrain serve: cannot parse {policy_path}: {exc}. "
            f"Falling back to default; pass --pii-block to override "
            f"or fix the YAML and restart.",
            file=sys.stderr,
        )
        return None
    return policy.block


def _cmd_serve(
    *,
    positional_url: str | None,
    url_env: str | None,
    store_path: str,
    events_path: str | None = None,
    no_events: bool = False,
    no_audit: bool = False,
    pii_block_csv: str | None = None,
    policy_path: str | None = None,
    statement_timeout_ms: int | None = None,
    max_rows_per_result: int | None = None,
) -> int:
    """Run the MCP server on stdio against the local store.

    Blocks until the client disconnects. The store stays open for the
    lifetime of the process; SQLiteStore is single-process safe and
    handles concurrent reads from FastMCP's async tool dispatch. Tools
    are read-only (no writes occur at MCP call time), so SQLite's
    single-writer limit is never approached.

    `events_path` / `no_events` control the observability bus. When
    `no_events` is False (default), construct a `JsonlEventBus` rooted
    at the resolved path (flag > env > default `~/.schemabrain/events.jsonl`)
    and pass it through. When `no_events` is True, a `NullEventBus` is
    used and no JSONL file is written.

    `no_audit` controls the `mcp_audit` table writer. When False
    (default), an `AuditWriter` is constructed against the same store
    file and each MCP tool call writes one row. When True (or when
    construction fails — same fallback discipline as the events bus),
    audit is disabled for the run.
    """
    import os as _os
    from typing import cast as _cast

    from schemabrain.audit.writer import AuditWriter
    from schemabrain.observability import JsonlEventBus, NullEventBus
    from schemabrain.pii import CATASTROPHIC_LEAK_CATEGORIES, PII_CATEGORIES, PIICategory

    # Three-state contract for `--pii-block`:
    #   - None (flag absent)        → catastrophic-leak default
    #     {credential, payment_card, government_id}. Surfaced to
    #     stderr at startup so the operator sees what's enforced.
    #     Safe-by-default: a zero-config operator following the README
    #     gets enforcement on the categories where no plausible
    #     aggregate-analytics use case justifies exposure.
    #   - "" (explicit empty)       → empty frozenset (escape hatch).
    #     Disables refusal — PII tags still flow to the audit row.
    #     Surfaced as a warning so the operator who reads logs sees
    #     they have OPTED OUT of enforcement.
    #   - "<csv>"                   → parse, validate, use the typed
    #     set. Unknown category names abort with a clear error.
    #
    # The argparse `default=None` is load-bearing — it lets `pii_block_csv == ""`
    # remain a distinguishable escape hatch from "no flag passed".
    pii_block: frozenset[PIICategory]
    if pii_block_csv is None:
        yaml_block = _try_load_policy_yaml_block(policy_path)
        if yaml_block is not None:
            # The catastrophic-leak floor is always-on and CANNOT be
            # dropped via pii_policy.yaml — only the explicit
            # `--pii-block ''` CLI escape hatch disables enforcement.
            # Union the floor so the resolved policy (and the startup
            # message) honestly reflect what the firewall enforces at
            # every gate. A YAML `block: []` is "floor only", NOT
            # "enforcement off" — the prior message lied about that.
            pii_block = yaml_block | CATASTROPHIC_LEAK_CATEGORIES
            print(
                f"schemabrain serve: --pii-block read from "
                f"{policy_path}: "
                f"{','.join(sorted(pii_block))} "
                f"(includes always-on catastrophic-leak floor).",
                file=sys.stderr,
            )
        else:
            pii_block = CATASTROPHIC_LEAK_CATEGORIES
            print(
                "schemabrain serve: --pii-block not passed; defaulting to "
                f"{','.join(sorted(CATASTROPHIC_LEAK_CATEGORIES))} "
                "(use --pii-block '' to disable, --pii-block <csv> to override).",
                file=sys.stderr,
            )
    elif pii_block_csv == "":
        pii_block = frozenset()
        print(
            "warning: --pii-block '' (explicit empty) disables refusal "
            "enforcement. PII tags still flow to the audit row.",
            file=sys.stderr,
        )
    else:
        requested = frozenset(c.strip() for c in pii_block_csv.split(",") if c.strip())
        unknown = requested - PII_CATEGORIES
        if unknown:
            print(
                f"error: --pii-block contains unknown category names: "
                f"{sorted(unknown)}. Valid categories: {sorted(PII_CATEGORIES)}.",
                file=sys.stderr,
            )
            return 2
        pii_block = _cast(frozenset[PIICategory], requested)

    if pii_block and no_audit:
        # Honest disclosure: enforcement still happens (the agent sees
        # the refusal envelope), but the refused row never lands in
        # mcp_audit. Operators relying on audit for compliance need
        # to know this combination is observable-but-not-persistent.
        print(
            "warning: --pii-block active with --no-audit: refusals will be "
            "enforced but not persisted to mcp_audit.",
            file=sys.stderr,
        )

    # Record the YAML state serve resolved against so the dashboard
    # sidecar can detect drift between the running firewall and the
    # operator's edits to pii_policy.yaml. Only meaningful when the
    # YAML is the source of truth — when --pii-block is explicit, the
    # CLI flag overrides YAML and drift is by-definition irrelevant.
    if pii_block_csv is None:
        _record_serve_policy_mtime(policy_path or _DEFAULT_POLICY_PATH)
    else:
        _delete_stale_serve_policy_sentinel()

    source_url = _resolve_url_source(positional=positional_url, url_env=url_env)
    if source_url is None:
        return 2
    if _resolve_url(source_url) is None:
        return 2
    source_id = _make_source_id(source_url)
    if no_events:
        bus: JsonlEventBus | NullEventBus = NullEventBus()
    else:
        resolved_events_path = (
            events_path or _os.environ.get("SCHEMABRAIN_EVENTS_PATH") or _DEFAULT_EVENTS_PATH
        )
        try:
            bus = JsonlEventBus(Path(resolved_events_path).expanduser())
        except OSError as exc:
            # Bus construction fails when the parent dir can't be
            # created (read-only volume, no write perms). The serve
            # process is more useful WITHOUT observability than not
            # at all, so fall back to a no-op bus and warn.
            print(
                f"schemabrain serve: cannot initialise events file at "
                f"{resolved_events_path}: {exc}. Continuing with events "
                f"disabled. Pass --no-events to suppress this warning, "
                f"or --events-path PATH to point at a writable location.",
                file=sys.stderr,
            )
            bus = NullEventBus()

    # Construct the same default embedder the indexer used so query and
    # stored vectors are dimension-compatible. fastembed loads the ONNX
    # model lazily on first call.
    # Build a read-only SQLAlchemy engine for `get_metric` to execute
    # compiled SQL against. Same posture as `PostgresProfiler` from
    # PR #9: `default_transaction_read_only=on` is defense-in-depth on
    # top of the read-only role we already enforce at index time.
    #
    # `statement_timeout` (when set via --statement-timeout-ms) is
    # injected into the connect_args options string rather than the
    # URL query allowlist. The allowlist comment at
    # connectors/_url.py warns that statement_timeout via URL would
    # be operator-overridable and thus bypassable; the connect_args
    # path is the bind-time fence the source role cannot relax.
    #
    # `NullPool` opens a fresh connection per request and discards it
    # on close, matching the introspection connector (postgres.py:54)
    # and the query-log miner (cli.py:_cmd_mine_query_log). This
    # eliminates the pool-state-pollution surface that would become
    # live the moment any future feature issued a `SET` / `SET LOCAL`
    # on a connection. Per-call cost is a few ms; `serve` is bounded
    # by agent turn latency so connection reuse buys nothing observable.
    options_parts = ["-c default_transaction_read_only=on"]
    if statement_timeout_ms is not None:
        options_parts.append(f"-c statement_timeout={statement_timeout_ms}")
    try:
        engine = sqlalchemy.create_engine(
            safe_engine_url(source_url),
            poolclass=NullPool,
            connect_args={"options": " ".join(options_parts)},
        )
    except (sqlalchemy.exc.ArgumentError, ValueError) as exc:  # pragma: no cover — defensive
        print(f"error: cannot construct read-only engine: {exc}", file=sys.stderr)
        return 2

    # `max_rows=0` on the CLI surface means "no cap" (matches the Postgres
    # `statement_timeout=0` convention we also honour). The executor's
    # internal contract is None-means-no-cap; translate at the boundary.
    metric_executor = EngineMetricExecutor(engine, max_rows=max_rows_per_result or None)

    # Construct the audit writer alongside the bus — same fallback
    # posture: an OSError during construction (read-only store dir,
    # missing parent perms) demotes to no-audit + stderr warning. The
    # serve process is more useful without audit than not at all.
    audit_writer: AuditWriter | None
    if no_audit:
        audit_writer = None
    else:
        try:
            audit_writer = AuditWriter(Path(store_path).expanduser())
        except Exception as exc:
            # OSError (permissions / read-only volume) is the common
            # case; SchemaVersionMismatchError, sqlite3.DatabaseError,
            # and corrupted-chain ValueError from the tail-load path
            # all want the same response — fall back to audit-disabled
            # rather than crash serve. The exception class is included
            # in the warning so the operator can distinguish causes.
            print(
                f"schemabrain serve: cannot initialise audit writer at "
                f"{store_path}: {type(exc).__name__}: {exc}. "
                f"Continuing with audit disabled. "
                f"Pass --no-audit to suppress this warning.",
                file=sys.stderr,
            )
            audit_writer = None

    # Optional OpenTelemetry tracing. `init_tracer_from_env` returns
    # None unless BOTH the `schemabrain[otel]` extra is installed AND
    # `OTEL_EXPORTER_OTLP_ENDPOINT` is set in the environment. When
    # either is missing the serve process runs with span emission
    # disabled — same posture as the events bus and audit writer.
    from schemabrain.observability import init_tracer_from_env

    tracer = init_tracer_from_env()

    try:
        with SQLiteStore(store_path) as store:
            run_stdio(
                store=store,
                source_connection_id=source_id,
                embedder=fastembed_default(),
                metric_executor=metric_executor,
                event_bus=bus,
                audit_writer=audit_writer,
                pii_block=pii_block,
                tracer=tracer,
            )
    except SchemaVersionMismatchError as exc:
        # A Claude Desktop launch against a store written by an older
        # schemabrain version would otherwise crash with a raw Python
        # traceback to MCP stderr — Claude Desktop's UI then just
        # shows "Server disconnected" with no actionable hint. Match
        # the inspect/check/doctor pattern and emit a guided block
        # instead — the operator-facing remediation is identical across
        # all four subcommands so operators only have to learn the
        # message once.
        _render_guided(
            GuidedError(
                kind="serve_schema_version_mismatch",
                message=str(exc),
                why="the local store was written by a different schemabrain version "
                "and `schemabrain serve` cannot start against it",
                fix="delete the store file and re-run `schemabrain init` "
                "(or `schemabrain index` if you only need the table-level "
                "structure, not the curated entities/metrics/joins)",
                next_step=f"rm {store_path} && schemabrain init ...",
            )
        )
        return 2
    except sqlite3.DatabaseError as exc:
        # The audit-writer init at line ~2541 already names
        # `sqlite3.DatabaseError` as a "want the same response" case,
        # but the store-open path one level up needs the same
        # handling. A corrupted store file (hard shutdown mid-write,
        # OS-level filesystem damage, truncated WAL) surfaces as
        # DatabaseError, not OSError, and would otherwise bubble up
        # as the same kind of raw traceback the
        # SchemaVersionMismatchError fix above was written to
        # prevent. Emit the same shape of guided block so the
        # operator's
        # recovery path is consistent.
        _render_guided(
            GuidedError(
                kind="serve_store_corrupted",
                message=f"sqlite3.DatabaseError: {exc}",
                why="the local store file is corrupted or unreadable as SQLite "
                "(common causes: hard shutdown mid-write, truncated WAL, "
                "filesystem damage, hand-edited binary)",
                fix="delete the store file and re-run `schemabrain init` to rebuild from scratch",
                next_step=f"rm {store_path} && schemabrain init ...",
            )
        )
        return 2
    except OSError as e:
        # Unwritable directory, missing parent, etc. Surface as a
        # guided block instead of a traceback — Claude Desktop config
        # issues are the most common case here.
        _render_guided(store_path_unwritable(store_path, e))
        return 2
    finally:
        engine.dispose()
    return 0


def _resolve_single_source_id(
    store: SQLiteStore,
    *,
    positional_url: str | None,
    url_env: str | None,
    store_path: str,
) -> tuple[str | None, int]:
    """Resolve the operator's source_id, or auto-pick a single source.

    Three outcomes:
      - Explicit --source/--url-env: parses, validates, returns the
        canonical id (`make_source_id`).
      - Neither flag passed AND exactly one source in the store:
        auto-pick. Lets `policy show` work in the common single-source
        project without requiring the operator to retype a URL they
        already indexed.
      - Neither flag passed AND zero/multiple sources: error out with
        a clear remediation hint.

    Returns `(source_id, exit_code)`. exit_code 0 = success;
    2 = malformed / ambiguous; 1 = no sources.

    Mirrors `_resolve_source_id_or_walk`'s shape but is stricter —
    the policy commands act against a single source and would
    silently break if walked across multiple.
    """
    if positional_url is not None or url_env is not None:
        source_url = _resolve_url_source(positional=positional_url, url_env=url_env)
        if source_url is None:
            return None, 2
        if _resolve_url(source_url) is None:  # pragma: no cover — defensive
            return None, 2
        return _make_source_id(source_url), 0
    source_ids = store.list_distinct_source_connection_ids()
    if not source_ids:
        print(
            f"error: no indexed sources in {store_path!r}. Run `schemabrain index` first.",
            file=sys.stderr,
        )
        return None, 1
    if len(source_ids) > 1:
        print(
            f"error: {len(source_ids)} sources indexed in {store_path!r}; "
            f"pass --source or --url-env to pick one.\n"
            f"  available: {', '.join(source_ids)}",
            file=sys.stderr,
        )
        return None, 2
    return source_ids[0], 0


def _cmd_docs(
    *,
    fmt: str,
    out: str | None,
    positional_url: str | None,
    url_env: str | None,
    store_path: str,
) -> int:
    """Render a data dictionary from the store as Markdown or HTML.

    Store-only reader — no LLM, no live source connection. The
    `--source` / `--url-env` flag is optional and only needed to scope
    the dictionary when the store carries more than one source.

    Exit codes (mirroring `inspect` / `policy show`):
      - 0: rendered successfully (to stdout or `--out`)
      - 1: store has no indexed sources (run `index` first)
      - 2: missing store / `--source`+`--url-env` conflict / malformed
        URL / ambiguous multi-source / schema-version mismatch
    """
    from schemabrain.datadict import build_dictionary, render_html, render_markdown

    store_p = Path(store_path)
    if not store_p.exists():
        _render_guided(
            GuidedError(
                kind="docs_store_missing",
                message=f"store not found at {store_path}",
                why="`schemabrain docs` renders the data dictionary from the "
                "local SQLite store; without a store there is nothing to document",
                fix=f"run `schemabrain index --url-env DBURL --store-path "
                f"{store_path}` to populate it",
                next_step="re-run `schemabrain docs` after `index` completes",
            )
        )
        return 2

    try:
        with SQLiteStore(store_p) as store:
            source_id, rc = _resolve_single_source_id(
                store,
                positional_url=positional_url,
                url_env=url_env,
                store_path=store_path,
            )
            if rc:
                return rc
            assert (
                source_id is not None
            )  # pragma: no cover — defensive; rc gate above caught all None paths
            model = build_dictionary(store=store, source_connection_id=source_id)
    except SchemaVersionMismatchError as exc:
        # Same guided recovery `inspect` gives on a stale store; covered
        # by test_docs_schema_version_mismatch_exits_2.
        _render_guided(
            GuidedError(
                kind="docs_schema_version_mismatch",
                message=str(exc),
                why="the local store was written by a different schemabrain version",
                fix="delete the store file and re-run `schemabrain index`",
                next_step=f"rm {store_path} && schemabrain index --url-env DBURL",
            )
        )
        return 2

    body = render_html(model) if fmt == "html" else render_markdown(model)
    return _write_yaml_body(body, out)


def _cmd_policy_show(
    *,
    store_path: str,
    policy_path: str | None,
    positional_url: str | None,
    url_env: str | None,
) -> int:
    """Print the active PII policy: block set + per-column tag listing.

    Resolves the active block from `policy_path` if present (matches
    what `serve` would do at startup with no `--pii-block` flag),
    else falls back to the catastrophic-leak default. Reads per-column
    tags from the store and renders them grouped by qualified table,
    with origin (heuristic / operator) annotated.
    """
    from schemabrain.pii.categories import (
        CATASTROPHIC_LEAK_CATEGORIES,
    )

    with SQLiteStore(store_path) as store:
        source_id, rc = _resolve_single_source_id(
            store,
            positional_url=positional_url,
            url_env=url_env,
            store_path=store_path,
        )
        if rc:
            return rc
        # `source_id is None` only happens when rc != 0; the type
        # checker doesn't narrow through the helper so re-assert.
        assert (
            source_id is not None
        )  # pragma: no cover — defensive; rc gate above caught all None paths

        yaml_block = _try_load_policy_yaml_block(policy_path)
        if yaml_block is not None:
            block_source = f"yaml ({policy_path})"
            active_block = yaml_block
        else:
            block_source = "catastrophic-leak default"
            active_block = CATASTROPHIC_LEAK_CATEGORIES

        rows = store.list_column_pii_tags_with_origin(source_connection_id=source_id)

    print(f"source:        {source_id}")
    print(f"policy_path:   {policy_path}")
    print(f"block source:  {block_source}")
    print(
        f"active block:  "
        f"{','.join(sorted(active_block)) if active_block else '(empty — operator policy off; floor still enforced)'}"
    )
    print(
        f"catastrophic floor (always-on at describe_* AND get_metric): "
        f"{','.join(sorted(CATASTROPHIC_LEAK_CATEGORIES))}"
    )
    print()
    if not rows:
        print(
            "no PII tags recorded for this source.\n"
            "  next: run `schemabrain index` to populate classifier tags."
        )
        return 0
    print(f"per-column tags ({len(rows)} rows):")
    print()
    current_table: str | None = None
    for qt, col, sens, cats, origin in rows:
        if qt != current_table:
            print(f"  {qt}")
            current_table = qt
        cat_str = ",".join(sorted(cats)) if cats else "-"
        # Verdict attribution: `blocked` = the column's category is in
        # the operator's active policy block. `floor-blocked` = not in
        # the operator's block, but caught by the always-on catastrophic
        # floor — which is enforced at EVERY read gate (`describe_*` AND
        # `get_metric`), so it is genuinely blocked, not "describe-only".
        # The split tells the operator what they can change (blocked) vs
        # what the floor enforces no matter what (floor-blocked).
        in_policy_block = bool(cats & active_block)
        in_effective_block = bool(cats & (active_block | CATASTROPHIC_LEAK_CATEGORIES))
        if in_policy_block:
            verdict = "blocked"
        elif in_effective_block:
            verdict = "floor-blocked"
        else:
            verdict = "allowed"
        marker = "*" if origin == "operator" else " "
        print(f"    {marker} {col:30s} {sens:13s} {cat_str:30s} {origin:9s} {verdict}")
    print()
    print(
        "legend: `*` = operator override · `blocked` = your active policy · "
        "`floor-blocked` = always-on catastrophic floor (can't be disabled) · "
        "verdicts are advisory"
    )
    return 0


def _cmd_policy_apply(
    *,
    yaml_path: str,
    store_path: str,
    positional_url: str | None,
    url_env: str | None,
    force_catastrophic_downgrade: bool = False,
) -> int:
    """Load a pii_policy.yaml file and persist column_overrides to the store.

    The `block` field is NOT persisted — `serve` reads it directly
    from the YAML at startup. Only the per-column overrides need
    store-side persistence because `get_metric` reads tags from the
    store during query resolution.

    Refuses cleanly on parse error; prints a one-line confirmation
    of what was written on success. A column_override that would strip a
    column's catastrophic-leak protection is refused (LB-2) unless
    `force_catastrophic_downgrade` is set; the offending override aborts
    the apply at that point (earlier, safe overrides are already
    persisted — fix the YAML and re-run, which is idempotent).
    """
    from schemabrain.pii.policy import CatastrophicDowngradeError
    from schemabrain.pii.policy_yaml import PolicyYamlError, parse_policy_yaml_file

    try:
        policy = parse_policy_yaml_file(Path(yaml_path).expanduser())
    except FileNotFoundError:
        print(
            f"error: pii_policy YAML not found at {yaml_path!r}.\n"
            f"  next: create the file, or pass a different path. "
            f"Example shape:\n"
            f"    version: 1\n"
            f"    block:\n"
            f"      - credential\n"
            f"      - payment_card\n"
            f"      - government_id\n",
            file=sys.stderr,
        )
        return 2
    except IsADirectoryError:
        print(
            f"error: {yaml_path!r} is a directory; expected a YAML file path",
            file=sys.stderr,
        )
        return 2
    except PolicyYamlError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    with SQLiteStore(store_path) as store:
        source_id, rc = _resolve_single_source_id(
            store,
            positional_url=positional_url,
            url_env=url_env,
            store_path=store_path,
        )
        if rc:
            return rc
        assert (
            source_id is not None
        )  # pragma: no cover — defensive; rc gate above caught all None paths

        for override in policy.column_overrides:
            try:
                store.upsert_column_pii_tag_override(
                    source_connection_id=source_id,
                    qualified_table=override.qualified_table,
                    column_name=override.column_name,
                    sensitivity=override.sensitivity,
                    categories=override.categories,
                    force_catastrophic_downgrade=force_catastrophic_downgrade,
                )
            except CatastrophicDowngradeError as exc:
                print(f"refused: {exc}", file=sys.stderr)
                return 2

    print(
        f"applied {yaml_path}: block={','.join(sorted(policy.block)) or '(empty)'}; "
        f"{len(policy.column_overrides)} column override(s) "
        f"persisted to {store_path} for source {source_id}."
    )
    if policy.column_overrides:
        print("  block set lives in YAML; `serve` reads it at startup.")
    return 0


def _split_qualified_column(value: str) -> tuple[str, str, str] | None:
    """Validate `schema.table.column` shape; return parts or None."""
    parts = value.split(".")
    if len(parts) != 3:
        return None
    schema, table, column = parts
    if not (schema and table and column):
        return None
    return schema, table, column


def _cmd_policy_tag_override(
    *,
    qualified_column: str,
    sensitivity: str,
    categories_csv: str,
    store_path: str,
    positional_url: str | None,
    url_env: str | None,
    force_catastrophic_downgrade: bool = False,
) -> int:
    """Upsert one operator-asserted PII tag override for a column."""
    from typing import cast as _cast

    from schemabrain.pii.categories import PII_CATEGORIES, PIICategory, Sensitivity
    from schemabrain.pii.policy import CatastrophicDowngradeError, ColumnOverride

    parts = _split_qualified_column(qualified_column)
    if parts is None:
        print(
            f"error: qualified_column must be `schema.table.column` "
            f"(three identifier-shaped parts joined by dots); got "
            f"{qualified_column!r}",
            file=sys.stderr,
        )
        return 2
    schema, table, column = parts

    if categories_csv:
        requested = frozenset(c.strip() for c in categories_csv.split(",") if c.strip())
    else:
        requested = frozenset()
    unknown = requested - PII_CATEGORIES
    if unknown:
        print(
            f"error: --categories contains unknown values: {sorted(unknown)}.\n"
            f"  valid: {sorted(PII_CATEGORIES)}",
            file=sys.stderr,
        )
        return 2
    typed_categories = _cast(frozenset[PIICategory], requested)
    typed_sensitivity = _cast(Sensitivity, sensitivity)

    # Validate via the dataclass to share the qualified-column shape
    # check with the YAML path (so CLI + YAML errors stay symmetric).
    try:
        ColumnOverride(
            qualified_column=qualified_column,
            sensitivity=typed_sensitivity,
            categories=typed_categories,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    with SQLiteStore(store_path) as store:
        source_id, rc = _resolve_single_source_id(
            store,
            positional_url=positional_url,
            url_env=url_env,
            store_path=store_path,
        )
        if rc:
            return rc
        assert (
            source_id is not None
        )  # pragma: no cover — defensive; rc gate above caught all None paths

        try:
            store.upsert_column_pii_tag_override(
                source_connection_id=source_id,
                qualified_table=f"{schema}.{table}",
                column_name=column,
                sensitivity=typed_sensitivity,
                categories=typed_categories,
                force_catastrophic_downgrade=force_catastrophic_downgrade,
            )
        except CatastrophicDowngradeError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 2

    cat_str = ",".join(sorted(typed_categories)) if typed_categories else "(empty)"
    print(
        f"override written: {qualified_column} -> "
        f"sensitivity={typed_sensitivity}, categories={cat_str} "
        f"(origin=operator)"
    )
    return 0


def _cmd_policy_tag_clear(
    *,
    qualified_column: str,
    store_path: str,
    positional_url: str | None,
    url_env: str | None,
    force_catastrophic_downgrade: bool = False,
) -> int:
    """Delete an operator-asserted PII tag override for one column."""
    from schemabrain.pii.policy import CatastrophicDowngradeError

    parts = _split_qualified_column(qualified_column)
    if parts is None:
        print(
            f"error: qualified_column must be `schema.table.column`; got {qualified_column!r}",
            file=sys.stderr,
        )
        return 2
    schema, table, column = parts

    with SQLiteStore(store_path) as store:
        source_id, rc = _resolve_single_source_id(
            store,
            positional_url=positional_url,
            url_env=url_env,
            store_path=store_path,
        )
        if rc:
            return rc
        assert (
            source_id is not None
        )  # pragma: no cover — defensive; rc gate above caught all None paths

        try:
            deleted = store.delete_column_pii_tag_override(
                source_connection_id=source_id,
                qualified_table=f"{schema}.{table}",
                column_name=column,
                force_catastrophic_downgrade=force_catastrophic_downgrade,
            )
        except CatastrophicDowngradeError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 2

    if deleted:
        print(
            f"override cleared: {qualified_column}. Next `schemabrain index` "
            f"run will re-classify the column from the heuristic rules."
        )
        return 0
    print(
        f"no operator override found for {qualified_column}; nothing to clear.\n"
        f"  (heuristic rows are not affected by `policy tag clear` — that's by design.)",
        file=sys.stderr,
    )
    return 1


def _cmd_policy_tag_list(
    *,
    origin: str | None,
    store_path: str,
    positional_url: str | None,
    url_env: str | None,
) -> int:
    """List PII tag rows with provenance."""
    with SQLiteStore(store_path) as store:
        source_id, rc = _resolve_single_source_id(
            store,
            positional_url=positional_url,
            url_env=url_env,
            store_path=store_path,
        )
        if rc:
            return rc
        assert (
            source_id is not None
        )  # pragma: no cover — defensive; rc gate above caught all None paths

        rows = store.list_column_pii_tags_with_origin(
            source_connection_id=source_id,
            origin=origin,
        )

    if not rows:
        if origin:
            print(f"no {origin} PII tags for source {source_id}.")
        else:
            print(f"no PII tags for source {source_id}.")
        return 0
    print(f"source: {source_id} ({len(rows)} tag(s))")
    print()
    print(f"  {'qualified_column':50s} {'sensitivity':13s} {'categories':30s} origin")
    print(f"  {'-' * 50} {'-' * 13} {'-' * 30} {'-' * 9}")
    for qt, col, sens, cats, row_origin in rows:
        cat_str = ",".join(sorted(cats)) if cats else "-"
        qualified = f"{qt}.{col}"
        print(f"  {qualified:50s} {sens:13s} {cat_str:30s} {row_origin}")
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
    not a SchemaBrain bug).

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


def _expand_yaml_paths(yaml_paths: list[str]) -> tuple[list[Path], list[tuple[str, str]]]:
    """Resolve a list of file-or-directory paths into a flat sorted list of YAML files.

    Each input path may be a file (used as-is when its suffix is
    `.yaml`/`.yml`, otherwise reported as a non-yaml error) OR a
    directory (every `.yaml`/`.yml` in the immediate children is
    included; subdirectories are NOT walked recursively to keep the
    expansion predictable — operators stage YAML in one folder per
    kind). Missing paths surface as failures, not exceptions.

    Returns `(yaml_files, failures)`. `failures` is a list of
    `(path, message)` tuples for the apply loop to surface alongside
    its own per-file failures; the caller decides whether to exit
    early or continue with the successfully-resolved files.
    """
    yaml_files: list[Path] = []
    failures: list[tuple[str, str]] = []
    seen: set[Path] = set()
    for raw in yaml_paths:
        path = Path(raw)
        if path.is_dir():
            try:
                children = list(path.iterdir())
            except OSError as exc:
                # PermissionError (read-protected dir) and any other
                # iterdir-side OS error becomes a failure entry rather
                # than crashing the CLI with a raw traceback. The
                # `is_dir()` check above already passed, so this is
                # an access / FS-level issue, not a missing-path one.
                failures.append((raw, f"could not list directory {raw!r}: {exc}"))
                continue
            dir_files = sorted(
                p for p in children if p.is_file() and p.suffix.lower() in (".yaml", ".yml")
            )
            if not dir_files:
                failures.append((raw, f"no `.yaml`/`.yml` files found in directory {raw!r}"))
                continue
            for f in dir_files:
                resolved = f.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                # Store the original path (not `resolved`) so the apply
                # loop's error messages reference the path the user
                # actually typed; `resolved` is dedup-only.
                yaml_files.append(f)
        elif path.is_file():
            if path.suffix.lower() not in (".yaml", ".yml"):
                failures.append((raw, f"{raw!r} is not a `.yaml`/`.yml` file"))
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            yaml_files.append(path)
        else:
            failures.append((raw, f"{raw!r} is not a file or directory"))
    return yaml_files, failures


def _cmd_entities_apply(
    *,
    yaml_paths: list[str],
    positional_url: str | None,
    url_env: str | None,
    store_path: str,
) -> int:
    """Load entity YAML file(s) or directory(ies) into the local store.

    Non-interactive by design — this is the deterministic file-to-
    store operation. `entities suggest --apply` is the LLM-suggest
    write path; both share the same `Store.write_entity` call.

    `yaml_paths` is a list of one or more paths; each item may be a
    single YAML file OR a directory containing YAMLs (only the
    immediate children are scanned). Shell globs that expand to
    multiple paths land here as multiple list elements. Per-file
    failures (parse / FK / dbt-guard) don't block the rest — the
    function aggregates failures and reports them in a summary.

    Exit codes:
      - 0: every file applied cleanly
      - 1: at least one file failed (parse / FK / dbt-guard /
        bad path / non-yaml extension)
      - 2: structural (URL missing, unwritable store, store-level
        DatabaseError other than IntegrityError)
    """
    source_url = _resolve_url_source(positional=positional_url, url_env=url_env)
    if source_url is None:
        return 2
    if _resolve_url(source_url) is None:
        return 2
    source_id = _make_source_id(source_url)

    yaml_files, path_failures = _expand_yaml_paths(yaml_paths)
    if not yaml_files and not path_failures:  # pragma: no cover — defensive
        # nargs="+" guarantees ≥ 1 path, _expand_yaml_paths routes
        # invalid paths to failures, so reaching here means a caller
        # bypassed argparse. Treat as a structural error.
        print("error: no entity YAML paths provided", file=sys.stderr)
        return 2

    applied: list[str] = []
    failures: list[tuple[str, str]] = list(path_failures)

    try:
        with SQLiteStore(store_path) as store:
            for yaml_file in yaml_files:
                try:
                    entity = parse_entity_yaml_file(yaml_file)
                except (
                    FileNotFoundError,
                    IsADirectoryError,
                ) as exc:  # pragma: no cover — _expand_yaml_paths already filters non-files; race-only path
                    failures.append((str(yaml_file), str(exc)))
                    continue
                except EntityParseError as exc:
                    failures.append((str(yaml_file), str(exc)))
                    continue

                try:
                    store.write_entity(entity, source_connection_id=source_id)
                    applied.append(entity.name)
                except DbtOwnedEntityError as exc:
                    failures.append((str(yaml_file), str(exc)))
                except sqlite3.IntegrityError:
                    # The bound-table FK is the only IntegrityError
                    # this call path can raise — keep the guided
                    # message pointing the user at `schemabrain index`.
                    failures.append(
                        (
                            str(yaml_file),
                            f"entity {entity.name!r} binds to table "
                            f"{entity.qualified_table!r} which isn't indexed "
                            f"for this source. Run `schemabrain index` first "
                            f"to make the table available, then re-run "
                            f"`entities apply`.",
                        )
                    )
                except sqlite3.DatabaseError as exc:
                    # Non-Integrity DB-level errors (disk full, WAL
                    # checkpoint failure, CHECK on a corrupted store)
                    # are structural — exit 2 immediately rather than
                    # continuing the loop with a broken store. Flush
                    # the per-file summary first so the user can see
                    # which files DID land before the structural error
                    # (e.g. "applied 5 of 10 then disk filled"); the
                    # single-file predecessor had no summary to flush
                    # but multi-file callers lose real applied-file
                    # confirmation if we skip this.
                    for name in applied:
                        print(f"applied entity: {name}")
                    for file_str, message in failures:
                        print(f"error in {file_str}: {message}", file=sys.stderr)
                    _render_guided(
                        GuidedError(
                            kind="entities_apply_store_error",
                            message=f"store-level error during write: {exc}",
                            why="the SQLite store reported an error other than a foreign-key violation",
                            fix="check the store file integrity, available disk "
                            "space, and that no other SchemaBrain process is "
                            "writing to the same store",
                            next_step=f"inspect {store_path} with `sqlite3 .schema`",
                        )
                    )
                    return 2
            # Refresh the v15 graph read-model so GET /api/graph reflects
            # the applied entities (ADR 0010). Idempotent; only when at
            # least one entity landed.
            if applied:
                _refresh_graph_projection(store, source_id)
    except OSError as e:
        _render_guided(store_path_unwritable(store_path, e))
        return 2

    for name in applied:
        print(f"applied entity: {name}")
    for file_str, message in failures:
        print(f"error in {file_str}: {message}", file=sys.stderr)

    return 1 if failures else 0


def _format_trust(inference_method: str, validation_state: str) -> str:
    """Render the charter v1.2 2D trust signal as `<method> · <state> (<CONF>)`.

    Lazily imports `derive_confidence` to keep the envelope module
    (and its Pydantic transitive deps) off the import path of CLI
    commands that don't render trust.
    """
    from schemabrain.mcp.envelope import derive_confidence

    confidence = derive_confidence(inference_method, validation_state)  # type: ignore[arg-type]
    return f"{inference_method} · {validation_state} ({confidence})"


def _cmd_entities_list(
    *,
    store_path: str,
    positional_url: str | None,
    url_env: str | None,
) -> int:
    """List entities in the store, pretty-printed.

    With `--source` / `--url-env` filter to one source. Without
    either, lists every entity across every source. The verification
    path after `entities apply` — symmetric with `joins list` and
    `metrics list` so the three semantic-layer surfaces share one
    discovery shape.

    Exit codes:
      0: success (empty list is success, not an error)
      2: structural (unwritable store path or URL-source mismatch)
    """
    source_id: str | None = None
    if positional_url is not None or url_env is not None:
        source_url = _resolve_url_source(positional=positional_url, url_env=url_env)
        if source_url is None:
            return 2
        # `_resolve_url_source` already validated; the second call is
        # defensive — same shape as `_cmd_joins_list` / `_cmd_metrics_list`.
        if _resolve_url(source_url) is None:  # pragma: no cover
            return 2  # pragma: no cover
        source_id = _make_source_id(source_url)

    try:
        with SQLiteStore(store_path) as store:
            entities = store.list_entities(source_connection_id=source_id)
    except OSError as e:
        _render_guided(store_path_unwritable(store_path, e))
        return 2
    except ValueError as exc:
        # Symmetric with `_cmd_metrics_list`: `list_entities` re-runs
        # `Entity.__post_init__` invariants on each row, so a corrupt
        # row (hand-edited store, invalid origin, non-identifier name)
        # surfaces as a plain `ValueError`. Wrap with store-path
        # context so the user sees "your store appears corrupt" rather
        # than a raw traceback.
        print(
            f"error: failed to read entities from {store_path!r}: store appears corrupt ({exc})",
            file=sys.stderr,
        )
        return 2

    if not entities:
        # Mirror the metrics-list empty-state: tell the operator the next
        # command instead of dead-ending on a parenthetical.
        print("(no entities in the store)")
        print(
            "  next: hand-author `<entity>.yaml` files and run "
            "`schemabrain entities apply ./entities`, or run "
            "`schemabrain entities suggest --out-dir ./entities` to propose "
            "them from the indexed schema first. (Index the source before "
            "either: `schemabrain index --url-env DBURL`.)"
        )
        return 0

    for entity in entities:
        trust = _format_trust(entity.inference_method, entity.validation_state)
        print(
            f"{entity.name}  "
            f"table={entity.binding.qualified_table}  "
            f"identity={entity.identity}  "
            f"origin={entity.origin}  "
            f"trust={trust}"
        )
    return 0


def _cmd_entities_export(
    *,
    name: str,
    out: str | None,
    store_path: str,
    positional_url: str | None,
    url_env: str | None,
) -> int:
    """Render one entity as apply-ready YAML on stdout or to `--out PATH`.

    Cross-source posture mirrors `metrics show`: without a source flag
    the handler walks every source the store knows about. If exactly
    one row matches the name → emit it. If zero → exit 1 with a list
    hint. If multiple → exit 2 with a disambiguation hint naming the
    source-id prefixes so the operator can re-run with `--source`.

    Exit codes:
      0: one entity emitted
      1: no entity with that name in any source the store knows about
      2: store path missing / corrupt, URL conflict, or multi-source
         collision without explicit `--source`/`--url-env`
    """
    from schemabrain.entities.yaml_grammar import entity_to_yaml

    source_id, rc = _resolve_source_id_or_walk(positional_url, url_env)
    if rc:
        return rc

    try:
        with SQLiteStore(store_path) as store:
            entity: Entity | None
            if source_id is not None:
                entity = store.get_entity(name, source_connection_id=source_id)
                if entity is None:
                    print(
                        f"error: no entity named {name!r} for source "
                        f"{source_id!r} in {store_path!r}",
                        file=sys.stderr,
                    )
                    return 1
            else:
                source_ids = sorted(_list_source_ids_with_entity(store, name))
                if not source_ids:
                    print(
                        f"error: no entity named {name!r} in {store_path!r}\n"
                        f"  next: run `schemabrain entities list` to see what is curated.",
                        file=sys.stderr,
                    )
                    return 1
                if len(source_ids) > 1:
                    print(
                        f"error: entity {name!r} is defined in {len(source_ids)} sources: "
                        f"{source_ids}. Re-run with --source/--url-env to disambiguate.",
                        file=sys.stderr,
                    )
                    return 2
                entity = store.get_entity(name, source_connection_id=source_ids[0])
                if entity is None:  # pragma: no cover — concurrent-writer race
                    print(
                        f"error: entity {name!r} no longer present in source {source_ids[0]!r}",
                        file=sys.stderr,
                    )
                    return 1
    except OSError as e:  # pragma: no cover — disk/permission failure not simulatable in CI
        _render_guided(store_path_unwritable(store_path, e))
        return 2

    return _write_yaml_body(entity_to_yaml(entity) + "\n", out)


def _cmd_entities_export_all(
    *,
    out_dir: str,
    store_path: str,
    positional_url: str | None,
    url_env: str | None,
) -> int:
    """Write one apply-ready YAML per entity into `--dir`.

    Refuses to overwrite existing `<entity>.yaml` files (preserves
    hand-edits) and refuses on cross-source name collisions when no
    `--source` is passed (otherwise two sources holding the same
    entity name would clobber the same filename).

    Exit codes:
      0: success (empty store is success, not error)
      2: store-path / URL / filesystem error, or collision refusal
    """
    from schemabrain.entities.yaml_grammar import entity_to_yaml

    source_id, rc = _resolve_source_id_or_walk(positional_url, url_env)
    if rc:
        return rc

    try:
        with SQLiteStore(store_path) as store:
            entities = store.list_entities(source_connection_id=source_id)
    except OSError as e:  # pragma: no cover — disk/permission failure not simulatable in CI
        _render_guided(store_path_unwritable(store_path, e))
        return 2

    return _bulk_export_yaml_files(
        items=entities,
        out_dir=out_dir,
        name_attr="name",
        serializer=entity_to_yaml,
        noun_singular="entity",
        noun_plural="entities",
        scope_has_source=source_id is not None,
    )


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
    source_url = _resolve_url_source(
        positional=positional_url,
        url_env=url_env,
        allow_interactive=True,
        interactive_purpose="to suggest entities for",
    )
    if source_url is None:
        return 2
    if _resolve_url(source_url) is None:  # pragma: no cover — defensive
        return 2
    source_id = _make_source_id(source_url)

    # Resolve the cost ceiling: CLI flag > env var > default. Delegates
    # to the shared `_env` parser with `on_invalid="raise"` so a typo'd
    # env var (e.g. "1_000" — Python's `float()` would silently coerce
    # to 1000) gets caught at the boundary and translated into the
    # standard CLI guided-error block (vs the wizard path, which uses
    # `on_invalid="warn_and_default"` because an interactive run
    # shouldn't abort on a leftover env var).
    if max_cost_usd is None:
        try:
            max_cost_usd = resolve_positive_float_env(
                _SUGGEST_COST_ENV_VAR,
                _DEFAULT_SUGGEST_MAX_COST_USD,
            )
        except ValueError as exc:
            _render_guided(
                GuidedError(
                    kind="suggest_cost_env_malformed",
                    message=str(exc),
                    why="cost ceiling must be a positive float (USD)",
                    fix=f"unset {_SUGGEST_COST_ENV_VAR} or set it to a positive "
                    f"number without underscores or scientific notation "
                    f"(e.g. {_SUGGEST_COST_ENV_VAR}=0.50)",
                    next_step="see `schemabrain entities suggest --help`",
                )
            )
            return 2

    # Build the LLM client. Stub reads canned YAML from env (so the
    # multi-line response stays out of argv). Anthropic reads
    # ANTHROPIC_API_KEY via the shared resolver — same env source as
    # `index`, with interactive prompt-on-miss when stderr is a TTY.
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
        api_key = _resolve_anthropic_key_source(
            allow_interactive=True,
            interactive_purpose="suggest entities",
            interactive_cost_estimate_usd=0.01,
            interactive_cap_usd=max_cost_usd,
            interactive_skip_hint="press Enter to abort (or re-run with --provider stub)",
        )
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

    # F1: wrap the LLM round-trip in a cost preamble + spinner so the
    # operator sees what's being spent BEFORE the ~20s wait. Skipped
    # for `--provider stub` (returns instantly; preamble's cost framing
    # would be misleading) and auto-suppressed on non-TTY stderr.
    if provider == "stub":
        progress_ctx: AbstractContextManager[None] = contextlib.nullcontext()
    else:
        progress_ctx = _suggest_llm_progress(
            action=f"identify business entities ({len(tables)} tables)",
            model="claude-sonnet-4-6",
            cost_estimate_usd=0.01,
            cap_usd=max_cost_usd,
        )
    try:
        with progress_ctx:
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
    except Exception as exc:
        # Narrow handler: only the LLM round-trip is inside this try
        # (table load + apply happen outside). An Anthropic SDK error
        # surfacing here is the F5 scenario — render Shape C and exit
        # cleanly. Anything not classified by the renderer (local
        # programming bugs) propagates so the user sees the traceback.
        if _try_render_llm_failure(
            exc,
            retry_command="schemabrain entities suggest",
            fallback_command=None,
        ):
            return 2
        raise

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

    Thin wrapper around `entities.yaml_grammar.entity_to_yaml` so the
    suggest-out-dir path and the `entities export` command share a
    single serialiser; a future grammar change lands in one place.
    """
    from schemabrain.entities.yaml_grammar import entity_to_yaml

    return entity_to_yaml(candidate.entity)


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
                    # Persist the model's self-rating (bind_confidence +
                    # rationale) alongside the clean entity. The file-
                    # review workflow (`suggest` → edit → `apply`) keeps
                    # these as YAML comments and resets them to NULL on
                    # apply; the direct `--apply` path is the one that
                    # persists them — see `to_persisted_entity`.
                    store.write_entity(
                        candidate.to_persisted_entity(), source_connection_id=source_id
                    )
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


def _partial_write_message(
    written: list[str],
    total: int,
    error: str,
    *,
    item_label: str = "entities",
) -> str:
    """Prefix an apply-mode error with the count of items that landed.

    Per-item writes commit independently (each is its own SQLite
    transaction), so a failure mid-loop leaves the store in a partial-
    write state. The user needs to know which items landed and which
    didn't so they can re-run cleanly.

    `item_label` names the kind of item being written — "entities" for
    `_render_apply` (the entity-suggest path), "metrics" for
    `_render_metrics_apply`. Without the label, a metrics-apply error
    would falsely say "N of M entities were written", confusing anyone
    debugging a partially applied metric batch.
    """
    if not written:
        return error
    return (
        f"{len(written)} of {total} {item_label} were written before this "
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
    source_url = _resolve_url_source(
        positional=positional_url,
        url_env=url_env,
        allow_interactive=True,
        interactive_purpose="to mine canonical joins for",
    )
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
    yaml_paths: list[str],
    positional_url: str | None,
    url_env: str | None,
    store_path: str,
) -> int:
    """Load canonical-join YAML file(s) or directory(ies) into the local store.

    `yaml_paths` accepts one or more files OR directories; directories
    are expanded to their immediate `.yaml`/`.yml` children. Shell
    globs that expand to multiple paths land here as multiple list
    elements. Per-file failures aggregate into a summary; an error in
    one file does NOT block the rest.

    Exit codes:
      0: every file applied cleanly
      1: at least one file failed (parse / FK violation / bad path)
      2: structural (URL missing, unwritable store)
    """
    source_url = _resolve_url_source(positional=positional_url, url_env=url_env)
    if source_url is None:
        return 2
    if _resolve_url(source_url) is None:  # pragma: no cover — defensive
        return 2
    source_id = _make_source_id(source_url)

    yaml_files, path_failures = _expand_yaml_paths(yaml_paths)
    if not yaml_files and not path_failures:  # pragma: no cover — defensive
        # nargs="+" guarantees ≥ 1 path; reaching here means a caller
        # bypassed argparse.
        print("error: no canonical-join YAML paths provided", file=sys.stderr)
        return 2

    applied: list[str] = []
    failures: list[tuple[str, str]] = list(path_failures)

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
            # Refresh the v15 graph read-model so GET /api/graph reflects
            # the applied joins (ADR 0010). Idempotent; only when at least
            # one join landed.
            if applied:
                _refresh_graph_projection(store, source_id)
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
        # Mirror the metrics-list empty-state: name the next command.
        print("(no canonical joins in the store)")
        print(
            "  next: hand-author `<join>.yaml` files and run "
            "`schemabrain joins apply ./joins`, or run "
            "`schemabrain joins suggest --out-dir ./joins` to mine them from "
            "FK constraints first. Joins reference entities, so apply "
            "entities before joins."
        )
        return 0

    for join in joins:
        on_summary = ", ".join(f"{p.source_column} ↔ {p.target_column}" for p in join.on)
        trust = _format_trust(join.inference_method, join.validation_state)
        print(
            f"{join.name}  "
            f"{join.source_entity} → {join.target_entity}  "
            f"[{on_summary}]  origin={join.origin}  "
            f"trust={trust}"
        )
    return 0


def _cmd_joins_export(
    *,
    name: str,
    out: str | None,
    store_path: str,
    positional_url: str | None,
    url_env: str | None,
) -> int:
    """Render one canonical join as apply-ready YAML on stdout or `--out PATH`.

    Cross-source posture matches `entities export` / `metrics export`.
    """
    from schemabrain.joins.yaml_grammar import canonical_join_to_yaml

    source_id, rc = _resolve_source_id_or_walk(positional_url, url_env)
    if rc:
        return rc

    try:
        with SQLiteStore(store_path) as store:
            join: CanonicalJoin | None
            if source_id is not None:
                join = store.get_canonical_join(name, source_connection_id=source_id)
                if join is None:
                    print(
                        f"error: no canonical join named {name!r} for source "
                        f"{source_id!r} in {store_path!r}",
                        file=sys.stderr,
                    )
                    return 1
            else:
                source_ids = sorted(_list_source_ids_with_join(store, name))
                if not source_ids:
                    print(
                        f"error: no canonical join named {name!r} in {store_path!r}\n"
                        f"  next: run `schemabrain joins list` to see what is curated.",
                        file=sys.stderr,
                    )
                    return 1
                if len(source_ids) > 1:
                    print(
                        f"error: canonical join {name!r} is defined in "
                        f"{len(source_ids)} sources: {source_ids}. "
                        f"Re-run with --source/--url-env to disambiguate.",
                        file=sys.stderr,
                    )
                    return 2
                join = store.get_canonical_join(name, source_connection_id=source_ids[0])
                if join is None:  # pragma: no cover — concurrent-writer race
                    print(
                        f"error: canonical join {name!r} no longer present "
                        f"in source {source_ids[0]!r}",
                        file=sys.stderr,
                    )
                    return 1
    except OSError as e:  # pragma: no cover — disk/permission failure not simulatable in CI
        _render_guided(store_path_unwritable(store_path, e))
        return 2

    return _write_yaml_body(canonical_join_to_yaml(join) + "\n", out)


def _cmd_joins_export_all(
    *,
    out_dir: str,
    store_path: str,
    positional_url: str | None,
    url_env: str | None,
) -> int:
    """Write one apply-ready YAML per canonical join into `--dir`.

    Refuses on cross-source name collisions and existing files, same
    contract as `_cmd_entities_export_all`.
    """
    from schemabrain.joins.yaml_grammar import canonical_join_to_yaml

    source_id, rc = _resolve_source_id_or_walk(positional_url, url_env)
    if rc:
        return rc

    try:
        with SQLiteStore(store_path) as store:
            joins = store.list_canonical_joins(source_connection_id=source_id)
    except OSError as e:  # pragma: no cover — disk/permission failure not simulatable in CI
        _render_guided(store_path_unwritable(store_path, e))
        return 2

    return _bulk_export_yaml_files(
        items=joins,
        out_dir=out_dir,
        name_attr="name",
        serializer=canonical_join_to_yaml,
        noun_singular="canonical join",
        noun_plural="canonical joins",
        scope_has_source=source_id is not None,
    )


# ----- metrics CLI commands --------------------------------------------------
#
# Mirrors `_cmd_entities_apply` / `_cmd_joins_apply`. Single-file + directory
# modes for apply; the dbt-owned-metric guard surfaces as a user-facing
# exit-1 message naming the metric.


def _cmd_metrics_apply(
    *,
    yaml_paths: list[str],
    positional_url: str | None,
    url_env: str | None,
    store_path: str,
) -> int:
    """Load metric YAML file(s) or directory(ies) into the local store.

    `yaml_paths` accepts one or more files OR directories; directories
    are expanded to their immediate `.yaml`/`.yml` children. Shell
    globs that expand to multiple paths land here as multiple list
    elements. Per-file failures aggregate into a summary; an error in
    one file does NOT block the rest.

    Exit codes:
      0: every file applied cleanly
      1: at least one file failed (parse / FK violation / dbt-owned /
        bad path)
      2: structural (URL missing, unwritable store)
    """
    source_url = _resolve_url_source(positional=positional_url, url_env=url_env)
    if source_url is None:
        return 2
    if _resolve_url(source_url) is None:  # pragma: no cover — defensive
        return 2
    source_id = _make_source_id(source_url)

    yaml_files, path_failures = _expand_yaml_paths(yaml_paths)
    if not yaml_files and not path_failures:  # pragma: no cover — defensive
        # nargs="+" guarantees ≥ 1 path; reaching here means a caller
        # bypassed argparse.
        print("error: no metric YAML paths provided", file=sys.stderr)
        return 2

    applied: list[str] = []
    failures: list[tuple[str, str]] = list(path_failures)

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
        # Mirror the MCP `list_metrics` tool's empty-state hint: tell
        # the operator how to populate the surface rather than dead-
        # ending with a parenthetical. The CLI used to print only
        # `(no metrics in the store)` and the operator had to guess
        # the next command.
        print("(no metrics in the store)")
        print(
            "  next: run `schemabrain metrics suggest --out-dir ./metrics` "
            "to propose metrics from the indexed entities, then "
            "`schemabrain metrics apply ./metrics` to persist."
        )
        return 0

    for metric in metrics:
        grains = ",".join(metric.time_grains) if metric.time_grains else "(non-temporal)"
        time_dim = metric.time_dimension or "—"
        # `measure.column` and `measure.expression` are mutually
        # exclusive — render whichever is populated so composite
        # metrics show their expression instead of `sum(None)`.
        measure_body = (
            metric.measure.column
            if metric.measure.column is not None
            else metric.measure.expression
        )
        trust = _format_trust(metric.inference_method, metric.validation_state)
        print(
            f"{metric.name}  "
            f"entity={metric.entity}  "
            f"{metric.measure.agg}({measure_body})  "
            f"time_dim={time_dim}  "
            f"grains={grains}  "
            f"origin={metric.origin}  "
            f"trust={trust}"
        )
    return 0


def _cmd_metrics_audit(
    *,
    store_path: str,
    positional_url: str | None,
    url_env: str | None,
    fix: bool,
) -> int:
    """Scan applied metrics for the anti-pattern phrases that
    `metrics suggest` blocks at suggest-time, optionally deleting them.

    Read-only without `--fix`: list every flagged metric with the
    matched phrase, exit 0 if clean / 1 if any flagged. CI can fail a
    build on a store with bad metrics by running the read-only path.

    With `--fix`: delete every non-dbt-owned finding. dbt-owned
    metrics are listed but not removed — the upstream dbt repo is the
    source of truth, and a local deletion would just drift back in on
    the next `schemabrain import dbt --include-metrics`.

    Exit codes:
      0: audit clean OR audit found+fixed every removable finding
      1: audit found flagged metrics and `--fix` was not given
      2: structural (unwritable store / unreadable URL / corrupt row)
    """
    from schemabrain.metrics.audit import (
        find_anti_pattern_metrics,
        remove_anti_pattern_metrics,
    )

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
            findings = find_anti_pattern_metrics(store, source_connection_id=source_id)
            if not findings:
                print("metrics audit: no anti-pattern metrics found.")
                return 0

            print(f"metrics audit: {len(findings)} flagged metric(s):")
            print()
            for finding in findings:
                m = finding.metric
                measure_body = (
                    m.measure.column if m.measure.column is not None else m.measure.expression
                )
                print(f"  {m.name}  entity={m.entity}  {m.measure.agg}({measure_body})")
                print(f"    matched phrase: {finding.matched_phrase!r}")
                print(
                    f"    origin: {m.origin}"
                    + (" [DBT-OWNED — cannot fix]" if finding.is_dbt_owned else "")
                )
                desc_line = (m.description or "").strip().replace("\n", " ")
                if desc_line:
                    excerpt = desc_line if len(desc_line) <= 140 else (desc_line[:137] + "...")
                    print(f"    description: {excerpt}")
                print()

            if not fix:
                # Read-only mode: exit 1 so callers (CI, scripts)
                # can branch on the audit verdict without parsing
                # stdout.
                print("Re-run with --fix to remove the non-dbt-owned findings.")
                return 1

            # `--fix` path: delete each non-dbt-owned finding.
            removed, skipped = remove_anti_pattern_metrics(store, findings)
            print(f"metrics audit: removed {removed} metric(s).")
            if skipped:
                print(
                    f"metrics audit: {len(skipped)} dbt-owned metric(s) were left in place. "
                    f"Drop them in your dbt repo and re-import to remove."
                )
            return 0
    except OSError as e:  # pragma: no cover — defensive, mirrors _cmd_metrics_list
        _render_guided(store_path_unwritable(store_path, e))
        return 2
    except ValueError as exc:  # pragma: no cover — defensive, mirrors _cmd_metrics_list
        print(
            f"error: failed to read metrics from {store_path!r}: store appears corrupt ({exc})",
            file=sys.stderr,
        )
        return 2


def _resolve_source_id_or_walk(
    positional_url: str | None,
    url_env: str | None,
) -> tuple[str | None, int]:
    """Resolve `--source`/`--url-env` flags to a source_id, or fall through.

    Shared shape for the export commands: returns `(source_id, 0)` on
    success. `source_id is None` means "no flag passed; the handler
    should walk every source the store knows about". `(None, 2)`
    signals a malformed URL flag; the caller propagates exit 2.
    """
    if positional_url is None and url_env is None:
        return None, 0
    source_url = _resolve_url_source(positional=positional_url, url_env=url_env)
    if source_url is None:
        return None, 2
    if _resolve_url(source_url) is None:  # pragma: no cover — defensive
        return None, 2
    return _make_source_id(source_url), 0


def _cmd_metrics_export(
    *,
    name: str,
    out: str | None,
    store_path: str,
    positional_url: str | None,
    url_env: str | None,
) -> int:
    """Render one metric as apply-ready YAML on stdout or to `--out PATH`.

    Cross-source posture matches `entities export`: without a source
    flag, the handler errors if the same name lives in multiple sources.
    """
    from schemabrain.metrics.yaml_grammar import metric_to_yaml

    source_id, rc = _resolve_source_id_or_walk(positional_url, url_env)
    if rc:
        return rc

    try:
        with SQLiteStore(store_path) as store:
            metric: Metric | None
            if source_id is not None:
                metric = store.get_metric(name, source_connection_id=source_id)
                if metric is None:
                    print(
                        f"error: no metric named {name!r} for source "
                        f"{source_id!r} in {store_path!r}",
                        file=sys.stderr,
                    )
                    return 1
            else:
                source_ids = sorted(_list_source_ids_with_metric(store, name))
                if not source_ids:
                    print(
                        f"error: no metric named {name!r} in {store_path!r}\n"
                        f"  next: run `schemabrain metrics list` to see what is curated.",
                        file=sys.stderr,
                    )
                    return 1
                if len(source_ids) > 1:
                    print(
                        f"error: metric {name!r} is defined in {len(source_ids)} sources: "
                        f"{source_ids}. Re-run with --source/--url-env to disambiguate.",
                        file=sys.stderr,
                    )
                    return 2
                metric = store.get_metric(name, source_connection_id=source_ids[0])
                if metric is None:  # pragma: no cover — concurrent-writer race
                    print(
                        f"error: metric {name!r} no longer present in source {source_ids[0]!r}",
                        file=sys.stderr,
                    )
                    return 1
    except OSError as e:  # pragma: no cover — disk/permission failure not simulatable in CI
        _render_guided(store_path_unwritable(store_path, e))
        return 2

    return _write_yaml_body(metric_to_yaml(metric) + "\n", out)


def _write_yaml_body(body: str, out: str | None) -> int:
    """Write a YAML body to stdout or to `out` (file path).

    Shared exit-code contract: 0 on success; 2 on filesystem error.
    Surfaces a `wrote <path>` confirmation on stderr when writing to
    disk so the operator sees what landed, while stdout stays clean
    for `tee` / `>` redirection of the body itself.
    """
    if out is None:
        sys.stdout.write(body)
        return 0
    out_p = Path(out).expanduser()
    try:
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(body, encoding="utf-8")
    except OSError as exc:  # pragma: no cover — disk/permission failure not simulatable in CI
        print(f"error: cannot write {out_p}: {exc}", file=sys.stderr)
        return 2
    print(f"wrote {out_p}", file=sys.stderr)
    return 0


def _cmd_metrics_export_all(
    *,
    out_dir: str,
    store_path: str,
    positional_url: str | None,
    url_env: str | None,
) -> int:
    """Write one apply-ready YAML per metric into `--dir`.

    Refuses on cross-source name collisions and existing files, same
    contract as `_cmd_entities_export_all`.
    """
    from schemabrain.metrics.yaml_grammar import metric_to_yaml

    source_id, rc = _resolve_source_id_or_walk(positional_url, url_env)
    if rc:
        return rc

    try:
        with SQLiteStore(store_path) as store:
            metrics = store.list_metrics(source_connection_id=source_id)
    except OSError as e:  # pragma: no cover — disk/permission failure not simulatable in CI
        _render_guided(store_path_unwritable(store_path, e))
        return 2

    return _bulk_export_yaml_files(
        items=metrics,
        out_dir=out_dir,
        name_attr="name",
        serializer=metric_to_yaml,
        noun_singular="metric",
        noun_plural="metrics",
        scope_has_source=source_id is not None,
    )


def _bulk_export_yaml_files(
    *,
    items: list,
    out_dir: str,
    name_attr: str,
    serializer,
    noun_singular: str,
    noun_plural: str,
    scope_has_source: bool,
) -> int:
    """Shared body for `entities/metrics/joins export-all`.

    Refuses on cross-source name collisions (when no source flag is
    passed and two rows share a name) and on existing target files
    (so prior hand-edits are not silently overwritten). Writes
    `<name>.yaml` per item via `serializer(item)`.
    """
    if not items:
        scope = "this source" if scope_has_source else "any source the store knows about"
        print(f"(no {noun_plural} to export in {scope})")
        return 0

    if not scope_has_source:
        seen: dict[str, int] = {}
        for item in items:
            key = getattr(item, name_attr)
            seen[key] = seen.get(key, 0) + 1
        collisions = sorted(n for n, c in seen.items() if c > 1)
        if collisions:
            print(
                f"error: {noun_singular} name(s) collide across sources: {collisions}. "
                f"Re-run with --source/--url-env to pick one.",
                file=sys.stderr,
            )
            return 2

    out_p = Path(out_dir).expanduser()
    existing: list[str] = []
    for item in items:
        candidate = out_p / f"{getattr(item, name_attr)}.yaml"
        if candidate.exists():
            existing.append(candidate.name)
    if existing:
        print(
            f"error: refusing to overwrite existing files in {out_p}: "
            f"{sorted(existing)}. Delete them or pick a fresh --dir.",
            file=sys.stderr,
        )
        return 2

    try:
        out_p.mkdir(parents=True, exist_ok=True)
        for item in items:
            (out_p / f"{getattr(item, name_attr)}.yaml").write_text(
                serializer(item) + "\n", encoding="utf-8"
            )
    except OSError as exc:  # pragma: no cover — disk/permission failure not simulatable in CI
        print(f"error: cannot write to {out_p}: {exc}", file=sys.stderr)
        return 2

    noun = noun_singular if len(items) == 1 else noun_plural
    print(f"exported {len(items)} {noun} to {out_p}/", file=sys.stderr)
    return 0


def _cmd_metrics_show(
    *,
    name: str,
    store_path: str,
    positional_url: str | None,
    url_env: str | None,
) -> int:
    """Drill into one metric by name — namespaced shortcut for `inspect`.

    `inspect <name>` already resolves entity → metric → join in priority,
    so a metric whose name collides with an entity is shadowed. `metrics
    show` skips the cascade and resolves only against metrics, so the
    operator who already knows they want a metric gets the right drill
    every time.

    Without `--source` / `--url-env` the handler walks every source the
    store knows about and renders each match — same cross-source posture
    as `inspect`. Exit codes mirror `inspect`'s drill mode:
      - 0: at least one metric rendered
      - 1: no metric with that name in any source the store knows about
      - 2: structural refusal (missing/corrupt store, URL conflict)
    """
    from schemabrain.core.store import SchemaVersionMismatchError
    from schemabrain.inspect import build_metric_detail, render_metric_detail

    source_id: str | None = None
    if positional_url is not None or url_env is not None:
        source_url = _resolve_url_source(positional=positional_url, url_env=url_env)
        if source_url is None:
            return 2
        if _resolve_url(source_url) is None:  # pragma: no cover — defensive
            return 2
        source_id = _make_source_id(source_url)

    store_p = Path(store_path)
    if not store_p.exists():
        _render_guided(
            GuidedError(
                kind="metrics_show_store_missing",
                message=f"store not found at {store_path}",
                why="`schemabrain metrics show` reads from the local SQLite "
                "store; without a store there is nothing to drill into",
                fix=f"run `schemabrain index --url-env DBURL --store-path "
                f"{store_path}` to populate it",
                next_step="re-run `schemabrain metrics show` after `index` "
                "and `metrics apply` complete",
            )
        )
        return 2

    console = _stderr_console()
    try:
        with SQLiteStore(store_p) as store:
            if source_id is not None:
                candidate_sources = [source_id]
            else:
                candidate_sources = _list_source_ids_with_metric(store, name)
                if not candidate_sources:
                    print(
                        f"error: no metric named {name!r} in {store_path!r}\n"
                        f"  next: run `schemabrain metrics list` to see what "
                        f"is curated, or `schemabrain inspect {name}` to "
                        f"search across entities, metrics, and joins.",
                        file=sys.stderr,
                    )
                    return 1

            rendered = False
            for sid in candidate_sources:
                metric_detail = build_metric_detail(
                    store=store,
                    metric_name=name,
                    source_connection_id=sid,
                )
                if metric_detail is None:
                    continue
                if rendered:  # pragma: no cover — same-name-in-many-sources separator
                    console.print()
                render_metric_detail(metric_detail, console=console)
                rendered = True

            if not rendered:
                print(
                    f"error: no metric named {name!r} in {store_path!r}\n"
                    f"  next: run `schemabrain metrics list` to see what "
                    f"is curated, or `schemabrain inspect {name}` to "
                    f"search across entities, metrics, and joins.",
                    file=sys.stderr,
                )
                return 1
            return 0
    except SchemaVersionMismatchError as exc:  # pragma: no cover — same recovery as inspect
        _render_guided(
            GuidedError(
                kind="metrics_show_schema_version_mismatch",
                message=str(exc),
                why="the local store was written by a different schemabrain version",
                fix="delete the store file and re-run `schemabrain index`, "
                "or downgrade to a matching schemabrain version",
                next_step=f"rm {store_path} && schemabrain index ...",
            )
        )
        return 2
    except sqlite3.OperationalError as exc:  # pragma: no cover — partial-migration variant
        _render_guided(
            GuidedError(
                kind="metrics_show_store_inconsistent",
                message=f"sqlite3.OperationalError: {exc}",
                why="the local store passed the schema-version check but "
                "a required table or column is missing — likely a "
                "partial migration or hand-edited store",
                fix="delete the store file and re-run `schemabrain index` to rebuild from scratch",
                next_step=f"rm {store_path} && schemabrain index ...",
            )
        )
        return 2


def _cmd_metrics_suggest(
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
    """LLM-suggest metrics for an indexed schema's entities.

    Orchestrates: resolve source -> read entities + tables from store ->
    build LLM client (anthropic or stub) wrapped in CostCeilingGuard ->
    run suggest pipeline -> render output per mode (dry-run / out-dir /
    apply). Mirror of `_cmd_entities_suggest` — same cost-ceiling
    contract, same env-var precedence, same exit-code surface.

    The pre-flight refuses with a guided error when no entities exist
    for this source. Metrics anchor on entities (FK enforced at write
    time), so suggesting against an empty entity set would either
    produce 0 candidates (wasted LLM call) or candidates that all fail
    at apply time.

    Exit codes:
      0: success
      1: user-input class (empty entities, malformed LLM output,
         ceiling breached, anchor-FK violation)
      2: structural (missing URL, missing API key, unwritable store)
    """
    source_url = _resolve_url_source(
        positional=positional_url,
        url_env=url_env,
        allow_interactive=True,
        interactive_purpose="to suggest metrics for",
    )
    if source_url is None:
        return 2
    if _resolve_url(source_url) is None:  # pragma: no cover — defensive
        return 2
    source_id = _make_source_id(source_url)

    # Resolve the cost ceiling: CLI flag > env var > default. Same
    # precedence as `entities suggest`; delegates to the shared `_env`
    # parser with `on_invalid="raise"` and translates the ValueError
    # into the standard guided-error block.
    if max_cost_usd is None:
        try:
            max_cost_usd = resolve_positive_float_env(
                _SUGGEST_COST_ENV_VAR,
                _DEFAULT_SUGGEST_MAX_COST_USD,
            )
        except ValueError as exc:
            _render_guided(
                GuidedError(
                    kind="suggest_cost_env_malformed",
                    message=str(exc),
                    why="cost ceiling must be a positive float (USD)",
                    fix=f"unset {_SUGGEST_COST_ENV_VAR} or set it to a positive "
                    f"number without underscores or scientific notation "
                    f"(e.g. {_SUGGEST_COST_ENV_VAR}=0.50)",
                    next_step="see `schemabrain metrics suggest --help`",
                )
            )
            return 2

    # Build the LLM client. Stub reads canned YAML from env (so the
    # multi-line response stays out of argv). Anthropic reads
    # ANTHROPIC_API_KEY via the shared resolver — same env source as
    # `entities suggest`, with interactive prompt-on-miss when TTY.
    llm_client: LLMClient
    if provider == "stub":
        canned = os.environ.get(_SUGGEST_STUB_RESPONSE_ENV_VAR)
        if canned is None:
            # `--provider stub` is meaningful only with a canned response.
            # Match the `entities suggest` shape: warn loudly to stderr
            # and default to an empty candidate list so a misconfigured
            # CI job that forgot to set the env var fails noisily rather
            # than silently exiting 0.
            print(
                f"warning: --provider stub with {_SUGGEST_STUB_RESPONSE_ENV_VAR} "
                f"unset; defaulting to an empty candidate list. Set "
                f"{_SUGGEST_STUB_RESPONSE_ENV_VAR} to provide a canned response.",
                file=sys.stderr,
            )
            canned = "candidates: []"
        llm_client = FakeLLMClient(text_provider=lambda _s, _u: canned)
    else:
        api_key = _resolve_anthropic_key_source(
            allow_interactive=True,
            interactive_purpose="suggest metrics",
            interactive_cost_estimate_usd=0.02,
            interactive_cap_usd=max_cost_usd,
            interactive_skip_hint="press Enter to abort (or re-run with --provider stub)",
        )
        if not api_key:
            _render_guided(
                GuidedError(
                    kind="anthropic_api_key_missing",
                    message="ANTHROPIC_API_KEY is not set",
                    why="metric suggestion uses Claude (Sonnet 4.6) to "
                    "analyse your entity bindings; the SDK needs a key",
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
    pipeline = MetricSuggestionPipeline(llm=guard)

    # Read entities + tables. Bail with a guided error rather than
    # calling the LLM with an empty entity set.
    try:
        entities, tables = _load_entities_and_tables_for_source(
            store_path=store_path,
            source_id=source_id,
        )
    except OSError as e:  # pragma: no cover — disk/permission failure not simulatable in CI
        _render_guided(store_path_unwritable(store_path, e))
        return 2
    if not entities:
        _render_guided(
            GuidedError(
                kind="suggest_metrics_no_entities",
                message="no entities in the local store for this source",
                why="metric suggestion needs at least one entity to anchor "
                "candidates on (every metric binds to one entity)",
                fix="run `schemabrain entities apply` or "
                "`schemabrain entities suggest --apply` first, then re-run",
                next_step="verify with `schemabrain entities list`",
            )
        )
        return 1
    if not tables:  # pragma: no cover — structurally rare; entity FK requires the table row
        # Defensive — an entity exists but its bound table isn't indexed.
        # Operationally rare (write_entity FK requires the table row),
        # but a manual mid-run table delete could land here.
        _render_guided(
            GuidedError(
                kind="suggest_metrics_no_tables",
                message="no indexed tables in the local store for this source",
                why="metric suggestion serialises column lists from each "
                "entity's bound table; missing tables would force the LLM "
                "to guess column names blind",
                fix="run `schemabrain index --url-env DATABASE_URL` first",
                next_step="verify with `schemabrain entities list`",
            )
        )
        return 1

    # F1 mirror of entities suggest — preamble + spinner around the
    # LLM call. Metrics estimate is 2x entities (more context tokens
    # via entity list + table schemas), matching the wizard preamble.
    if provider == "stub":
        progress_ctx: AbstractContextManager[None] = contextlib.nullcontext()
    else:
        progress_ctx = _suggest_llm_progress(
            action=f"define metrics ({len(entities)} entities, {len(tables)} tables)",
            model="claude-sonnet-4-6",
            cost_estimate_usd=0.02,
            cap_usd=max_cost_usd,
        )
    try:
        with progress_ctx:
            result = pipeline.propose_from_entities(entities, tables, top_k=top_k)
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
    except MetricSuggestionParseError as exc:
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
    except Exception as exc:
        # Narrow handler: only the LLM round-trip is inside this try.
        # F5 scenario — render Shape C and exit cleanly; anything not
        # classified by the renderer (local programming bugs)
        # propagates so the user sees the traceback. Mirrors the
        # entity-suggest handler above.
        if _try_render_llm_failure(
            exc,
            retry_command="schemabrain metrics suggest",
            fallback_command=None,
        ):
            return 2
        raise

    if dry_run:
        _render_metrics_dry_run(result)
        return 0
    if out_dir is not None:
        return _render_metrics_to_out_dir(result, Path(out_dir))
    if not apply:  # pragma: no cover — argparse mutex group makes this unreachable
        # `assert` would be stripped under `python -O`, silently
        # returning None (which sys.exit treats as 0). Use an
        # explicit raise so the invariant survives optimization.
        raise RuntimeError(
            "unreachable: argparse mutex group requires --dry-run, --out-dir, or --apply"
        )
    return _render_metrics_apply(result, store_path=store_path, source_id=source_id)


def _load_entities_and_tables_for_source(
    *,
    store_path: str,
    source_id: str,
) -> tuple[list[Entity], list[Table]]:
    """Read every indexed Entity and Table for `source_id` from the local store.

    Single read pass, single context-manager scope — both lists hydrate
    in one open of the store. Returns empty lists if the store has no
    rows for this source (the suggest CLI's "did you index yet?" /
    "did you define entities?" checks fire on that).
    """
    with SQLiteStore(store_path) as store:
        entities = store.list_entities(source_connection_id=source_id)
        names = store.list_tables(source_connection_id=source_id)
        tables: list[Table] = []
        for schema, name in names:
            table = store.get_table(schema, name, source_connection_id=source_id)
            if (
                table is not None
            ):  # pragma: no branch — `list_tables` only yields names backed by rows; the None branch defends against a TOCTOU delete
                tables.append(table)
        return entities, tables


def _render_metrics_dry_run(result: MetricSuggestionResult) -> None:
    """Print metric-suggestion candidates to stdout in human-readable form.

    Each candidate is rendered as a YAML body (the apply-ready metric
    grammar) with envelope fields (confidence, rationale) as comment
    lines above. A trailing summary reports total cost and the LLM
    model. Same shape as `_render_dry_run` for entities.
    """
    if not result.candidates:
        print("no candidates suggested.")
        return
    for candidate in result.candidates:
        print(_format_metric_candidate_for_dry_run(candidate))
        print()
    print(
        f"-- {len(result.candidates)} candidate(s) | "
        f"model: {result.llm_model} | "
        f"cost: ${result.total_cost_usd:.4f}"
    )


def _format_metric_candidate_for_dry_run(candidate: MetricCandidate) -> str:
    """Render one MetricCandidate as YAML body + envelope comments.

    Body matches the canonical metric YAML grammar (so it could be
    copy-pasted into a file and applied verbatim). Envelope fields
    appear as `# <field>: <value>` comments above the body — visible
    to humans, invisible to `parse_metric_yaml`.
    """
    rationale = _collapse_newlines(candidate.rationale or "(no rationale provided)")
    lines: list[str] = [
        f"# confidence: {candidate.confidence}",
        f"# rationale: {rationale}",
    ]
    lines.extend(_format_metric_yaml_body(candidate).splitlines())
    return "\n".join(lines)


def _format_metric_yaml_body(candidate: MetricCandidate) -> str:
    """Render the canonical metric YAML body — apply-ready, no envelope.

    Thin wrapper around `metrics.yaml_grammar.metric_to_yaml` so the
    suggest-out-dir path and the `metrics export` command share a
    single serialiser; a future grammar change lands in one place.
    """
    from schemabrain.metrics.yaml_grammar import metric_to_yaml

    return metric_to_yaml(candidate.metric)


def _render_metrics_to_out_dir(result: MetricSuggestionResult, out_dir: Path) -> int:
    """Write one apply-ready YAML per candidate plus a metadata sidecar.

    Per-metric YAML is the canonical metric grammar — clean of
    envelope fields. The sidecar `_suggestion_metadata.json` carries
    confidence/rationale keyed by metric name, so a human reviewing
    the directory can see the LLM's reasoning without it leaking into
    the persisted metric rows.

    Refuses to overwrite existing files: a user who has hand-edited a
    previous run's YAML in this directory should not lose that edit
    silently. The conflict check fires before any write, so a partial
    write isn't possible either.

    Filesystem errors (unwritable parent, disk-full mid-write,
    permission denied on `mkdir`) surface as a guided error with
    exit code 2. The mkdir-then-conflict-check-then-write sequence
    runs inside one try/except so a disk-full failure during the
    write loop is reported without a raw Python traceback.
    """
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        # Pre-check for conflicts so we either write everything or
        # write nothing — no partial overwrites of user-edited files.
        conflicts: list[str] = []
        for candidate in result.candidates:
            if (out_dir / f"{candidate.metric.name}.yaml").exists():
                conflicts.append(f"{candidate.metric.name}.yaml")
        sidecar = out_dir / "_suggestion_metadata.json"
        if sidecar.exists():
            conflicts.append("_suggestion_metadata.json")
        if conflicts:
            _render_guided(
                GuidedError(
                    kind="suggest_metrics_out_dir_conflict",
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
            yaml_path = out_dir / f"{candidate.metric.name}.yaml"
            yaml_path.write_text(_format_metric_yaml_body(candidate) + "\n")
            metadata[candidate.metric.name] = {
                "confidence": candidate.confidence,
                "rationale": candidate.rationale,
            }
        sidecar.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    except OSError as e:
        # Mid-loop OSError leaves a partial directory (some YAMLs
        # written, sidecar maybe not). Flag the partial state so the
        # operator doesn't `metrics apply` on a half-written directory.
        _render_guided(
            GuidedError(
                kind="suggest_metrics_out_dir_unwritable",
                message=f"cannot write candidates to {out_dir!r}: {e}",
                why="filesystem write failed (unwritable parent, disk full, "
                "or permission denied); the directory may contain a partial "
                "set of YAML files",
                fix="check the path is writable and has free space, then "
                "delete any partial output and re-run",
                next_step=f"`ls {out_dir!r}` to inspect what landed before failure",
            )
        )
        return 2
    print(
        f"wrote {len(result.candidates)} candidate(s) to {out_dir} | "
        f"model: {result.llm_model} | "
        f"cost: ${result.total_cost_usd:.4f}"
    )
    return 0


def _render_metrics_apply(
    result: MetricSuggestionResult,
    *,
    store_path: str,
    source_id: str,
) -> int:
    """Write suggested metric candidates to the store with origin='suggested'.

    `store.write_metric` commits per call (each is its own SQLite
    transaction). If candidate N fails (anchor-entity FK violation or
    dbt-guard refusal), candidates 0..N-1 are already durably
    committed. The error message names how many metrics landed before
    the failure so the user knows the state of the store without
    having to query it manually.

    **Stop-on-first-failure policy.** This handler bails on the first
    failing candidate and does NOT attempt the remaining ones — mirror
    of `_render_apply` for entities. The asymmetric counterpart is
    `_cmd_joins_suggest`, which aggregates failures across candidates
    and exits 1 at the end. The asymmetry is deliberate: failures here
    are typically schema-shape issues (missing anchor entity, dbt
    ownership conflict) that surface once and would surface again on
    every remaining candidate, so continuing produces noise without
    information. Re-run after fixing the named issue; the `--apply`
    write is idempotent (UPSERT semantics), so already-applied
    candidates won't double-write.
    """
    written: list[str] = []
    total = len(result.candidates)
    try:
        with SQLiteStore(store_path) as store:
            for candidate in result.candidates:
                try:
                    store.write_metric(candidate.metric, source_connection_id=source_id)
                except DbtOwnedMetricError as exc:
                    _metric_error(
                        _partial_write_message(written, total, str(exc), item_label="metrics")
                    )
                    return 1
                except sqlite3.IntegrityError:
                    # FK violation — the anchor entity doesn't exist
                    # for this source. CHECK violations are ruled out
                    # by the dataclass invariants. Drop the raw SQLite
                    # text ("FOREIGN KEY constraint failed") so the
                    # user sees the actionable fix, not database lingo.
                    _metric_error(
                        _partial_write_message(
                            written,
                            total,
                            f"metric {candidate.metric.name!r} anchors on entity "
                            f"{candidate.metric.entity!r}, which is not present in "
                            f"the store. Run `schemabrain entities apply` first.",
                            item_label="metrics",
                        )
                    )
                    return 1
                written.append(candidate.metric.name)
    except OSError as e:  # pragma: no cover — store-path-unwritable path is covered by sibling list/apply OSError tests
        _render_guided(store_path_unwritable(store_path, e))
        return 2

    for name in written:
        print(f"applied metric: {name}")
    print(
        f"-- {len(written)}/{total} metric(s) applied | "
        f"model: {result.llm_model} | "
        f"cost: ${result.total_cost_usd:.4f}"
    )
    return 0


def _metric_error(message: str) -> None:
    """Print a metric-write error to stderr.

    Symmetric with `_entity_error` — keeps the suggest-apply summary
    style consistent between the two surfaces.
    """
    print(f"error: {message}", file=sys.stderr)


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
    verify: bool = False,
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

    `verify=True` routes to the mock-agent smoke instead of the
    config-health report — different surface, different exit code
    semantics (0 = green, 2 = at least one required stage failed).
    """
    import time as _time

    from schemabrain.setup.doctor_flow import doctor, render_doctor, render_doctor_json

    source_url: str | None = None
    if positional_url is not None or url_env is not None:
        source_url = _resolve_url_source(positional=positional_url, url_env=url_env)
        if source_url is None:
            # Guided error already rendered to stderr.
            return 2

    if verify:
        from schemabrain.setup.doctor_verify import render_verify, verify_mock_agent

        result = verify_mock_agent(store_path=Path(store_path), source_url=source_url)
        render_verify(result, console=_stderr_console())
        return result.exit_code
    started = _time.perf_counter()
    result = doctor(
        source_url=source_url,
        store_path=Path(store_path),
        host=host,  # type: ignore[arg-type]
    )
    elapsed_ms = int((_time.perf_counter() - started) * 1000)
    if json_output:
        # JSON to stdout — clean pipe target. ``elapsed_ms`` is
        # deliberately not folded into the JSON contract — the doctor
        # JSON shape is frozen at ``{checks, summary, exit_code}`` so
        # CI pipelines grepping against it don't break on the addition
        # of a presentation-only field.
        sys.stdout.write(render_doctor_json(result))
    else:
        # Human-readable to stderr so users can pipe stdout cleanly
        # in mixed-output scripts.
        render_doctor(result, console=_stderr_console(), elapsed_ms=elapsed_ms)
    return result.exit_code


def _cmd_init(
    *,
    positional_url: str | None,
    url_env: str | None,
    store_path: str,
    host: str | None,
    env_var: str,
    skip_index: bool,
    no_entities: bool,
    no_metrics: bool,
    no_joins: bool,
    no_embed: bool,
    enrich: bool,
    entities_max_cost_usd: float | None,
    metrics_max_cost_usd: float | None,
    assume_yes: bool,
    print_only: bool,
    from_dbt: str | None = None,
    skip_llm_confirm: bool = False,
    pii_block_csv: str | None = None,
    emit_yaml_dir: str | None = None,
    enable_sonnet: bool = False,
) -> int:
    """Run the activation wizard and render the multi-stage outcome.

    The wizard executes seven stages: validate the source, index the
    schema, suggest entities, suggest metrics anchored on those
    entities, suggest canonical joins between the entities, wire the
    MCP host, print the next-step hint. See `schemabrain.setup.wizard`
    for the per-stage contracts.

    Exit codes:
      - 0: wizard reached stage 7 (whether or not stages 3, 4, or 5
        were skipped or failed — entity / metric / join curation are
        all best-effort)
      - 1: stage 6 (wire_host) succeeded but Claude Code's `claude
        mcp add` shell-out failed (the snippet is still printable
        from the rendered output so the user can fall back to running
        it)
      - 2: the wizard aborted before reaching stage 7 (stage 1, 2,
        or 6 failed, or the URL flags were malformed)

    Interactive recovery: if stage 6 fails because a different
    schemabrain entry already exists in the host config, the user
    is prompted to confirm an overwrite. Declining returns 0 with
    no changes.

    `--print-only` is an alias for `--host manual` — when either is
    set, stage 6 never writes; the snippet renders to stdout.
    """
    from typing import get_args as _get_args

    from schemabrain.setup.hosts import HostName, detect_host
    from schemabrain.setup.preflight import detect_apple_silicon_fastembed_gap
    from schemabrain.setup.wizard import WizardConfig, run_default_wizard

    # Early validation only: when `--host` was passed explicitly, fail
    # fast on a typo before stage 0 fires. The full host RESOLUTION
    # (interactive prompt vs detect_host vs explicit) runs later,
    # after source-URL resolution — that way the operator answers
    # questions in the order they think about them (which database
    # first, where to wire it second). The valid_hosts gate stays
    # here so argparse-validated choices and the manual override
    # path both get the same clean error.
    valid_hosts = _get_args(HostName)
    if host is not None and host not in valid_hosts:
        _render_guided(
            GuidedError(
                kind="init_invalid_host",
                message=f"unknown --host {host!r}",
                why="schemabrain init wires one of a fixed list of MCP hosts",
                fix=f"pass --host one of {sorted(valid_hosts)}",
                next_step="run `schemabrain init --help` to see the choices",
            )
        )
        return 2

    # Stage 0 preflight: refuse fast on the Apple Silicon + Python
    # 3.12+ + missing-fastembed combination, before any URL prompt
    # fires. `--skip-index` and `--no-embed` both bypass the embedder
    # path, so neither needs the gate. Without this guard the wizard
    # runs through host validation and source-URL prompts before
    # crashing at stage 2 with an opaque ImportError when the
    # indexer tries to load `fastembed`.
    if not skip_index and not no_embed:
        gap = detect_apple_silicon_fastembed_gap()
        if gap is not None:
            _render_guided(
                GuidedError(
                    kind="init_apple_silicon_fastembed_gap",
                    message=gap,
                    why="the wizard's index stage will try to build embeddings "
                    "via `fastembed` and crash with an ImportError on this "
                    "platform combination",
                    fix="re-run with `--no-embed` to skip embedding generation "
                    "(degrades `find_relevant_entities` from vector to keyword "
                    "matching but the wizard completes), OR switch to "
                    "Python 3.11 via pyenv (`pyenv install 3.11.10 && "
                    "pyenv local 3.11.10`) where onnxruntime ships a wheel",
                    next_step="see CONTRIBUTING.md for the supported Python/platform matrix",
                )
            )
            return 2

    # Stage 0 — the day-one UX overhaul's demo-vs-own-DB fork prompt.
    # Runs ONLY when no URL was supplied via CLI flag or env AND
    # stderr is a TTY AND --yes was NOT passed. `--yes` must skip
    # stage 0 — otherwise a CI run with `--yes` plus an env var
    # would still hit the fork prompt, and the demo-default `[2]`
    # would silently override the env var's URL with the pinned
    # demo URL (the worst kind of CI bug: works interactively,
    # fails differently in automation). Treating `--yes` as "fully
    # non-interactive — no prompts, ever" matches the rest of the
    # wizard's `--yes` contract.
    source_url: str | None = None
    no_source_provided = positional_url is None and url_env is None
    if no_source_provided and _stderr_is_interactive_tty() and not assume_yes:
        from schemabrain.setup.setup_stage import prompt_for_init_setup

        try:
            source_url = prompt_for_init_setup(console=_stderr_console())
        except (KeyboardInterrupt, EOFError):
            # Ctrl-C or stdin-EOF at the setup prompt — clean abort
            # per the standard SIGINT convention. EOFError fires when
            # stdin is closed (SSH session drops, terminal recorder
            # redirects input, pytest test harness). Both produce
            # exit 130 + "aborted." on stderr so the user / CI sees
            # the same shape rather than a Python traceback from
            # rich.prompt.Prompt.ask. EOFError is included because
            # wrapping only KeyboardInterrupt would leave the SSH-
            # drop / non-TTY-mid-prompt paths producing raw
            # tracebacks.
            print("\naborted.", file=sys.stderr)
            return 130
    if source_url is None:
        source_url = _resolve_url_source(positional=positional_url, url_env=url_env)
    if source_url is None:
        # _resolve_url_source already rendered a guided error.
        return 2
    # Apply the silent `+psycopg` rewrite to the resolved URL before
    # handing it to the wizard. Without this, a user who took the
    # stage-0 demo path (DEMO_DATABASE_URL is a bare `postgresql://`)
    # OR typed a bare `postgresql://` in the own-DB prompt would hit
    # stage 1's `create_engine` with the raw scheme, triggering a
    # confusing `ModuleNotFoundError: psycopg2` instead of the silent
    # rewrite the post-init commands already apply.
    #
    # Deliberately NOT using `_resolve_url` here because the init
    # wizard supports non-Postgres sources (sqlite:// for dbt-only
    # paths, where stages 2/3 skip) — `_resolve_url`'s scheme
    # validation would reject those. The rewrite-only path keeps
    # init's broader source surface intact while fixing the
    # Postgres footgun.
    parsed_scheme = urlparse(source_url).scheme
    rewritten = silent_rewrite_to_psycopg(parsed_scheme, source_url)
    if rewritten is not None:
        source_url = rewritten
        # One-line confirmation so the operator sees that we rewrote
        # their pasted libpq URL to the SQLAlchemy/psycopg form. The
        # rewrite itself stays silent in spirit (it doesn't abort or
        # prompt), but a single dim stderr line means a developer who
        # later inspects logs / pastes the same URL into another tool
        # understands why the two strings differ.
        print(
            f"[schemabrain init] detected {parsed_scheme}:// URL; using "
            "postgresql+psycopg:// for SQLAlchemy compatibility",
            file=sys.stderr,
        )

    # Host resolution: source URL is now known; ask the operator
    # which MCP host to wire it into. `--print-only` always wins
    # (alias for manual). Otherwise `--host` explicit → use it.
    # `--host` omitted (sentinel None) → interactive menu under
    # TTY+not-`--yes`; silent `detect_host` single-winner under
    # `--yes` / non-TTY, with a one-line stderr note so a scripted
    # operator sees what got picked and can lock it in next time
    # with `--host X`. The stderr note matters — silent auto-detect
    # is bad UX, not good convenience.
    if print_only:
        effective_host: str = "manual"
    elif host is not None:
        effective_host = host
    elif _stderr_is_interactive_tty() and not assume_yes:
        from schemabrain.setup.host_select import prompt_for_host_selection

        try:
            effective_host = prompt_for_host_selection(console=_stderr_console())
        except (KeyboardInterrupt, EOFError):
            # Ctrl-C / stdin-EOF at the host prompt — clean abort,
            # same shape as the stage-0 source-fork Ctrl-C handler
            # above.
            print("\naborted.", file=sys.stderr)
            return 130
    else:
        effective_host = detect_host()
        print(
            f"[schemabrain init] auto-selected --host {effective_host} "
            "(non-interactive); pass --host explicitly to override.",
            file=sys.stderr,
        )

    # `--yes` is a superset shorthand: it implies the LLM-prompt
    # skip AND the host-overwrite auto-confirm. A user who only
    # wants the LLM-prompt skip (e.g., they want to be asked before
    # overwriting an existing host config) can pass
    # `--skip-llm-confirm` alone.
    effective_skip_llm_confirm = skip_llm_confirm or assume_yes

    # Resolve the PII-block set written into the host snippet under
    # a three-state contract that mirrors `schemabrain serve` itself:
    #
    #   - `--pii-block <csv>` explicit   → parse, validate, use it
    #   - `--pii-block ''` explicit empty → empty tuple (disabled,
    #                                       no `--pii-block` flag
    #                                       added to the snippet)
    #   - flag absent + interactive TTY  → interactive prompt (legacy)
    #   - flag absent + --yes / non-TTY  → catastrophic-leak default
    #                                       + one-line stderr confirm
    #
    # The fourth row is the load-bearing fix: the prior behavior
    # silently dropped to the wizard's `("contact",)` default under
    # `--yes`, so a CI / scripted operator following the README got
    # `contact`-only enforcement while `staff.password` (credential),
    # `customer.credit_card` (payment_card), and `staff.ssn`
    # (government_id) remained readable. The catastrophic-leak
    # default closes that gap without surprising the operator —
    # the stderr confirmation surfaces what's enforced and how to
    # override it.
    from schemabrain.pii import CATASTROPHIC_LEAK_CATEGORIES, PII_CATEGORIES

    pii_block_choice: tuple[str, ...] | None = None
    if pii_block_csv is not None:
        # Explicit flag value — short-circuits both the interactive
        # prompt and the --yes default. Empty CSV → empty tuple
        # (operator opted out); otherwise parse + validate.
        if pii_block_csv == "":
            pii_block_choice = ()
        else:
            requested = tuple(sorted({c.strip() for c in pii_block_csv.split(",") if c.strip()}))
            unknown = sorted(set(requested) - PII_CATEGORIES)
            if unknown:
                print(
                    f"error: --pii-block contains unknown category names: "
                    f"{unknown}. Valid categories: {sorted(PII_CATEGORIES)}.",
                    file=sys.stderr,
                )
                return 2
            pii_block_choice = requested
    elif _stderr_is_interactive_tty() and not assume_yes:
        from schemabrain.setup.setup_stage import prompt_for_pii_block

        try:
            pii_block_choice = prompt_for_pii_block(console=_stderr_console())
        # Ctrl-C / EOF re-raise — same clean-abort convention as
        # `prompt_for_init_setup` above: any prompt exits with the
        # standard exit-130 / exit-2 path the outer handler provides.
        # Skipped from coverage because the operator-driven abort is
        # identical in shape to the existing init-prompt handler.
        except (KeyboardInterrupt, EOFError):  # pragma: no cover
            raise
    else:
        # --yes or non-TTY with no explicit --pii-block. Apply the
        # safe-by-default catastrophic-leak set and surface the
        # choice to stderr so CI logs show what got enforced.
        pii_block_choice = tuple(sorted(CATASTROPHIC_LEAK_CATEGORIES))
        print(
            "schemabrain init: --pii-block not passed; defaulting to "
            f"{','.join(pii_block_choice)} "
            "(use --pii-block '' to disable, --pii-block <csv> to override).",
            file=sys.stderr,
        )

    wizard_kwargs: dict[str, object] = {
        "source_url": source_url,
        "store_path": Path(store_path),
        "host": effective_host,
        "env_var_name": env_var,
        "skip_index": skip_index,
        "no_entities": no_entities,
        "enrich": enrich,
        "entities_max_cost_usd": entities_max_cost_usd,
        "assume_yes": assume_yes,
        "no_metrics": no_metrics,
        "metrics_max_cost_usd": metrics_max_cost_usd,
        "no_joins": no_joins,
        "no_embed": no_embed,
        "enable_sonnet": enable_sonnet,
        "from_dbt": Path(from_dbt) if from_dbt else None,
        "skip_llm_confirm": effective_skip_llm_confirm,
    }
    if pii_block_choice is not None:
        wizard_kwargs["pii_block"] = pii_block_choice
    cfg = WizardConfig(**wizard_kwargs)  # type: ignore[arg-type]

    host_display = _host_display_name(effective_host)
    console = _stderr_console()
    # Print the header BEFORE the wizard runs so the user sees the
    # banner immediately and the stage-context spinner has a place
    # to land. F3 (post-PR-#79): pre-F3 this block ran inside a
    # `while True:` loop that re-ran the entire wizard when the
    # operator accepted an overwrite prompt — rendering the header
    # twice and orphaning the prompt between the hero panel and
    # the 7-stage table. The overwrite handling now lives INSIDE
    # `_stage_wire_host` (with the prompt rendered inline during
    # stage 6, paused-spinner-aware), so the orchestrator only
    # needs one wizard run.
    _render_wizard_header(host_display=host_display, console=console)
    result = run_default_wizard(cfg, stage_context=_wizard_stage_context)

    # A user-cancelled overwrite is an informed choice, not a
    # failure — handle it BEFORE `_render_wizard_after` so the
    # operator does NOT see a red "Stopped at stage 6 of 7" abort
    # panel for a deliberate cancellation. (Earlier versions
    # rendered the panel first, then the "cancelled" line appeared
    # below it, then exit 0 — UX-misleading.)
    #
    # Use the structured `user_cancelled` field on StageOutcome
    # instead of a message-prefix check — eliminates the cross-
    # module string coupling that would silently break if either
    # copy were edited.
    aborted_at = result.aborted_at
    if result.aborted and aborted_at is not None and aborted_at.user_cancelled:
        console.print("[yellow]cancelled[/] no changes made.")
        return 0

    _render_wizard_after(result, host_display=host_display, console=console)
    if result.aborted:
        return 2

    # Emit YAML projection of the (now-populated) store if requested.
    # Runs AFTER the wizard summary so the operator sees stage outcomes
    # first, then the file-emit confirmation. Skipped on `result.aborted`
    # above so a mid-stage abort doesn't leave a half-populated YAML
    # tree on disk; runs on shell_out_failed because the store IS
    # complete and YAML files are precisely the manual-recovery path.
    # The branch is # pragma: no cover because reaching it requires a
    # successful real wizard run (needs Postgres); the wiring it
    # invokes is exercised directly via the `_emit_yaml_projection`
    # tests in `tests/test_cli_yaml_roundtrip.py`.
    if (
        emit_yaml_dir is not None
    ):  # pragma: no cover — exercised via _emit_yaml_projection unit test
        emit_rc = _emit_yaml_projection(
            base_dir=emit_yaml_dir,
            store_path=store_path,
            source_url=source_url,
        )
        if emit_rc != 0:
            # Surface filesystem / collision failures with a non-zero
            # exit code, but only when the wizard itself succeeded —
            # so a failing emit on an otherwise-good wizard doesn't
            # masquerade as a wizard failure.
            return emit_rc

    if (
        result.host_install_result is not None
        and result.host_install_result.state == "shell_out_failed"
    ):
        return 1
    return 0


def _emit_yaml_projection(
    *,
    base_dir: str,
    store_path: str,
    source_url: str,
) -> int:
    """Write the YAML projection of a freshly-initialised store.

    Produces three subdirectories — `entities/`, `metrics/`, `joins/`
    — under `base_dir`, each containing one YAML per row in the
    corresponding store table. Reuses the existing `_cmd_*_export_all`
    handlers so the file format and collision contract are identical
    between `init --emit-yaml-dir` and the standalone export commands.

    Returns 0 on success, 2 on any collision / filesystem failure.
    """
    base = Path(base_dir).expanduser()
    # Pass `source_url` as the positional URL so the export handlers
    # resolve to the same source_id the wizard just wrote into. Without
    # this the handlers would walk every source, which is fine for a
    # fresh store with one source but error-prone on re-runs against
    # a store that has accumulated multiple sources.
    handlers = (
        (_cmd_entities_export_all, "entities"),
        (_cmd_metrics_export_all, "metrics"),
        (_cmd_joins_export_all, "joins"),
    )
    for handler, subdir in handlers:
        rc = handler(
            out_dir=str(base / subdir),
            store_path=store_path,
            positional_url=source_url,
            url_env=None,
        )
        if rc != 0:
            return rc

    # Seed pii_policy.yaml at the project root (not under a subdir)
    # alongside entities/ metrics/ joins/. Mirrors dbt's `selectors.yml`
    # layout — top-level, one-file-per-project. The starter file
    # carries the catastrophic-leak defaults so the operator's first
    # view shows what's actually enforced (avoids "what does --pii-block
    # do without a flag?" being a source-code question). Refuses to
    # overwrite an existing file so a re-run of `init --emit-yaml-dir`
    # against a directory the operator has hand-edited stays safe.
    from schemabrain.pii.categories import CATASTROPHIC_LEAK_CATEGORIES
    from schemabrain.pii.policy import Policy
    from schemabrain.pii.policy_yaml import policy_to_yaml

    policy_path = base / "pii_policy.yaml"
    if policy_path.exists():
        print(
            f"emit: skipped {policy_path} (already exists; hand-edits preserved)",
            file=sys.stderr,
        )
    else:
        starter = Policy(
            block=CATASTROPHIC_LEAK_CATEGORIES,
            description="Edit `block` to change the categories the firewall refuses.\n"
            "The catastrophic floor (credential, payment_card, government_id) is\n"
            "always enforced in addition to this list — removing those lines does\n"
            "not disable it.\n"
            "`column_overrides` lets operators downgrade over-tagged columns\n"
            "(e.g. card_number_last4 per PCI-DSS Q&A — declare it `internal`\n"
            "with empty categories to allow analytics on it).",
        )
        policy_path.write_text(policy_to_yaml(starter) + "\n", encoding="utf-8")

    print(
        f"emitted YAML projection under {base}/ (entities/, metrics/, joins/, pii_policy.yaml)",
        file=sys.stderr,
    )
    return 0


def _cmd_apply_project(
    *,
    project_dir: str,
    positional_url: str | None,
    url_env: str | None,
    store_path: str,
) -> int:
    """Walk a project tree (entities/, metrics/, joins/) and apply each.

    Directory order is deliberate: entities first, then metrics + joins
    which reference entities. Missing subdirectories are skipped (the
    operator may not curate all three resource types). Each per-resource
    apply runs via the existing `_cmd_*_apply` handlers so the FK
    invariants, dbt-owned guard, and per-file error reporting are
    identical to invoking them directly.

    Exit codes:
      0: every YAML applied cleanly across every subdirectory
      1: at least one file failed in at least one subdir
      2: project_dir missing / not a directory, or source URL missing
    """
    base = Path(project_dir).expanduser()
    if not base.exists():
        print(f"error: project directory not found: {base}", file=sys.stderr)
        return 2
    if not base.is_dir():
        print(f"error: project path is not a directory: {base}", file=sys.stderr)
        return 2

    # Pre-validate the source URL once so a missing flag fails before
    # any per-resource handler opens the store. The inner handlers
    # re-validate, but doing it here keeps the error surface flat:
    # the operator sees "missing --source/--url-env" once, not three
    # times if all three subdirs exist.
    source_url = _resolve_url_source(positional=positional_url, url_env=url_env)
    if source_url is None:
        return 2

    overall_rc = 0
    summary_lines: list[str] = []
    handlers: tuple[tuple[str, object], ...] = (
        ("entities", _cmd_entities_apply),
        ("metrics", _cmd_metrics_apply),
        ("joins", _cmd_joins_apply),
    )
    for subdir_name, handler in handlers:
        subdir = base / subdir_name
        if not subdir.exists():
            summary_lines.append(f"  {subdir_name}/: skipped (subdirectory missing)")
            continue
        # Sorted glob so apply order is deterministic — important for
        # reproducible CI runs and for human-readable progress output.
        yaml_files = sorted(p for p in subdir.iterdir() if p.suffix in (".yaml", ".yml"))
        if not yaml_files:
            summary_lines.append(f"  {subdir_name}/: skipped (no YAML files)")
            continue
        rc = handler(  # type: ignore[operator]
            yaml_paths=[str(p) for p in yaml_files],
            positional_url=positional_url,
            url_env=url_env,
            store_path=store_path,
        )
        verb = "applied" if rc == 0 else "applied with errors"
        summary_lines.append(f"  {subdir_name}/: {verb} {len(yaml_files)} file(s) (rc={rc})")
        if rc > overall_rc:
            overall_rc = rc

    # pii_policy.yaml is a single file at the project root (not under
    # a subdirectory). Pick it up alongside the entity/metric/join
    # subdirs so a single `schemabrain apply ./schemabrain` covers
    # every YAML artefact the operator can edit.
    policy_file = base / "pii_policy.yaml"
    if policy_file.exists():
        rc = _cmd_policy_apply(
            yaml_path=str(policy_file),
            store_path=store_path,
            positional_url=positional_url,
            url_env=url_env,
        )
        verb = "applied" if rc == 0 else "applied with errors"
        summary_lines.append(f"  pii_policy.yaml: {verb} (rc={rc})")
        if rc > overall_rc:
            overall_rc = rc
    else:
        summary_lines.append("  pii_policy.yaml: skipped (file missing)")

    print(f"schemabrain apply: {base}/")
    for line in summary_lines:
        print(line)
    return overall_rc


def _cmd_diff_project(
    *,
    project_dir: str,
    positional_url: str | None,
    url_env: str | None,
    store_path: str,
) -> int:
    """Show drift between a project YAML tree and the store.

    For each resource type (entities, metrics, joins), parses every
    YAML in the subdirectory and compares against rows in the store
    for the resolved source. Both sides are round-tripped through the
    YAML serialiser before comparison so trust-signal fields
    (`inference_method`, `validation_state`) that the YAML grammar
    does not carry are excluded from the diff — matching the
    round-trip semantics of `export[-all]` ↔ `apply`.

    Drift categories per resource:
      * only on disk → YAML present, store row missing (apply would add it)
      * only in store → store row present, no YAML (apply would not delete it; this is informational)
      * value-mismatch → both sides exist but their YAML bodies differ

    Exit codes:
      0: store and tree agree (no drift)
      1: drift detected (CI-actionable, not a tool error)
      2: project_dir missing, source URL missing, parse failure
    """
    base = Path(project_dir).expanduser()
    if not base.exists():
        print(f"error: project directory not found: {base}", file=sys.stderr)
        return 2
    if not base.is_dir():
        print(f"error: project path is not a directory: {base}", file=sys.stderr)
        return 2

    source_url = _resolve_url_source(positional=positional_url, url_env=url_env)
    if source_url is None:
        return 2
    source_id = _make_source_id(source_url)

    try:
        with SQLiteStore(store_path) as store:
            entity_drift = _diff_resource_dir(
                subdir=base / "entities",
                store_items=store.list_entities(source_connection_id=source_id),
                parser="entity",
                serializer="entity",
                kind="entity",
            )
            metric_drift = _diff_resource_dir(
                subdir=base / "metrics",
                store_items=store.list_metrics(source_connection_id=source_id),
                parser="metric",
                serializer="metric",
                kind="metric",
            )
            join_drift = _diff_resource_dir(
                subdir=base / "joins",
                store_items=store.list_canonical_joins(source_connection_id=source_id),
                parser="join",
                serializer="join",
                kind="join",
            )
    except OSError as e:  # pragma: no cover — disk/permission failure not simulatable in CI
        _render_guided(store_path_unwritable(store_path, e))
        return 2

    total_drift = entity_drift + metric_drift + join_drift
    if total_drift == 0:
        print(f"schemabrain diff: {base}/ in sync with store (no drift)")
        return 0
    noun = "drift" if total_drift == 1 else "drifts"
    print(f"schemabrain diff: {total_drift} {noun} detected")
    return 1


def _diff_resource_dir(
    *,
    subdir: Path,
    store_items: list,
    parser: str,
    serializer: str,
    kind: str,
) -> int:
    """Diff one resource type's YAML tree against the store rows.

    Returns the count of drift lines printed for this resource. Both
    sides are serialised to YAML for comparison so trust-signal fields
    not present in the grammar are excluded — matching the
    export→edit→apply round-trip contract. A parse failure on a YAML
    file is reported as a drift entry and counted, NOT raised, so a
    `diff` run against a tree with one broken file still surfaces
    every other drift before failing.

    `parser` / `serializer` / `kind` route to the right grammar module.
    Pass strings ("entity" / "metric" / "join") rather than the
    function objects themselves so this helper does not need to know
    about the dataclass types at signature time.
    """
    grammars = {
        "entity": (
            "schemabrain.entities.yaml_grammar",
            "parse_entity_yaml_file",
            "entity_to_yaml",
        ),
        "metric": (
            "schemabrain.metrics.yaml_grammar",
            "parse_metric_yaml_file",
            "metric_to_yaml",
        ),
        "join": (
            "schemabrain.joins.yaml_grammar",
            "parse_canonical_join_yaml_file",
            "canonical_join_to_yaml",
        ),
    }
    module_name, parser_name, serializer_name = grammars[parser]
    import importlib

    grammar = importlib.import_module(module_name)
    parse_file = getattr(grammar, parser_name)
    to_yaml = getattr(grammar, serializer_name)

    on_disk: dict[str, str] = {}
    parse_errors: list[tuple[str, str]] = []
    if subdir.exists():
        for yaml_path in sorted(subdir.iterdir()):
            if yaml_path.suffix not in (".yaml", ".yml"):
                continue
            try:
                obj = parse_file(yaml_path)
            except ValueError as exc:
                parse_errors.append((yaml_path.name, str(exc)))
                continue
            on_disk[obj.name] = to_yaml(obj)

    in_store: dict[str, str] = {item.name: to_yaml(item) for item in store_items}

    drift = 0
    for filename, message in parse_errors:
        print(f"  {kind}  {filename}: PARSE ERROR — {message}")
        drift += 1

    only_disk = sorted(set(on_disk) - set(in_store))
    only_store = sorted(set(in_store) - set(on_disk))
    common = sorted(set(on_disk) & set(in_store))

    for name in only_disk:
        print(f"  {kind}  {name}: only on disk (apply would add it)")
        drift += 1
    for name in only_store:
        print(f"  {kind}  {name}: only in store (no YAML file)")
        drift += 1
    # CLI subcommand families are not the same as the resource noun
    # — entities/metrics/joins (plural) on the CLI vs entity/metric/
    # join (singular) on the diff lines. Map explicitly so the hint
    # in the value-mismatch line points at the right subcommand.
    cli_family = {"entity": "entities", "metric": "metrics", "join": "joins"}[kind]
    for name in common:
        if on_disk[name] != in_store[name]:
            print(f"  {kind}  {name}: value mismatch (run `{cli_family} export {name}` to inspect)")
            drift += 1
    return drift


def _resolve_tail_events_path(*, events_path: str | None, store_path: str | None) -> str:
    """Resolve `tail`'s events-JSONL path with the documented priority.

    Priority order:

    1. Explicit `--events-path` always wins.
    2. `$SCHEMABRAIN_EVENTS_PATH` env var.
    3. `<store_dir>/events.jsonl` IF `--store-path` was supplied AND
       the file exists. Documented as a convenience for operators who
       wrote events alongside the store; the default `serve`
       configuration writes to `~/.schemabrain/events.jsonl` instead,
       so the existence check keeps us from inventing a path that
       isn't there.
    4. Module-level `_DEFAULT_EVENTS_PATH`.

    Pure function so the priority order is testable without spinning
    a real reader. Uses `is not None` rather than truthy checks so
    an explicit empty-string from a future caller doesn't silently
    fall through to env-var resolution.
    """
    import os as _os
    from pathlib import Path as _Path

    if events_path is not None:
        return events_path
    env_path = _os.environ.get("SCHEMABRAIN_EVENTS_PATH")
    if env_path is not None:
        return env_path
    if store_path:
        candidate = _Path(store_path).expanduser().parent / "events.jsonl"
        if candidate.exists():
            return str(candidate)
    return _DEFAULT_EVENTS_PATH


def _cmd_tail(
    *,
    since: str,
    follow: bool,
    json_mode: bool,
    events_path: str | None,
    store_path: str | None,
) -> int:
    """Run `schemabrain tail`: stream events from the JSONL bus file."""
    import json as _json
    import os as _os
    import sys as _sys
    from pathlib import Path as _Path

    from rich.console import Console as _Console

    from schemabrain.observability import (
        TailOptions,
        TailReader,
        parse_since,
        render_event_pretty,
    )

    resolved_path = _resolve_tail_events_path(events_path=events_path, store_path=store_path)
    path = _Path(resolved_path).expanduser()
    # If the operator passed `--store-path` expecting tail to find
    # events alongside the store, but neither `--events-path` /
    # `$SCHEMABRAIN_EVENTS_PATH` were set AND no sibling
    # `events.jsonl` exists, the resolver silently falls back to
    # `~/.schemabrain/events.jsonl`. That fallback is rarely what
    # the operator intended; surface a one-line note so they know
    # which file we ended up reading. Suppressed when an explicit
    # flag/env was used (the operator already picked the path).
    if (
        store_path is not None
        and events_path is None
        and _os.environ.get("SCHEMABRAIN_EVENTS_PATH") is None
        and resolved_path == _DEFAULT_EVENTS_PATH
    ):
        print(
            f"note: no `events.jsonl` found alongside {store_path}; "
            f"tailing the default {_DEFAULT_EVENTS_PATH}. "
            f"Pass --events-path PATH to override.",
            file=_sys.stderr,
        )
    try:
        since_dt = parse_since(since)
    except ValueError as exc:
        print(f"error: {exc}", file=_sys.stderr)
        return 2
    options = TailOptions(
        events_path=path,
        since=since_dt,
        follow=follow,
        json_mode=json_mode,
    )
    if json_mode:
        out = _sys.stdout
        try:
            with TailReader(options) as reader:
                for event in reader.stream():
                    out.write(_json.dumps(event, separators=(",", ":")) + "\n")
                    out.flush()
        except KeyboardInterrupt:
            return 0
        return 0
    console = _Console()
    try:
        with TailReader(options) as reader:
            for event in reader.stream():
                render_event_pretty(event, console)
    except KeyboardInterrupt:
        return 0
    return 0


# Cap on the `schema_version` value we'll echo to stderr in
# `_warn_on_schema_drift`. A crafted store file could stuff a multi-MB
# string into `schemabrain_meta.value` and flood the operator's stderr
# pipeline. 64 chars is generous headroom for any legitimate version
# string (current "12"; a "v3.5.7-rc1+build.42" would still fit).
_MAX_SCHEMA_VERSION_ECHO = 64


def _warn_on_schema_drift(conn: sqlite3.Connection, path: Path) -> None:
    """Emit a stderr warning when the store's schema_version diverges
    from `SCHEMA_VERSION`, or when the version record is missing.

    The two `audit` read paths (`list`, `verify`) deliberately bypass
    `SQLiteStore` to open the file read-only — which means they also
    bypass the strict `SchemaVersionMismatchError` that `SQLiteStore`
    raises on drift. That's intentional for inspectors (history rows
    stay readable even after an upgrade), but the user deserves to
    know the column shapes they're seeing may not match the current
    code's expectations. Warn-and-proceed, not error-and-refuse.

    Three failure modes get distinct treatment:
      - `schemabrain_meta` table absent or unreadable → silent return.
        The caller's own audit query will hit the same DB error and
        surface it to the user; a second message would be noise.
      - Meta row missing (`schema_version` key not present in an
        otherwise readable store) → warn. This is the case where the
        audit table IS readable but the version record was deleted; the
        operator deserves to know the column shape is unverifiable.
      - Version mismatch → warn with stored vs expected.

    Both call sites assign `conn.row_factory = sqlite3.Row` before
    invoking this helper, so `row["value"]` is always the correct
    accessor.
    """
    from schemabrain.core.store import SCHEMA_VERSION

    try:
        row = conn.execute(
            "SELECT value FROM schemabrain_meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.DatabaseError:
        return
    if row is None:
        print(
            f"warning: store at {path} has no schema_version record — "
            f"column shape may not match this build's expectations",
            file=sys.stderr,
        )
        return
    stored = row["value"]
    if stored != SCHEMA_VERSION:
        # Cap the echoed value: a crafted store could insert a multi-MB
        # value here and flood stderr. `repr` (via `!r` below) escapes
        # control chars in whatever we DO print.
        if len(stored) > _MAX_SCHEMA_VERSION_ECHO:
            stored = stored[:_MAX_SCHEMA_VERSION_ECHO] + "..."
        print(
            f"warning: store at {path} has schema_version={stored!r} "
            f"but this build expects {SCHEMA_VERSION!r} — output may "
            f"reflect a different column shape than the current code "
            f"expects",
            file=sys.stderr,
        )


def _cmd_audit_verify(*, store_path: str, full: bool, since: str | None) -> int:
    """Walk `mcp_audit`'s chain hash; report mismatches.

    Opens the store as a read-only cursor so a concurrent `serve`
    process can keep writing audit rows. Exit code 0 means the chain
    is intact (no mismatches found); 1 means at least one mismatch;
    2 means an operational error (missing store, malformed --since,
    --since resolves to no cursor row).

    `since` (optional) anchors the walk to a known-good cursor row:
    a hex prefix of `chain_hash` (≥8 chars), a duration like `7d`,
    or an ISO 8601 timestamp. The cursor row's own integrity is NOT
    verified — operator must have archived a trusted copy of the
    chain_hash separately. Rows after the cursor are verified using
    the cursor's stored chain_hash as the trusted baseline.
    """
    import sqlite3 as _sqlite3
    import sys as _sys
    from pathlib import Path as _Path

    from schemabrain.audit.verify import SinceCursorError, resolve_since_cursor, walk_chain

    path = _Path(store_path).expanduser()
    if not path.exists():
        print(f"error: store file not found at {path}", file=_sys.stderr)
        return 2

    try:
        conn = _sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except _sqlite3.DatabaseError as exc:
        print(
            f"error: cannot open {path} as a SQLite database: {exc}",
            file=_sys.stderr,
        )
        return 2
    try:
        # row_factory + drift-warn live inside the try so an
        # unexpected raise from either still closes `conn`.
        conn.row_factory = _sqlite3.Row
        _warn_on_schema_drift(conn, path)

        # Resolve `--since` to a cursor row id BEFORE the walk so the
        # operator gets a clean exit-2 error when the spec is malformed
        # or unmatched, rather than a confusing zero-mismatch result.
        start_after_row_id: int | None = None
        if since is not None:
            try:
                start_after_row_id = resolve_since_cursor(conn, since)
            except SinceCursorError as exc:
                print(f"error: {exc}", file=_sys.stderr)
                return 2
            except ValueError as exc:
                # `parse_since` raises bare ValueError for malformed
                # duration / ISO strings; surface the same exit code.
                print(f"error: {exc}", file=_sys.stderr)
                return 2

        try:
            mismatches = list(walk_chain(conn, full=full, start_after_row_id=start_after_row_id))
            # Stats are only read on the success path — when the
            # chain has zero mismatches we render the intact-summary
            # block; mismatched runs go straight to per-row mismatch
            # rendering without touching the stats helper. Keeping
            # the assignment co-located with the chain walk shares
            # the same `conn` + outer `except` and avoids re-opening
            # the connection in a second branch.
            stats = (
                _read_audit_chain_stats(conn, start_after_row_id=start_after_row_id)
                if not mismatches
                else None
            )
        except _sqlite3.DatabaseError as exc:
            print(f"error: SQLite read failed: {exc}", file=_sys.stderr)
            return 2
    finally:
        conn.close()

    if not mismatches:
        # `stats is not None` follows from `not mismatches` (same
        # guard above gated the read); narrow for mypy.
        assert stats is not None
        _render_audit_chain_intact(stats, since_cursor_row_id=start_after_row_id)
        return 0

    for m in mismatches:
        print(f"row {m.row_id}: chain mismatch  expected={m.expected_hex}  actual={m.actual_hex}")
    print(f"\n{len(mismatches)} mismatch(es) reported.", file=_sys.stderr)
    return 1


def _read_audit_chain_stats(
    conn: sqlite3.Connection,
    *,
    start_after_row_id: int | None = None,
) -> dict[str, object]:
    """Read summary stats for a chain-verified `mcp_audit` table.

    Only called once `walk_chain` returned zero mismatches, so the
    counts are over a chain that has just been proven intact. Empty
    table is normal (fresh store) — row_count == 0 then signals the
    renderer to skip the "all N rows preserve..." claim line.

    When `start_after_row_id` is set (`--since` cursor walk), stats
    are restricted to the post-cursor segment — otherwise the footer
    counts would silently contradict the headline ("intact after row
    N" with "all M rows preserve invariant" where M is total, not
    post-cursor).

    Fingerprint version aggregates as the distinct set rather than a
    single value so a multi-version table doesn't silently report one
    of them — operators auditing a long-running deployment want to see
    every version that landed.
    """
    if start_after_row_id is None:
        row = conn.execute(
            "SELECT COUNT(*) AS n, "
            "MIN(occurred_at) AS earliest, "
            "MAX(occurred_at) AS latest "
            "FROM mcp_audit"
        ).fetchone()
        fp_versions = [
            r["fingerprint_version"]
            for r in conn.execute(
                "SELECT DISTINCT fingerprint_version FROM mcp_audit "
                "WHERE fingerprint_version IS NOT NULL "
                "ORDER BY fingerprint_version"
            )
        ]
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS n, "
            "MIN(occurred_at) AS earliest, "
            "MAX(occurred_at) AS latest "
            "FROM mcp_audit WHERE id > ?",
            (start_after_row_id,),
        ).fetchone()
        fp_versions = [
            r["fingerprint_version"]
            for r in conn.execute(
                "SELECT DISTINCT fingerprint_version FROM mcp_audit "
                "WHERE id > ? AND fingerprint_version IS NOT NULL "
                "ORDER BY fingerprint_version",
                (start_after_row_id,),
            )
        ]
    return {
        "row_count": int(row["n"]),
        "earliest": row["earliest"],
        "latest": row["latest"],
        "fp_versions": fp_versions,
    }


def _render_audit_chain_intact(
    stats: dict[str, object],
    *,
    since_cursor_row_id: int | None = None,
) -> None:
    """Render the verified-intact summary as a Rich stats block + claim lines.

    Writes to stdout (success output, not error) so callers piping to
    a file or grep capture the report normally. The block has three
    claim lines: chain-hash validity, append-only invariant, and
    fingerprint-version consistency. The "no DELETE / UPDATE
    attempted" claim from the demo vision is deliberately omitted —
    we cannot prove it from the read-only verifier alone (the
    no-update trigger lives in the schema; verifying it requires a
    separate check), and a false claim is worse than a missing one.

    `since_cursor_row_id` (optional) names a trust-anchor row from a
    `--since <hash>` / `<duration>` / `<iso>` walk. When set, the
    headline narrows the integrity claim to "after row N" so the
    operator does not misread a since-walk as a full-chain proof.
    """
    from rich.console import Console

    console = Console()
    row_count = int(stats["row_count"])
    fp_versions = list(stats["fp_versions"])  # type: ignore[arg-type]
    fp_label = ", ".join(fp_versions) if fp_versions else "n/a"
    header = (
        "[dim]Verifying audit chain integrity...[/]"
        if since_cursor_row_id is None
        else f"[dim]Verifying audit chain integrity (since row {since_cursor_row_id})...[/]"
    )
    console.print(header)
    console.print()
    console.print(f"  Audit rows:           [bold]{row_count}[/]")
    console.print(f"  Chain length:         [bold]{row_count}[/] hops")
    console.print(f"  Fingerprint version:  [bold]{fp_label}[/]")
    if row_count > 0:
        console.print(f"  Earliest event:       {stats['earliest']}")
        console.print(f"  Latest event:         {stats['latest']}")
    console.print()
    # Three claim lines that together prove chain integrity. The
    # primary signal — "chain intact — no tampering detected" — gets
    # full-weight green ✓; the two reinforcement claims (append-only
    # preserved, fp version consistent) deliberately render dim so
    # the eye lands on line 1 first and treats the rest as
    # corroborating detail rather than three competing headlines.
    #
    # Markup nesting note: `[dim][green]✓[/green]...[/dim]` is
    # deliberate. Rich composes parent dim + child green into a single
    # SGR style (`2;32m`) — the glyph renders dim AND green together.
    # Do NOT collapse to `[dim]✓ ...[/dim]` thinking the green is
    # redundant; the glyph would lose its semantic colour and the
    # corroborating lines would visually drift from the primary
    # green-✓ headline above.
    primary_claim = (
        "[green]✓[/] audit chain intact — no tampering detected"
        if since_cursor_row_id is None
        else (
            f"[green]✓[/] audit chain intact after row {since_cursor_row_id} "
            "— no tampering detected in the post-cursor segment"
        )
    )
    console.print(primary_claim)
    if row_count > 0:
        # Pre-cursor rows are skipped, not verified — soften the
        # "all N rows" claim to "N rows after the cursor" when a
        # since-walk is in effect so the operator does not infer
        # a full-history guarantee from a since-anchored result.
        if since_cursor_row_id is None:
            console.print(
                f"[dim][green]✓[/green] all {row_count} row(s) preserve append-only invariant[/dim]"
            )
        else:
            console.print(
                f"[dim][green]✓[/green] {row_count} row(s) after the cursor "
                "preserve append-only invariant[/dim]"
            )
    if len(fp_versions) <= 1:
        console.print("[dim][green]✓[/green] fingerprint version consistent across all rows[/dim]")
    else:
        # Multiple fp versions is not a failure — it's the expected
        # shape after a deployment crosses a version bump. Render in
        # neutral yellow so it reads as informational rather than as
        # one of the green-✓ corroborating claims.
        console.print(
            f"[yellow]i[/] {len(fp_versions)} fingerprint versions present "
            "(expected when a deployment has crossed a version bump)"
        )


# Audit rows with `occurred_at` newer than this threshold render in
# compact `HH:MM:SS` form; older ones keep the full ISO string.
_AUDIT_RECENT_THRESHOLD_SECS: int = 24 * 3600


def _format_audit_occurred_at(iso: str, *, now: datetime) -> str:
    """Compact recent timestamps; keep older ones in full ISO form.

    Audit rows accumulate at high rate during active sessions; the full
    `YYYY-MM-DDTHH:MM:SS.ffffffZ` form dominates a terminal row. Within
    `_AUDIT_RECENT_THRESHOLD_SECS` of `now` the date is implied, so
    `HH:MM:SS` is enough to distinguish rows for an operator scanning a
    live audit. Beyond that the full ISO string carries again — date
    matters when a row could be from yesterday or last week.

    Future timestamps (clock skew, test-seeded rows) keep the full ISO
    form deliberately. A compact "14:30:00" with no date would mislead
    the operator into reading a future row as today's.

    Malformed timestamps (defensive — the writer never emits them) fall
    through to the raw string so the table still renders something
    rather than crashing the CLI.
    """
    try:
        parsed = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError:
        return iso
    delta_secs = (now - parsed).total_seconds()
    if 0 <= delta_secs < _AUDIT_RECENT_THRESHOLD_SECS:
        return parsed.strftime("%H:%M:%S")
    return iso


def _cmd_audit_list(
    *,
    store_path: str,
    since: str | None,
    status: str | None,
    tool: str | None,
    limit: int,
    json_mode: bool,
) -> int:
    """Print recent `mcp_audit` rows with optional filters."""
    import json as _json
    import sqlite3 as _sqlite3
    import sys as _sys
    from pathlib import Path as _Path

    from rich.console import Console as _Console
    from rich.table import Table as _Table

    from schemabrain.observability import parse_since

    path = _Path(store_path).expanduser()
    if not path.exists():
        print(f"error: store file not found at {path}", file=_sys.stderr)
        return 2

    since_iso: str | None = None
    if since is not None:
        try:
            since_iso = parse_since(since).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        except ValueError as exc:
            print(f"error: {exc}", file=_sys.stderr)
            return 2

    where_clauses: list[str] = []
    params: list[object] = []
    if since_iso is not None:
        where_clauses.append("occurred_at >= ?")
        params.append(since_iso)
    if status is not None:
        where_clauses.append("status = ?")
        params.append(status)
    if tool is not None:
        where_clauses.append("tool_name = ?")
        params.append(tool)
    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    params.append(limit)

    try:
        conn = _sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except _sqlite3.DatabaseError as exc:
        print(
            f"error: cannot open {path} as a SQLite database: {exc}",
            file=_sys.stderr,
        )
        return 2
    try:
        # row_factory + drift-warn live inside the try so an
        # unexpected raise from either still closes `conn`.
        conn.row_factory = _sqlite3.Row
        _warn_on_schema_drift(conn, path)
        # Column list hardcoded; `where_sql` assembled from hardcoded
        # clause fragments above (only `?` placeholders for user
        # values). All user-supplied filter values flow through
        # `params`. No user input enters the SQL string itself.
        sql = (
            "SELECT id, occurred_at, tool_name, status, cost_class, "  # nosec B608
            "pii_categories, fingerprint, fingerprint_version "
            f"FROM mcp_audit {where_sql} "
            "ORDER BY id DESC LIMIT ?"
        )
        try:
            rows = list(conn.execute(sql, params))
        except _sqlite3.DatabaseError as exc:
            print(f"error: SQLite read failed: {exc}", file=_sys.stderr)
            return 2
        # Differentiate "empty audit log" from "filters excluded
        # everything". A bare `no rows matched` was ambiguous — operators
        # couldn't tell whether they had no MCP traffic yet, or simply a
        # filter typo. Computed only when needed so the happy path
        # doesn't pay for an extra query.
        total_rows: int | None = None
        if not rows:
            try:
                total_rows = conn.execute("SELECT COUNT(*) FROM mcp_audit").fetchone()[0]
            except _sqlite3.DatabaseError:  # pragma: no cover — defensive
                total_rows = None
    finally:
        conn.close()

    if json_mode:
        for row in rows:
            payload = {
                "id": row["id"],
                "occurred_at": row["occurred_at"],
                "tool_name": row["tool_name"],
                "status": row["status"],
                "cost_class": row["cost_class"],
                "pii_categories": row["pii_categories"],
                "fingerprint": bytes(row["fingerprint"]).hex(),
                "fingerprint_version": row["fingerprint_version"],
            }
            print(_json.dumps(payload, separators=(",", ":")))
        return 0

    if not rows:
        if total_rows == 0:
            print("(audit log is empty — no MCP tool calls have run yet)")
            print(
                "  next: drive the MCP server (Claude Desktop, "
                "`examples/anthropic_demo.py`, or another MCP client) to "
                "produce audit rows."
            )
        elif where_clauses:
            suffix = f" (audit log has {total_rows} rows total)" if total_rows else ""
            print(f"no audit rows matched the filters{suffix}")
            print("  next: widen with `--since 24h` or drop `--status`/`--tool` filters.")
        else:  # pragma: no cover — empty without filters yet total>0 is impossible
            print("no audit rows in this view")
        return 0

    console = _Console()
    table = _Table(title=f"mcp_audit (showing {len(rows)} rows)")
    table.add_column("id", justify="right")
    table.add_column("occurred_at")
    table.add_column("tool")
    table.add_column("status")
    table.add_column("cost")
    table.add_column("pii")
    table.add_column("fingerprint", overflow="fold")
    now = datetime.now(UTC)
    for row in rows:
        pii_raw = row["pii_categories"] or ""
        pii_cell = pii_raw if pii_raw else "[dim](none)[/]"
        table.add_row(
            str(row["id"]),
            _format_audit_occurred_at(row["occurred_at"], now=now),
            row["tool_name"],
            row["status"],
            row["cost_class"],
            pii_cell,
            bytes(row["fingerprint"]).hex()[:16],
        )
    console.print(table)
    _render_audit_list_footer(console, rows)
    return 0


def _render_audit_list_footer(console: object, rows: list[sqlite3.Row]) -> None:
    """Render the status + cost-class summary footer under `audit list`.

    Aggregates over the rendered rows only (NOT all-time), so the
    footer mirrors what the operator sees in the table above. The
    `mcp_audit` schema has no dollar `cost_usd` field and no
    confidence/trust column — both would need a schema migration to
    surface honestly per call. The footer ships what the persisted
    data supports today: status counts + cost-class counts. The
    Charter-v1.2 trust signal lives on entity / metric / join
    provenance, not on audit rows, so an operator who wants a
    per-call trust footer is asking for a v15-schema feature.

    Empty `rows` is impossible at this call site (the caller's
    empty-branch returns before this function fires), so no
    early-return guard is needed.
    """
    # Collections.Counter would be more idiomatic but adds an import
    # for two small dicts; a plain loop is clearer at this size.
    status_counts: dict[str, int] = {}
    cost_counts: dict[str, int] = {}
    for row in rows:
        status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1
        cost_counts[row["cost_class"]] = cost_counts.get(row["cost_class"], 0) + 1

    # Deterministic order: status follows the Charter enum sequence;
    # cost_class is small → medium → large → refused.
    status_order = ("success", "empty", "partial", "degraded", "error", "refused")
    cost_order = ("small", "medium", "large", "refused")

    def _fmt(parts: dict[str, int], order: tuple[str, ...]) -> str:
        present = [f"{parts[k]} {k}" for k in order if parts.get(k)]
        return " · ".join(present)

    total = len(rows)
    status_breakdown = _fmt(status_counts, status_order)
    cost_breakdown = _fmt(cost_counts, cost_order)

    # The footer separator (Unicode box-drawing horizontal) reads as
    # a faint rule under the table without competing with Rich's own
    # table border glyphs. Two summary lines below it.
    console.print()  # type: ignore[attr-defined]
    console.print("[dim]" + ("─" * 60) + "[/]")  # type: ignore[attr-defined]
    console.print(  # type: ignore[attr-defined]
        f"[dim]Summary:[/] [bold]{total}[/] call{'' if total == 1 else 's'} "
        f"([dim]{status_breakdown}[/])"
    )
    console.print(f"[dim]Cost class:[/] {cost_breakdown}")  # type: ignore[attr-defined]


def _stderr_is_interactive_tty() -> bool:
    """True iff init can safely prompt — both stdin AND stderr are TTYs.

    Thin shim over ``_ui.stderr_is_interactive_tty`` (F3 lift) so
    every existing CLI callsite + test monkeypatch keeps working.
    The wizard imports the canonical helper directly to keep the
    test surface small.
    """
    from schemabrain._ui import stderr_is_interactive_tty

    return stderr_is_interactive_tty()


def _prompt_yes_no(question: str, *, default: bool) -> bool:
    """Ask the user a yes/no question via the shared ``_ui.prompt_yes_no``.

    Lazy-imported so the cli's import-cost path isn't affected when
    no subcommand needs interactive input. Thin shim over the
    primitive in ``_ui`` so the CLI's residual callsites (those
    without a console handy) share the same spinner-pause discipline
    as the wizard's inline prompts.
    """
    from schemabrain._ui import prompt_yes_no

    return prompt_yes_no(_stderr_console(), question, default=default)


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


# Wizard stage `status` → shared `_ui.status_glyph` tier name.
# The wizard's outcome vocabulary (``done`` / ``skipped`` / ``failed``)
# pre-dates the shared severity vocabulary that PR #72 established in
# ``schemabrain._ui`` (``ok`` / ``skipped`` / ``err`` / ``warn`` /
# ``active`` / ``pending``); this map is the translation seam.
#
# Visible glyph flip in PR #3: the previous ``skipped`` glyph ``↷``
# (Unicode RIGHTWARDS WAVE ARROW) becomes ``⊘`` (CIRCLED DIVISION
# SLASH) — the design's spec for a skipped stage. The flip lands here
# rather than as a constant change in ``_ui.py`` so the migration is
# auditable in the wizard re-render diff per the PR #72 fold comment.
#
# An unknown ``status`` (defensive — wizard outcomes are always one of
# the three known values) routes through ``status_glyph(\"err\")`` →
# ``(✗, red)`` to surface the routing gap visibly rather than silently.
_WIZARD_STATUS_TO_TIER: Final[dict[str, str]] = {
    "done": "ok",
    "skipped": "skipped",
    "failed": "err",
}

# Soft cap for wizard stage/abort panel width. Without this cap, the
# `expand=False` panels stretch to fit their longest body line — fine
# for short "✓ source reachable" outcomes, awful for long failure or
# recovery messages that blow the panel out to 200+ columns and force
# horizontal scrolling. 100 cells comfortably fits the longest
# recovery-hint sentences while still wrapping in modern 100/120/140
# column terminals. `min(console.width, ...)` keeps the panel from
# exceeding the actual terminal so narrow terminals (80) still render
# inside their viewport.
_STAGE_PANEL_MAX_WIDTH = 100


def _wizard_panel_width(console: object) -> int:
    """Width budget for one wizard Panel, soft-capped at `_STAGE_PANEL_MAX_WIDTH`.

    Reads `console.width` via getattr with a defensive fallback so a
    Console implementation without a `.width` attribute (custom stub
    in tests) doesn't crash the renderer. Falls back to the soft cap.
    """
    detected = getattr(console, "width", _STAGE_PANEL_MAX_WIDTH)
    return min(detected, _STAGE_PANEL_MAX_WIDTH)


# Total stage count for the wizard pipeline. Used as the abort
# denominator ("stage N of 7") so an early abort still labels the
# pipeline shape correctly. Must stay in sync with `DEFAULT_STAGES`.
_WIZARD_TOTAL_STAGES: int = 7


def _redact_stderr_credentials(stderr_text: str) -> str:
    """Strip embedded credentials from a captured stderr blob.

    `claude mcp add`'s stderr is currently safe (it doesn't echo
    argv), but the helper is defense-in-depth: if a future version
    of the Claude Code CLI starts including the redacted env value
    in its error path, we want the redaction to apply automatically.
    The regex matches any `<scheme>://user:pass@host/db`-style URL
    and replaces the credentials portion.
    """
    import re

    return re.sub(
        r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^@\s]+@",
        r"\1<redacted>@",
        stderr_text,
    )


def _format_duration(seconds: float) -> str:
    """Format a wall-clock duration as a one-decimal `X.Xs` string.

    Sub-second times still render with one decimal so a row of
    durations stays visually aligned (0.4s next to 6.1s next to
    0.0s). Larger values would benefit from "12.3s" / "1.5m"
    formatting; we keep it simple at v1 since stages typically
    complete well under 60 seconds.
    """
    return f"{seconds:.1f}s"


def _format_path_for_terminal(path: Path, *, max_width: int = 60) -> str:
    """Compact a filesystem path for terminal display.

    Macos Claude Desktop config paths run ~100 characters which wraps
    on 80-col terminals. The compacted form replaces HOME with `~`
    and, when still too long, left-truncates to `…/last/three/parts`
    so the meaningful tail of the path stays visible.

    `max_width` is a soft cap — the truncated form may exceed it if
    the deepest 3 components themselves are huge (rare). The
    expectation is "shorter than a wrapping render"; pixel-perfect
    width policing is the terminal's job.

    The truncated form is intentionally NOT round-trippable: an
    absolute path like `/Users/x/Library/Application Support/Claude/...`
    renders as `…/Application Support/Claude/...` with the leading
    `/Users/x` discarded. The result is a UI hint for identifying the
    file at a glance, not a path the caller can pass to `Path()` to
    recover the original location.
    """
    try:
        home = Path.home()
        if path.is_absolute() and path.is_relative_to(home):
            display = "~/" + str(path.relative_to(home))
        else:
            display = str(path)
    except (OSError, ValueError):  # pragma: no cover — defensive (Windows shares, no HOME)
        display = str(path)
    if len(display) <= max_width:
        return display
    # Keep the last 3 path components — usually parent/parent/file.json
    # — so the user can identify what was touched without reading the
    # whole path. Using `Path.parts` ensures the separator is platform-correct.
    parts = path.parts
    if len(parts) <= 3:
        # Path has 3 or fewer components — left-truncation can't shorten it.
        return display
    tail = Path(*parts[-3:])
    return "…/" + str(tail)


# Stages whose handlers commonly take long enough to need a visible
# "I'm working" cue. Stages 1, 5 are fast enough that a spinner
# would flash and clear before the eye registers it; stages 2, 3, 4
# routinely take 5-60s on real schemas. Without the `metrics`
# entry stage 4 can look frozen for ~minute on a real schema; the
# spinner restores symmetry with stage 3.
_SPINNER_STAGES: frozenset[str] = frozenset({"index", "entities", "metrics"})


@contextlib.contextmanager
def _suggest_llm_progress(
    *,
    action: str,
    model: str,
    cost_estimate_usd: float,
    cap_usd: float,
) -> Iterator[None]:
    """Show a cost preview + Rich Live elapsed timer around a standalone LLM call.

    F1 + D1: wizard stages 3+4 use ``live_llm_stage_progress``
    directly; the standalone commands (``entities suggest`` /
    ``metrics suggest``) thread through this helper for one
    additional gate: callers skip the whole context entirely on
    ``provider == "stub"`` (returns instantly; cost framing would
    lie). All other behavior — non-TTY auto-suppress, two-line
    preamble+timer display, exception propagation — comes from
    ``_ui.live_llm_stage_progress`` so cross-surface output stays
    pixel-identical.

    D1 (post-PR-#79 polish): replaced the F1 static-preamble +
    ticking-spinner shape (``print_llm_stage_preamble`` +
    ``console.status``) with the live elapsed-timer display.
    Operators on a ~30s LLM round-trip now see the elapsed seconds
    tick up instead of an opaque spinner — much clearer signal that
    the call is still in flight (vs hung).

    No ``--quiet`` flag is added to the suggest subparsers — TTY
    auto-detect (via ``console.is_terminal`` in
    ``live_llm_stage_progress``) covers the CI case, and shaving
    parser surface area beats per-command flag proliferation.
    Operators who want to suppress on a TTY can redirect stderr.
    """
    from schemabrain._ui import live_llm_stage_progress, make_console

    # Narrow try: only the Rich console construction. If `make_console`
    # itself raises (Rich init bug, broken terminfo), silently no-op
    # — the live display is a UX nicety, not a correctness
    # requirement. MUST NOT wrap the `yield` below: catching a
    # body-raised exception via the yield re-raise would double-yield
    # and trip contextlib's "generator didn't stop after throw()"
    # guard, masking the original exception from the F5 handler
    # downstream (the F1-era bug that this comment block preserves).
    try:
        console = make_console(stderr=True)
    except Exception:  # pragma: no cover — defensive against Rich init failures
        yield
        return

    with live_llm_stage_progress(
        console,
        action=action,
        model=model,
        cost_estimate_usd=cost_estimate_usd,
        cap_usd=cap_usd,
    ):
        yield


@contextlib.contextmanager
def _wizard_stage_context(stage: object) -> Iterator[None]:
    """Context manager wrapping each wizard stage handler with a spinner.

    Only the slow async stages (`index`, `entities`) get a spinner,
    and only when stderr is a TTY — CI logs and redirected output
    fall through to no-op so log scrapers don't get carriage-return
    confusion. The orchestrator passes this factory to `run_wizard`
    via its `stage_context` kwarg.

    Typed as `object` for the stage parameter so this module doesn't
    import `WizardStage` at module-import time (matches the lazy-import
    discipline elsewhere). The function relies on `stage.name` via
    `getattr` with a defensive empty-string fallback.

    Any failure constructing the `Console` (Rich initialisation bug,
    upstream import error) falls through to no-op rather than
    propagating — the spinner is a UX nicety, not a correctness
    guarantee, and a wizard crash with the header already on screen
    would be more confusing than no spinner.
    """
    from schemabrain._ui import register_active_spinner

    name: str = getattr(stage, "name", "") or ""
    if name not in _SPINNER_STAGES:
        yield
        return
    try:
        console = _stderr_console()
        if not console.is_terminal:
            yield
            return
    except Exception:  # pragma: no cover — defensive against Rich init failures
        yield
        return
    label = _stage_display_name(name)
    # `console.status(...)` returns a `Status` object that supports
    # both the context-manager protocol (start on `__enter__`, stop on
    # `__exit__`) and direct `.start()` / `.stop()` calls. The wizard's
    # interactive prompt code pauses the spinner via the latter — see
    # `_ui.pause_active_spinner` and `setup.wizard._prompt_llm_confirmation`.
    # Without registration the spinner kept rendering during
    # `input()` and read as "stage is already running"; registering
    # the active spinner here gives the prompt a handle to pause it
    # during the wait.
    #
    # Cleanup ordering note: in `with A, B:`, Python exits B first,
    # then A. Here that means `register_active_spinner.__exit__` runs
    # FIRST (clears the registry), then `status.__exit__` runs (stops
    # the spinner). The registry is cleared while the status is still
    # live, which is what we want: it prevents another thread (or a
    # re-entrant call) from `pause_active_spinner()`-ing a status
    # that's about to be cleaned up.
    status = console.status(f"[cyan]{label}…[/]", spinner="dots")
    with status, register_active_spinner(status):
        yield


def _host_display_name(host: str) -> str:
    """Map the kebab-case host identifier to a human display string.

    Used by the wizard renderer's orientation line. Unknown values
    pass through so a future host added to the literal doesn't crash
    the renderer before the lookup is updated.
    """
    return {
        "claude-desktop": "Claude Desktop",
        "claude-code": "Claude Code",
        "cursor": "Cursor",
        "windsurf": "Windsurf",
        "manual": "manual mode",
    }.get(host, host)


def _render_wizard_header(*, host_display: str | None, console: object) -> None:
    """Print the wizard's brand line.

    Split out of ``_render_wizard_result`` so the CLI can print this
    BEFORE the wizard starts running — without it the user stares at
    a blank terminal during slow stages (index, entities) before
    seeing anything at all.

    Rendered as a single flat line per the design's brand-line
    convention (handoff bundle ``cli/init.jsx`` line 20): a cyan
    ``◆`` brand glyph, the bold command name, and a dim
    activation-context tail. Replaces the previous bordered Panel —
    Panel chrome competes with the stage-list rule below and the
    operator's eye should land on the stages, not the framing.
    """
    # Lazy-import `_ui` here (rather than at module top) to keep
    # `cli.py` import-time light for subcommands that never enter
    # the wizard renderer (audit, mcp serve, fixture-path).
    from schemabrain._ui import GLYPH_BRAND

    activating = (
        f"— activating for [bold]{host_display}[/]. ~30s."
        if host_display
        else "— activating. ~30s."
    )
    console.print()  # type: ignore[attr-defined]
    console.print(  # type: ignore[attr-defined]
        f"[cyan]{GLYPH_BRAND}[/] [bold]SchemaBrain init[/] [dim]{activating}[/]"
    )
    console.print()  # type: ignore[attr-defined]


def _render_wizard_result(result: object, *, host_display: str | None = None) -> None:
    """Render the multi-stage outcome of a wizard run.

    Caller is `_cmd_init`. Typed as `object` here so the cli module
    doesn't import the wizard types at parse time, matching the
    lazy-import discipline elsewhere in the module.

    `host_display` is the human-readable host target (e.g.
    "Claude Desktop"). When provided, the orientation line mentions
    it; when None, a generic orientation is rendered. The caller
    derives this from `args.host` via `_host_display_name`.

    This entry point composes `_render_wizard_header` +
    `_render_wizard_after` so tests can keep using the single
    function. Production CLI flow calls them separately to land the
    header before the wizard runs.

    Layout:

      SchemaBrain
      Activating SchemaBrain for Claude Desktop. ~30s.

        [N/7] <stage display name>
              <glyph> <message>
              <indented next_step, if present>

    After the stage list, additional context lines render the host
    install detail (path + backup, redacted shell-out argv on
    failure, paste-ready JSON snippet for manual mode), and a
    closing block prints either the next-step hint (clean run) or a
    bordered failure panel (abort).
    """
    from schemabrain.setup.wizard import WizardResult

    if not isinstance(result, WizardResult):
        raise TypeError(f"_render_wizard_result expected WizardResult, got {type(result).__name__}")
    console = _stderr_console()
    _render_wizard_header(host_display=host_display, console=console)
    _render_wizard_after(result, host_display=host_display, console=console)


def _compose_progress_rule(
    result: WizardResult,
    *,
    total: int,
    console: object,
) -> Text:
    """Build the design's progress rule shown above the stage list.

    Returns a ``rich.text.Text`` (via ``_ui.top_rule``) summarising
    the wizard's run shape:

      ─── 7 stages ───────────────────── 21.0s · 1 advisory failure ───

    The rule fences the stage list inside a visual band so the
    operator's eye lands on "what shape was this run?" before
    scanning the individual rows. Cost is intentionally NOT
    surfaced here in PR #3 — wizard cost lives on stage-level
    results, not on ``WizardResult`` itself; threading a
    total-cost field through the data model is deferred until a
    follow-up PR (the live cost ticker the design specifies during
    execution, not the post-run summary).

    The right-side metadata adapts to the run shape:

    * Clean run → ``{elapsed}s``
    * Run with N skipped stages → ``{elapsed}s · {N} advisory``
    * Aborted run → ``{elapsed}s · stopped at stage {N}``

    Caller (``_render_wizard_after``) already narrows the input to
    ``WizardResult``; this helper trusts that contract rather than
    re-checking with a dead defensive guard.
    """
    # Lazy-import — see `_render_wizard_header` for the rationale.
    from schemabrain._ui import top_rule

    elapsed = sum(o.duration_s for o in result.outcomes)
    pieces: list[str] = [f"{_format_duration(elapsed)}"]
    if result.aborted and result.aborted_at is not None:
        pieces.append(f"stopped at stage {result.aborted_at.stage}")
    else:
        # Count advisory (skipped) outcomes — wizard stages 3 + 4 use
        # ``skipped`` for "the run continued without curating this
        # surface" (LLM cost cap, ``--no-entities``, advisory failure).
        # Surfacing the count tells the operator at a glance whether
        # the run was fully curated or partial without scanning rows.
        advisory_count = sum(1 for o in result.outcomes if o.status == "skipped")
        if advisory_count == 1:
            pieces.append("1 advisory")
        elif advisory_count > 1:
            pieces.append(f"{advisory_count} advisory")
    right = " · ".join(pieces)

    width = getattr(console, "width", 120)
    return top_rule(f"{total} stages", right, width=min(width, _STAGE_PANEL_MAX_WIDTH))


def _render_wizard_after(result: object, *, host_display: str | None, console: object) -> None:
    """Render everything that follows the wizard's header — progress
    rule, stage list, host install detail, closing block (clean run)
    or failure panel (abort).

    Split from ``_render_wizard_result`` so the CLI can call this
    after the wizard finishes — the header runs first (immediate
    feedback) and the spinner-driven stage-context callback fills
    the gap until the wizard returns.

    Layout per the design's State B hero (handoff bundle
    ``cli/init.jsx``):

      ─── 7 stages ────────────────────── 21.0s · 0 advisory ──

        01  ✓  source_check  ok                              0.4s
            ↳ next-step hint, if present (dim, indented)
        02  ✓  index         ok                              6.1s
        ...

    Stages render as a compact column rather than per-stage Panels:
    one row per outcome, glyph + ordinal + display name + message +
    duration. A dim follow-up row carries the ``next_step`` hint
    when one is set, indented under the message column so the
    operator's eye stays in the same vertical track.

    Wire-host install detail (config path, backup, manual snippet)
    renders after the stage table — ``printed_only`` writes the JSON
    snippet to stdout (machine-readable) and a Table around mixed
    stderr/stdout output would break the JSON consumer's parse.
    """
    # Lazy-import — see `_render_wizard_header` for the rationale.
    from rich.table import Table

    from schemabrain._ui import status_glyph
    from schemabrain.setup.wizard import WizardResult

    if not isinstance(result, WizardResult):
        raise TypeError(f"_render_wizard_after expected WizardResult, got {type(result).__name__}")
    # The wizard always has 7 stages even on early abort — using
    # `len(result.outcomes)` for the denominator would render "of 2"
    # on a stage-2 abort, misleading the user about the pipeline shape.
    total = _WIZARD_TOTAL_STAGES

    console.print(_compose_progress_rule(result, total=total, console=console))  # type: ignore[attr-defined]
    console.print()  # type: ignore[attr-defined]

    # Invariant cell styles (ordinal, name, duration) live on the
    # column rather than embedded in each cell's Rich markup — that
    # keeps data and presentation separate and means a future
    # palette change flips one place. The glyph and message
    # columns vary per-row so their styles stay inline.
    #
    # `table.width` is set post-construction to the
    # `_wizard_panel_width(console)` soft cap (100 cols min). The
    # previous per-stage Panel rendering enforced this; without it,
    # very wide terminals fold long messages much later than
    # before, breaking the visual column the abort panel and
    # closing block still use. `Table.grid()` doesn't accept
    # `width` directly so the assignment lives one line below.
    table = Table.grid(padding=(0, 2), expand=False)
    table.width = _wizard_panel_width(console)
    table.add_column(no_wrap=True, style="bright_black")  # ordinal "01"
    table.add_column(no_wrap=True)  # glyph (per-cell style — varies by status)
    table.add_column(no_wrap=True, style="bold")  # display name
    table.add_column(no_wrap=False, overflow="fold")  # message + optional next-step
    table.add_column(justify="right", no_wrap=True, style="bright_black")  # duration

    pending_wire_host_detail = None
    for outcome in result.outcomes:
        tier = _WIZARD_STATUS_TO_TIER.get(outcome.status, "err")
        glyph, style = status_glyph(tier)
        ordinal = f"{outcome.stage:02d}"
        glyph_cell = f"[{style}]{glyph}[/]"
        name_cell = _stage_display_name(outcome.name)
        msg_cell = outcome.message
        # Sub-50ms durations represent peek-and-bypass stages where
        # the orchestrator measured `perf_counter` but no real work
        # happened — rendering "0.0s" next to them would mislead.
        duration_cell = _format_duration(outcome.duration_s) if outcome.duration_s >= 0.05 else ""
        table.add_row(ordinal, glyph_cell, name_cell, msg_cell, duration_cell)
        if outcome.next_step:
            # Follow-up hint indented under the message column —
            # operator's eye stays in the same vertical track as the
            # primary outcome rather than jumping back to the gutter.
            table.add_row("", "", "", f"[dim]{outcome.next_step}[/]", "")
        # Stage-6 host install detail (config path / backup / manual
        # JSON snippet) renders AFTER the stage table — see method
        # docstring for the mixed-stream rationale.
        if outcome.name == "wire_host" and outcome.status == "done":
            pending_wire_host_detail = result.host_install_result

    console.print(table)  # type: ignore[attr-defined]
    if pending_wire_host_detail is not None:
        console.print()  # type: ignore[attr-defined]
        _render_wire_host_detail(pending_wire_host_detail, console)
    console.print()  # type: ignore[attr-defined]

    if result.aborted:
        _render_abort_panel(result, total=total, console=console)
        return

    # Clean run — print the closing block (rule + restart-or-snippet
    # prompt + tail/audit hints + thesis tagline). Skipped on
    # shell_out_failed (the existing Note covers the recovery — adding
    # a "Restart Claude Code" line would contradict it). Any other
    # non-failed state (today: written / unchanged / printed_only;
    # future-proof against new states added to `InitResult.state`)
    # gets the closing block so the user always sees the next-step
    # copy, never a silent no-output succeed.
    host_result = result.host_install_result
    if host_result is not None and host_result.state == "shell_out_failed":
        console.print(  # type: ignore[attr-defined]
            "  [dim]Note:[/] `claude mcp add` failed; you can run the redacted "
            "command above with real credentials to register manually."
        )
        return
    if host_result is not None:
        _render_closing_block(result, host_display=host_display, console=console)


def _render_abort_panel(result: object, *, total: int, console: object) -> None:
    """Render a bordered failure panel for aborted wizard runs.

    Replaces the previous single red line ("wizard aborted at stage N
    of M.") with a Rich Panel — visually contains the failure, the
    title carries the stage ordinal, and the body shows the message +
    recovery hint without ambiguity.
    """
    from rich.panel import Panel

    from schemabrain.setup.wizard import WizardResult

    if not isinstance(result, WizardResult):
        return  # pragma: no cover — defensive; caller already narrowed
    aborted = result.aborted_at
    title = f"Stopped at stage {aborted.stage if aborted else '?'} of {total}"
    body_lines: list[str] = []
    if aborted is not None:
        body_lines.append(aborted.message)
        if aborted.next_step:
            body_lines.append("")
            body_lines.append(f"[dim]{aborted.next_step}[/]")
    body = "\n".join(body_lines) if body_lines else "(no failure detail recorded)"
    console.print(  # type: ignore[attr-defined]
        Panel(
            body,
            title=title,
            border_style="red",
            expand=False,
            width=_wizard_panel_width(console),
        )
    )


def _ui_extra_importable() -> bool:
    """True when the optional ``[ui]`` extra is installed.

    The dashboard sidecar refuses to boot without ``uvicorn`` (see
    ``dashboard/cli.py``), so ``uvicorn`` is the canonical sentinel for
    "the ``[ui]`` extra is present". Uses ``importlib.util.find_spec`` so
    we never pay the (heavy) ``uvicorn`` import just to decide which
    closing-block copy to show.
    """
    import importlib.util

    return importlib.util.find_spec("uvicorn") is not None


def _graph_route_in_wheel() -> bool:
    """True when the built dashboard static export ships the ``/graph`` route.

    The graph payoff points the operator at the dashboard's knowledge-graph
    surface; that surface only exists if ``graph.html`` was bundled into the
    wheel (``pnpm run export`` + the ``publish.yml`` per-route sentinel). A
    bare source/sdist checkout without a built export has no ``graph.html``,
    so the payoff falls back to a plain dashboard hint instead of dangling.
    """
    from schemabrain.dashboard import STATIC_DIR

    return (STATIC_DIR / "graph.html").exists()


def _graph_payoff_available() -> bool:
    """Gate for the init closing block's graph call-to-action.

    Lead with the graph payoff only when it can actually be delivered: the
    ``[ui]`` extra is importable AND the ``/graph`` route shipped in the
    wheel. Otherwise the closing block keeps the restart prompt as the lead
    and shows an install/dashboard hint (wsINIT-graph-payoff). This is a
    printed call-to-action only — init never launches a browser, so the
    "no auto-open in CI" invariant holds by construction.
    """
    return _ui_extra_importable() and _graph_route_in_wheel()


def _render_closing_block(
    wizard_result: object,
    *,
    host_display: str | None,
    console: object,
) -> None:
    """Render the post-stage closing block for clean runs.

    Layout:

      ──────────────────────────────────────────────────────────────
      Restart Claude Desktop, then ask:
      > list the entities SchemaBrain knows about

      [pending-action block, only when stage 3 did not curate entities]

      Inspect activity:  schemabrain tail --follow
      Review the audit:  schemabrain audit list

      The agent reads. It doesn't write. That's the whole point.

    Manual mode swaps the restart line for "Add the snippet above to
    your host config, then ask:" since there's nothing to restart yet.

    The pending-action block surfaces stage 3's recovery hint inside
    the closing block — so a user who skipped entity curation (missing
    `ANTHROPIC_API_KEY`, `--no-entities`, or a stage failure) sees the
    next concrete step without scrolling back up to the stage list.
    Without this block the user lands at "ask: list the entities ..."
    and the agent honestly answers "no entities are configured" — a
    dead end. The block restores the trajectory.
    """
    from schemabrain.setup.init_flow import InitResult
    from schemabrain.setup.wizard import WizardResult

    if not isinstance(wizard_result, WizardResult):
        return  # pragma: no cover — defensive; caller already narrowed
    host_result = wizard_result.host_install_result
    if not isinstance(host_result, InitResult):
        return  # pragma: no cover — defensive; caller only calls this after a stage-4 success
    console.print("[dim]" + "─" * 62 + "[/]")  # type: ignore[attr-defined]
    # wsINIT-graph-payoff: when the dashboard graph view can actually be
    # shown ([ui] importable + /graph in the wheel), LEAD with it — the
    # signature payoff of an init run is "your schema is now a navigable
    # knowledge graph", not "go restart your MCP host". The host-restart
    # prompt stays (agents still need the reload to discover the tools) but
    # becomes the secondary step by position. Nothing auto-opens here — this
    # is a printed call-to-action, so CI / headless runs are safe by default.
    graph_payoff = _graph_payoff_available()
    if graph_payoff:
        console.print("Your schema is now a [bold]knowledge graph[/].")  # type: ignore[attr-defined]
        console.print(  # type: ignore[attr-defined]
            "  See it:  [bold]schemabrain dashboard[/]  "
            "[dim]→ your schema as an interactive graph[/]"
        )
        console.print()  # type: ignore[attr-defined]
    if host_result.state == "printed_only":
        console.print("Add the snippet above to your host config, then ask:")  # type: ignore[attr-defined]
    else:
        target = host_display or "your MCP host"
        console.print(f"Restart {target}, then ask:")  # type: ignore[attr-defined]
    console.print("[cyan]>[/] list the entities SchemaBrain knows about")  # type: ignore[attr-defined]
    # UX audit #12: show the config path so the operator knows where
    # the entry landed without scrolling back up to stage 6 or running
    # `schemabrain doctor`. Surfaces only for claude-desktop where the
    # path is a JSON file the operator can inspect / cat / open in an
    # editor. claude-code's `claude mcp add` shell-out and manual mode
    # have no operator-visible file to point at.
    if host_result.state in ("written", "unchanged") and host_result.config_path is not None:
        console.print(  # type: ignore[attr-defined]
            f"[dim]config written: {host_result.config_path}[/]"
        )
    _render_cold_start_flare(host_result, console=console)
    console.print()  # type: ignore[attr-defined]
    _render_pending_entity_block(wizard_result, console=console)
    _render_pending_metrics_block(wizard_result, console=console)
    _render_pending_joins_block(wizard_result, console=console)
    # Note: agent steering ("call find_relevant_entities first, don't
    # fall back to list_tables") ships in the MCP server's initialize
    # response (`_SERVER_INSTRUCTIONS` in mcp/server.py) — Claude
    # Desktop / Cursor / Windsurf / Claude Code all honor it.
    # Wizard print of the same snippet would be duplicative and leave
    # the user wondering where to paste it.
    console.print("Inspect activity:  [bold]schemabrain tail --follow[/]")  # type: ignore[attr-defined]
    console.print("Review the audit:  [bold]schemabrain audit list[/]")  # type: ignore[attr-defined]
    # Day-one UX overhaul: discovery links for the other commands a
    # new user benefits from after init. `inspect` is the most
    # common next step (see what was curated); `doctor` verifies
    # the wiring on demand; `check` runs the drift check after
    # schema changes. Kept short — one line per command, dim
    # styling so it reads as supplementary, not as the primary
    # call-to-action.
    console.print("See what was curated:  [bold]schemabrain inspect[/]")  # type: ignore[attr-defined]
    console.print("Verify the wiring:     [bold]schemabrain doctor[/]")  # type: ignore[attr-defined]
    console.print("Detect schema drift:   [bold]schemabrain check[/]")  # type: ignore[attr-defined]
    # Dashboard discovery line — only when we did NOT already lead with the
    # graph payoff (avoids duplicating the CTA). The dashboard is opt-in via
    # the `[ui]` extra: an indie dev who ran `pip install schemabrain` (no
    # extras) would otherwise never discover this surface exists, since it
    # doesn't show up in the init flow or any `--help`-level catalog. When
    # the extra is present but the export isn't built (dev/sdist), just name
    # the command; when it's missing, append the install hint.
    if not graph_payoff:
        if _ui_extra_importable():
            console.print(  # type: ignore[attr-defined]
                "See your schema as a graph:  [bold]schemabrain dashboard[/]"
            )
        else:
            # `\[ui]` escapes the bracket so Rich renders a literal
            # `schemabrain[ui]` rather than parsing `[ui]` as a (dropped)
            # markup tag — the un-escaped form silently shipped
            # `pip install 'schemabrain'` (the extra vanished).
            console.print(  # type: ignore[attr-defined]
                "See your schema as a graph:  [bold]schemabrain dashboard[/]  "
                "[dim](pip install 'schemabrain\\[ui]')[/]"
            )
    console.print()  # type: ignore[attr-defined]
    console.print("[dim]The agent reads. It doesn't write. That's the whole point.[/]")  # type: ignore[attr-defined]


_CLAUDE_DESKTOP_COLD_START_BODY = (
    "Claude Desktop only reads MCP configs on cold start. After init "
    "writes the entry, fully quit Claude Desktop ([bold]Cmd+Q on macOS[/], "
    "[bold]Ctrl+Q on Linux[/], [bold]File → Exit on Windows[/]) and "
    "reopen — a regular window-close is not enough."
)
"""Body copy for `_render_cold_start_flare`.

The flare fires only on the `claude-desktop` host because Claude Desktop
is the host whose reload behavior tripped DevRel-flagged "doesn't work"
reports (operators saw the wizard succeed, closed the Claude window,
and assumed the wiring was broken when the MCP entry hadn't loaded).
Claude Code's `claude mcp add` reloads in-process; the cursor and
windsurf hosts read their configs on every prompt cycle — no panel
needed for those.
"""


def _render_cold_start_flare(host_result: object, *, console: object) -> None:
    """Bold-bordered Rich panel reminding Claude Desktop users to fully quit.

    Renders only when the wizard wrote (or no-opped on) a claude-desktop
    config. Skipped on:

    - `claude-code` (shell-out reloads in-process)
    - `manual` / `printed_only` (operator hasn't installed anything yet —
      the cold-start instruction would land before the entry exists)
    - `shell_out_failed` (the existing fallback Note covers recovery)

    The body's cross-platform copy ([Cmd+Q] / [Ctrl+Q] / [File → Exit])
    keeps the panel useful without requiring a `platform.system()` probe.
    """
    from rich.panel import Panel

    from schemabrain.setup.init_flow import InitResult

    if not isinstance(host_result, InitResult):
        return  # pragma: no cover — defensive; caller already narrowed
    if host_result.host != "claude-desktop":
        return
    if host_result.state not in ("written", "unchanged"):
        return  # pragma: no cover — claude-desktop state in {shell_out_*, printed_only} is unreachable in real flow; defensive guard for future InitState additions
    console.print(  # type: ignore[attr-defined]
        Panel(
            _CLAUDE_DESKTOP_COLD_START_BODY,
            title="Restart Claude Desktop",
            border_style="bold",
            expand=False,
            width=_wizard_panel_width(console),
        )
    )


def _render_pending_entity_block(wizard_result: object, *, console: object) -> None:
    """Surface stage 3 (entities) recovery hint when curation did not complete.

    Scans `wizard_result.outcomes` for the entities stage. Renders one
    of three short blocks based on the outcome's status + message
    prefix:

    - `ANTHROPIC_API_KEY not set` (skipped) → API-key recovery
    - `--no-entities set` (skipped, explicit opt-out) → opt-in pointer
    - any other `skipped`/`failed` outcome → generic retry pointer

    Renders nothing when stage 3 succeeded (`status="done"` with
    applied_count > 0) or when stage 3 didn't run (wizard aborted
    earlier — the abort panel covers that path).

    The match strings here are coupled to the prefixes the wizard
    handler writes in `schemabrain/setup/wizard.py::_stage_entities`.
    If you change them there, update this matcher too — the closing
    block surfaces the same recovery action the stage's dim
    `next_step` line shows below the stage outcome, so the two must
    say compatible things.
    """
    from schemabrain.setup.wizard import WizardResult

    if not isinstance(wizard_result, WizardResult):
        return  # pragma: no cover — defensive; caller already narrowed
    entities_outcome = next(
        (o for o in wizard_result.outcomes if o.name == "entities"),
        None,
    )
    if entities_outcome is None or entities_outcome.status == "done":
        return
    if entities_outcome.message.startswith("already curated:"):
        # Idempotent re-run on a store that already has entities. The
        # status is `skipped` but the user is in the happy path: the
        # ask line is honest. No pending block.
        return
    if entities_outcome.message.startswith("ANTHROPIC_API_KEY not set"):
        console.print(  # type: ignore[attr-defined]
            "To curate entities (let SchemaBrain understand customer/order/...):"
        )
        console.print("  [dim]export[/] ANTHROPIC_API_KEY=sk-ant-...")  # type: ignore[attr-defined]
        console.print("  schemabrain entities suggest --apply")  # type: ignore[attr-defined]
        console.print()  # type: ignore[attr-defined]
        return
    if entities_outcome.message.startswith("--no-entities set"):
        console.print("Curate entities when ready:")  # type: ignore[attr-defined]
        console.print("  schemabrain entities suggest --apply")  # type: ignore[attr-defined]
        console.print()  # type: ignore[attr-defined]
        return
    # Generic recovery: any other skipped/failed status (--skip-index,
    # non-Postgres source, transient failure, LLM-returned-zero-
    # candidates, partial write). The two prefixes that DO carry
    # tailored copy are short-circuited above; everything else lands
    # here.
    console.print("Stage 3 did not curate entities (see above). Retry when ready:")  # type: ignore[attr-defined]
    console.print("  schemabrain entities suggest --apply")  # type: ignore[attr-defined]
    console.print()  # type: ignore[attr-defined]


def _render_pending_metrics_block(wizard_result: object, *, console: object) -> None:
    """Surface stage 4 (metrics) recovery hint when curation did not complete.

    Mirror of `_render_pending_entity_block`. Scans
    `wizard_result.outcomes` for the metrics stage and renders one of
    four short blocks based on the outcome's status + message prefix:

    - `ANTHROPIC_API_KEY not set` (skipped) → API-key recovery
    - `--no-metrics set` (skipped, explicit opt-out) → opt-in pointer
    - `entity store is empty` (skipped, cross-stage dependency) →
      pointer at curating entities first
    - any other `skipped`/`failed` outcome → generic retry pointer

    Renders nothing when stage 4 succeeded, was idempotently
    short-circuited on already-curated, or didn't run (wizard
    aborted earlier — the abort panel covers that).

    The match strings here are coupled to the prefixes the wizard
    handler writes in `schemabrain/setup/wizard.py::_stage_metrics`.
    If you change them there, update this matcher too — the closing
    block surfaces the same recovery action the stage's dim
    `next_step` line shows below the stage outcome, so the two must
    say compatible things.
    """
    from schemabrain.setup.wizard import WizardResult

    if not isinstance(wizard_result, WizardResult):
        return  # pragma: no cover — defensive; caller already narrowed
    metrics_outcome = next(
        (o for o in wizard_result.outcomes if o.name == "metrics"),
        None,
    )
    if metrics_outcome is None or metrics_outcome.status == "done":
        return
    if metrics_outcome.message.startswith("already curated:"):
        # Idempotent re-run on a store that already has metrics. The
        # status is `skipped` but the user is in the happy path. No
        # pending block.
        return
    if metrics_outcome.message.startswith("ANTHROPIC_API_KEY not set"):
        console.print(  # type: ignore[attr-defined]
            "To curate metrics (revenue / orders_placed / aov / ...):"
        )
        console.print("  [dim]export[/] ANTHROPIC_API_KEY=sk-ant-...")  # type: ignore[attr-defined]
        console.print("  schemabrain metrics suggest --apply")  # type: ignore[attr-defined]
        console.print()  # type: ignore[attr-defined]
        return
    if metrics_outcome.message.startswith("--no-metrics set"):
        console.print("Curate metrics when ready:")  # type: ignore[attr-defined]
        console.print("  schemabrain metrics suggest --apply")  # type: ignore[attr-defined]
        console.print()  # type: ignore[attr-defined]
        return
    if metrics_outcome.message.startswith("entity store is empty"):
        # Cross-stage dependency surfaced: entities must land before
        # metrics. The block points at entity curation first so the
        # user knows the order.
        console.print("Metrics anchor on entities. Curate entities first, then metrics:")  # type: ignore[attr-defined]
        console.print("  schemabrain entities suggest --apply")  # type: ignore[attr-defined]
        console.print("  schemabrain metrics suggest --apply")  # type: ignore[attr-defined]
        console.print()  # type: ignore[attr-defined]
        return
    # Generic recovery: any other skipped/failed status.
    console.print("Stage 4 did not curate metrics (see above). Retry when ready:")  # type: ignore[attr-defined]
    console.print("  schemabrain metrics suggest --apply")  # type: ignore[attr-defined]
    console.print()  # type: ignore[attr-defined]


def _render_pending_joins_block(wizard_result: object, *, console: object) -> None:
    """Surface stage 5 (joins) recovery hint when curation did not complete.

    Mirror of `_render_pending_metrics_block`, but with one fewer
    branch — joins is deterministic (FK + query-log mining), so
    there is no ANTHROPIC_API_KEY branch to handle.

    Three branches based on the outcome's status + message prefix:

    - `--no-joins set` (skipped, explicit opt-out) → opt-in pointer
    - `entity store is empty` (skipped, cross-stage dependency) →
      pointer at curating entities first
    - any other `skipped`/`failed` outcome → generic retry pointer

    Renders nothing when stage 5 succeeded, was idempotently
    short-circuited on already-curated, or didn't run (wizard
    aborted earlier — the abort panel covers that).

    The match strings here are coupled to the prefixes the wizard
    handler writes in `schemabrain/setup/wizard.py::_stage_joins`.
    If you change them there, update this matcher too.
    """
    from schemabrain.setup.wizard import WizardResult

    if not isinstance(wizard_result, WizardResult):
        return  # pragma: no cover — defensive; caller already narrowed
    joins_outcome = next(
        (o for o in wizard_result.outcomes if o.name == "joins"),
        None,
    )
    if joins_outcome is None or joins_outcome.status == "done":
        return
    if joins_outcome.message.startswith("already curated:"):
        return
    if joins_outcome.message.startswith("--no-joins set"):
        console.print("Curate joins when ready:")  # type: ignore[attr-defined]
        console.print("  schemabrain joins suggest --apply")  # type: ignore[attr-defined]
        console.print()  # type: ignore[attr-defined]
        return
    if joins_outcome.message.startswith("entity store is empty"):
        # Cross-stage dependency surfaced: entities must land before
        # joins. Point at entity curation first.
        console.print("Joins anchor on entities. Curate entities first, then joins:")  # type: ignore[attr-defined]
        console.print("  schemabrain entities suggest --apply")  # type: ignore[attr-defined]
        console.print("  schemabrain joins suggest --apply")  # type: ignore[attr-defined]
        console.print()  # type: ignore[attr-defined]
        return
    # Generic recovery: any other skipped/failed status.
    console.print("Stage 5 did not curate joins (see above). Retry when ready:")  # type: ignore[attr-defined]
    console.print("  schemabrain joins suggest --apply")  # type: ignore[attr-defined]
    console.print()  # type: ignore[attr-defined]


def _stage_display_name(name: str) -> str:
    """Map the wizard's stable stage names to friendlier display strings."""
    return {
        "source_check": "Source check",
        "index": "Index schema",
        "entities": "Curate entities",
        "metrics": "Curate metrics",
        "joins": "Curate joins",
        "wire_host": "Wire host",
        "next_step": "Next",
    }.get(name, name)


def _render_wire_host_detail(host_result: object, console: object) -> None:
    """Render the post-stage-4 context lines for the host install.

    Shows the config path + backup for `written`, nothing for
    `unchanged`, the redacted argv for `shell_out_failed`, and the
    paste-ready JSON snippet to stdout for `printed_only`.
    """
    from schemabrain.setup.init_flow import InitResult

    if not isinstance(host_result, InitResult):
        return  # pragma: no cover — defensive; stage-4 always populates this on `done`

    if host_result.state == "written" and host_result.config_path is not None:
        wrote = _format_path_for_terminal(host_result.config_path)
        console.print(f"        [dim]wrote:[/] {wrote}")  # type: ignore[attr-defined]
        if host_result.backup_made:
            backup_path = host_result.config_path.parent / (host_result.config_path.name + ".bak")
            backup = _format_path_for_terminal(backup_path)
            console.print(f"        [dim]backup:[/] {backup}")  # type: ignore[attr-defined]
    elif host_result.state == "shell_out_failed":
        if host_result.shell_out_command:
            console.print()  # type: ignore[attr-defined]
            console.print(  # type: ignore[attr-defined]
                "        " + " ".join(_redact_env_args(host_result.shell_out_command))
            )
            console.print(  # type: ignore[attr-defined]
                "        [dim]env values redacted above; re-run `schemabrain init` "
                "to register with real credentials.[/]"
            )
        if host_result.shell_out_stderr:
            safe_stderr = _redact_stderr_credentials(host_result.shell_out_stderr)
            console.print(  # type: ignore[attr-defined]
                f"        [dim]stderr:[/] {safe_stderr}"
            )
    elif host_result.state == "printed_only":
        import json as _json

        console.print()  # type: ignore[attr-defined]
        console.print("        Add this to your MCP host's config:")  # type: ignore[attr-defined]
        console.print()  # type: ignore[attr-defined]
        console.file.flush()  # type: ignore[attr-defined]
        entry = {"mcpServers": {"schemabrain": host_result.snippet.to_mcp_entry()}}
        sys.stdout.write(_json.dumps(entry, indent=2))
        sys.stdout.write("\n")
        sys.stdout.flush()
        console.print()  # type: ignore[attr-defined]
        console.print("        [dim]Common config paths:[/]", soft_wrap=True)  # type: ignore[attr-defined]
        for line in (
            "          Claude Desktop (macOS):   ~/Library/Application Support/Claude/claude_desktop_config.json",
            "          Claude Desktop (Windows): %APPDATA%\\Claude\\claude_desktop_config.json",
            "          Cursor:                   ~/.cursor/mcp.json",
            "          Continue:                 ~/.continue/config.json",
            "          Windsurf:                 ~/.codeium/windsurf/mcp_config.json",
        ):
            console.print(line, soft_wrap=True, highlight=False)  # type: ignore[attr-defined]


def _cmd_check(
    *,
    positional_url: str | None,
    url_env: str | None,
    store_path: str,
    json_mode: bool,
) -> int:
    """Run `schemabrain check` and render the drift report.

    Reads entities, metrics, and canonical joins from the local store,
    confirms each against the live source schema, and emits a report.

    Exit codes:
      - 0: no drift detected
      - 1: at least one drift surfaced
      - 2: operational refusal before the check could run (e.g.
        --source + --url-env conflict, --url-env names an unset
        variable, the source URL cannot be reached, the store file
        does not exist or carries a schema-version mismatch)

    JSON mode writes a parseable report to stdout and suppresses the
    rich-rendered output. Useful for CI pipelines that gate on
    drift-free state via `jq '.exit_code'`.
    """
    from schemabrain.check.engine import check_drift
    from schemabrain.check.render import render_json, render_report
    from schemabrain.core.store import SchemaVersionMismatchError

    url = _resolve_url_source(
        positional=positional_url,
        url_env=url_env,
        allow_interactive=True,
        interactive_purpose="to check for schema drift",
    )
    if url is None:
        # _resolve_url_source already rendered a guided error.
        return 2
    canonical = _resolve_url(url)
    if canonical is None:
        # Malformed URL — `_resolve_url` rendered a `url_invalid` /
        # `url_wrong_driver` guided error. Without this branch, a
        # bare-string mistake (e.g. `--source DATABASE_URL` instead of
        # `--url-env DATABASE_URL`) crashes with an unhandled
        # `ValueError` from `_canonical_url`.
        return 2
    source_id = _make_source_id(url)
    store_p = Path(store_path)
    if not store_p.exists():
        _render_guided(
            GuidedError(
                kind="check_store_missing",
                message=f"store not found at {store_path}",
                why="`schemabrain check` reads persisted definitions from the local "
                "SQLite store; without a store there is nothing to compare against the source",
                fix=f"run `schemabrain index --url-env DBURL --store-path {store_path}` first",
                next_step="re-run `schemabrain check` after `index` completes",
            )
        )
        return 2

    try:
        with (
            PostgresDataSource(url) as source,
            SQLiteStore(store_p) as store,
        ):
            report = check_drift(
                store=store,
                source=source,
                source_connection_id=source_id,
            )
    except SchemaVersionMismatchError as exc:
        _render_guided(
            GuidedError(
                kind="check_schema_version_mismatch",
                message=str(exc),
                why="the local store was written by a different schemabrain version",
                fix="delete the store file and re-run `schemabrain index`, "
                "or downgrade to a matching schemabrain version",
                next_step=f"rm {store_path} && schemabrain index ...",
            )
        )
        return 2
    except OperationalError as e:
        _render_guided(postgres_operational_error(e, url_hint=canonical))
        return 2

    if json_mode:
        sys.stdout.write(render_json(report))
    else:
        render_report(report, console=_stderr_console(), source_label=canonical)
    return report.exit_code


def _cmd_inspect(
    *,
    name: str | None,
    positional_url: str | None,
    url_env: str | None,
    store_path: str,
) -> int:
    """Run `schemabrain inspect` and render either the summary or an
    entity drill view.

    No live source connection is needed — the inspect surface is
    purely a store reader. The `--source` / `--url-env` flag is
    therefore OPTIONAL and only used to scope reads to one source
    when the store carries entities from multiple sources.

    When drilling (`name` is supplied) and no source is given, the
    handler walks every source the store knows about and renders
    every match for `name` in succession. A drilled name that
    resolves to zero entities exits 1 with a guided error.

    Exit codes:
      - 0: rendered successfully (empty store and missing-name-with-
        zero-matches at the cross-source level both exit 0/1
        respectively)
      - 1: drilled name not found in any source
      - 2: operational refusal (--source + --url-env conflict, bad
        store, malformed URL)
    """
    from schemabrain.core.store import SchemaVersionMismatchError
    from schemabrain.inspect import (
        build_entity_detail,
        build_join_detail,
        build_metric_detail,
        build_summary,
        render_entity_detail,
        render_join_detail,
        render_metric_detail,
        render_summary,
    )

    source_id: str | None = None
    if positional_url is not None or url_env is not None:
        source_url = _resolve_url_source(positional=positional_url, url_env=url_env)
        if source_url is None:
            return 2
        # Validate URL before handing to `_make_source_id` (which calls
        # `_canonical_url` internally and would raise ValueError on a
        # malformed URL — e.g. `--source DATABASE_URL` instead of
        # `--url-env DATABASE_URL`). `_resolve_url` renders the
        # canonical `url_invalid` guided error and returns None.
        if _resolve_url(source_url) is None:
            return 2
        source_id = _make_source_id(source_url)

    store_p = Path(store_path)
    if not store_p.exists():
        _render_guided(
            GuidedError(
                kind="inspect_store_missing",
                message=f"store not found at {store_path}",
                why="`schemabrain inspect` reads from the local SQLite "
                "store; without a store there is nothing to inspect",
                fix=f"run `schemabrain index --url-env DBURL --store-path "
                f"{store_path}` to populate it",
                next_step="re-run `schemabrain inspect` after `index` completes",
            )
        )
        return 2

    console = _stderr_console()
    try:
        with SQLiteStore(store_p) as store:
            if name is None:
                summary = build_summary(store=store, source_connection_id=source_id)
                render_summary(summary, console=console, store_path=store_path)
                return 0

            # Drill mode resolves `name` as entity → metric → join in
            # that priority. The summary view lists all three kinds
            # alongside each other with no namespace, so callers like
            # the summary's "Drill into one: `schemabrain inspect <name>`"
            # link must work for any of them.
            if source_id is not None:
                candidate_sources = [source_id]
            else:
                # Union the source-id sets across the three name spaces
                # — a name that lives only as a metric (or join) still
                # gets a non-empty candidate set so the drill below can
                # find it. Sorted for determinism in the rendered output.
                source_ids = sorted(
                    set(_list_source_ids_with_entity(store, name))
                    | set(_list_source_ids_with_metric(store, name))
                    | set(_list_source_ids_with_join(store, name))
                )
                if not source_ids:
                    print(
                        f"error: no entity, metric, or join named {name!r} in {store_path!r}",
                        file=sys.stderr,
                    )
                    return 1
                candidate_sources = source_ids

            rendered = False
            for sid in candidate_sources:
                # Priority: entity → metric → join. A name that exists
                # as both an entity and a metric (rare; the identifiers
                # share the same alphabet but the operator typically
                # avoids the collision) drills as the entity, and the
                # operator can disambiguate by passing the metric/join
                # name verbatim when it differs.
                entity_detail = build_entity_detail(
                    store=store,
                    entity_name=name,
                    source_connection_id=sid,
                )
                if entity_detail is not None:
                    if rendered:
                        console.print()
                    render_entity_detail(entity_detail, console=console)
                    rendered = True
                    continue
                metric_detail = build_metric_detail(
                    store=store,
                    metric_name=name,
                    source_connection_id=sid,
                )
                if metric_detail is not None:
                    if rendered:  # pragma: no cover — multi-source separator; same name resolving in >1 source-id is rare
                        console.print()
                    render_metric_detail(metric_detail, console=console)
                    rendered = True
                    continue
                join_detail = build_join_detail(
                    store=store,
                    join_name=name,
                    source_connection_id=sid,
                )
                if join_detail is not None:
                    if rendered:  # pragma: no cover — multi-source separator; same name resolving in >1 source-id is rare
                        console.print()
                    render_join_detail(join_detail, console=console)
                    rendered = True

            if not rendered:
                print(
                    f"error: no entity, metric, or join named {name!r} in {store_path!r}",
                    file=sys.stderr,
                )
                return 1
            return 0

    except SchemaVersionMismatchError as exc:
        _render_guided(
            GuidedError(
                kind="inspect_schema_version_mismatch",
                message=str(exc),
                why="the local store was written by a different schemabrain version",
                fix="delete the store file and re-run `schemabrain index`, "
                "or downgrade to a matching schemabrain version",
                next_step=f"rm {store_path} && schemabrain index ...",
            )
        )
        return 2
    except sqlite3.OperationalError as exc:
        # Partial-migration shape (missing `column_pii_tags`, missing
        # `entities`) — the schema-version check passed but the SQL
        # surface is inconsistent. Surface as exit 2 with the SQLite
        # error name so the operator can grep for the missing table.
        _render_guided(
            GuidedError(
                kind="inspect_store_inconsistent",
                message=f"sqlite3.OperationalError: {exc}",
                why="the local store passed the schema-version check but "
                "a required table or column is missing — likely a "
                "partial migration or hand-edited store",
                fix="delete the store file and re-run `schemabrain index` to rebuild from scratch",
                next_step=f"rm {store_path} && schemabrain index ...",
            )
        )
        return 2


def _list_source_ids_with_entity(store: SQLiteStore, entity_name: str) -> list[str]:
    """Walk every source-id known to the store and return those that
    carry an entity named `entity_name`. Cross-source drill helper.

    `Store.list_entities` doesn't expose the per-row `source_connection_id`,
    so we read it via the underlying SQLite connection. A partial-
    migration store missing the `entities` table is the only realistic
    failure mode at v12 — we swallow `sqlite3.OperationalError` and
    return `[]` so the caller renders the documented "no entity named
    X" message instead of an uncaught traceback.
    """
    # `SQLiteStore` exposes its connection via `_require_conn` (used
    # by `get_column_pii_tags`). Inspect is read-only here so direct
    # SELECT is appropriate; mirrors the audit-CLI's raw-SQL reader
    # pattern.
    conn = store._require_conn()
    try:
        rows = conn.execute(
            "SELECT DISTINCT source_connection_id FROM entities WHERE name = ?",
            (entity_name,),
        ).fetchall()
    except sqlite3.OperationalError:  # pragma: no cover — pre-v10 partial-migration defense
        return []
    return [r[0] for r in rows]


def _list_source_ids_with_metric(store: SQLiteStore, metric_name: str) -> list[str]:
    """Cross-source variant of `_list_source_ids_with_entity` for metrics.

    Same partial-migration tolerance: a store missing the `metrics`
    table (pre-v10) returns an empty list rather than crashing, so
    the caller's "name not found" path still renders cleanly.
    """
    conn = store._require_conn()
    try:
        rows = conn.execute(
            "SELECT DISTINCT source_connection_id FROM metrics WHERE name = ?",
            (metric_name,),
        ).fetchall()
    except sqlite3.OperationalError:  # pragma: no cover — pre-v10 partial-migration defense
        return []
    return [r[0] for r in rows]


def _list_source_ids_with_join(store: SQLiteStore, join_name: str) -> list[str]:
    """Cross-source variant of `_list_source_ids_with_entity` for joins."""
    conn = store._require_conn()
    try:
        rows = conn.execute(
            "SELECT DISTINCT source_connection_id FROM canonical_joins WHERE name = ?",
            (join_name,),
        ).fetchall()
    except sqlite3.OperationalError:  # pragma: no cover — pre-v10 partial-migration defense
        return []
    return [r[0] for r in rows]


def _cmd_dashboard(*, store_path: str, port: int, open_browser: bool) -> int:
    """Boot the read-only dashboard sidecar against a previously-indexed store.

    Thin wrapper over `schemabrain.dashboard.cli.run_dashboard`. Defers the
    actual import so a base wheel (without the `[ui]` extra) doesn't pay
    an ImportError just to load the CLI parser.

    Source-id selection is automatic: the sidecar resolves the canonical
    source_id from the store via `/api/meta`. Multi-source stores surface
    the first known source_id in the response; an explicit source-selection
    flag is deliberately not exposed at v1 — operators that need it can
    re-run with a per-source store.

    Exit codes (delegated to `run_dashboard`):
      - 0: served until interrupted with Ctrl+C
      - 1: invalid store (missing path, wrong schema, malformed)
      - 2: `[ui]` extra not installed (uvicorn / fastapi missing)
    """
    from pathlib import Path as _Path

    from schemabrain.dashboard.cli import run_dashboard

    return run_dashboard(
        store_path=_Path(store_path),
        port=port,
        open_browser=open_browser,
    )


def _cmd_demo(
    *,
    action: str | None,
    store_path: str | None,
    host: str | None,
    port: int,
    open_browser: bool,
) -> int:
    """Zero-setup showcase. Builds the offline SaaS store (12 entities /
    11 joins / 5 metrics + seeded audit chain — no Docker, no API key,
    no Postgres), then either runs the requested action directly or, when
    interactive and no action flag was passed, offers a guided menu.

    Exit codes: 0 success / served-until-Ctrl+C; 2 when an action's
    prerequisite is missing (e.g. Docker for --wire, the `[ui]` extra for
    the dashboard).
    """
    from pathlib import Path as _Path

    from schemabrain.setup import demo as _demo

    console = _stderr_console()
    path = _Path(store_path) if store_path else _demo.DEMO_STORE_PATH
    console.print(
        "[bright_black]◇ building the demo store "
        "(12 entities · 11 joins · 5 metrics · audit chain)…[/]"
    )
    source_id = _demo.seed_demo_store(path)
    console.print(f"[green]✓[/] demo store ready [bright_black]· {path}[/]")

    if action == "dashboard":
        return _demo.open_dashboard(
            store_path=path,
            source_id=source_id,
            port=port,
            open_browser=open_browser,
            console=console,
        )
    if action == "showcase":
        return _demo.run_showcase(store_path=path, source_id=source_id, console=console)
    if action == "wire":
        return _demo.wire_demo_host(
            store_path=path, source_id=source_id, host=host, console=console
        )

    if _stderr_is_interactive_tty():
        return _demo.run_demo_menu(
            store_path=path,
            source_id=source_id,
            console=console,
            port=port,
            open_browser=open_browser,
        )
    _demo.print_next_steps(path, console)
    return 0


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


def _try_render_llm_failure(
    exc: BaseException,
    *,
    retry_command: str,
    fallback_command: str | None,
) -> bool:
    """Render the Shape C LLM-failure advisory if `exc` is a known Anthropic error.

    Returns True on a successful render (caller should NOT re-raise),
    False when `exc` is not an Anthropic SDK error the renderer knows
    about (caller should propagate so a less-specific handler or the
    top-level traceback sees it). Centralizes the classify + render
    pair so every CLI callsite shares one boundary.
    """
    kind = classify_llm_failure(exc)
    if kind is None:
        return False
    # The `getattr(exc, "message", ...)` extraction lives in
    # `cause_from_llm_error` (`errors_render.py`) so the untyped
    # access lives next to `classify_llm_failure` — single owner,
    # single fallback chain. Long messages get visually truncated
    # by Rich on the ✗-glyph line.
    render_llm_failure(
        kind=kind,
        retry_command=retry_command,
        fallback_command=fallback_command,
        cause=cause_from_llm_error(exc),
        console=_stderr_console(),
    )
    return True


def _resolve_url_source(
    *,
    positional: str | None,
    url_env: str | None,
    allow_interactive: bool = False,
    interactive_purpose: str = "to connect to your database",
) -> str | None:
    """Resolve a connection URL from either a positional arg or a named env var.

    Returns the URL string (with bare `postgresql://` / `postgres://`
    silently rewritten to `postgresql+psycopg://` for free — see the
    `_apply_silent_rewrite` helper below) on success, or `None` after
    rendering a guided error to stderr. Emits a single-line
    deprecation warning to stderr when
    `positional` is used AND contains an embedded password, nudging the
    user toward `--url-env`. Env-var resolution is always silent — env
    is the safe path we're nudging users toward.

    Rules:
      - exactly one of {positional, url_env} must be provided
      - `url_env` names an env var; the var must exist and be non-empty
      - the warning does NOT echo the password back at the user (which
        would defeat the point — the warning would itself become a leak
        on a shared terminal or screen-recording)

    Applies ``silent_rewrite_to_psycopg`` to every returned URL,
    BEFORE the caller sees it. The rewrite lives at this boundary
    (not inside ``_resolve_url``) because 14 of 17 callsites discard
    `_resolve_url`'s return value (`if _resolve_url(url) is None:
    return 2`) and continue using the raw URL — so bare
    `postgresql://` URLs would silently slip past validation and
    reach ``PostgresDataSource`` with the raw scheme, triggering
    ``ModuleNotFoundError: psycopg2``. Applying the rewrite at the
    source-resolution boundary fixes it for all 17 callsites without
    requiring each to be touched.

    Interactive escape hatch (``allow_interactive=True``): when neither
    `positional` nor `url_env` is provided AND stderr is a TTY, prompt
    the user for a URL via ``prompt_for_url`` instead of rendering the
    "no URL provided" guided error. The prompt uses ``password=True`` so
    URLs with embedded credentials don't echo to scrollback. If the user
    presses Enter without typing, falls through to the guided error
    (preserves the existing exit-2 behavior for non-interactive paths
    AND for interactive users who decline to provide a URL). Default
    `False` preserves the strict env-var-or-die behavior for every
    callsite that hasn't opted in.

    ``interactive_purpose`` is the verb-phrase shown in the prompt's
    preamble ("to index your database", "to check for drift"). Only
    used when the prompt actually fires.
    """

    def _apply_silent_rewrite(url: str) -> str:
        """Apply the bare-scheme rewrite at every exit point.

        Centralised so a future contributor adding a new return path
        cannot accidentally skip the rewrite. Returns the URL
        unchanged when the scheme isn't eligible.

        Tolerates malformed URLs (e.g. unclosed IPv6 brackets that
        make ``urlparse`` raise ``ValueError``) by returning the
        input as-is — the downstream ``_resolve_url`` will render the
        real diagnostic. Without the try/except, the
        `test_malformed_positional_url_falls_through_without_warning`
        contract regressed.
        """
        try:
            scheme = urlparse(url).scheme
        except ValueError:
            return url
        rewritten = silent_rewrite_to_psycopg(scheme, url)
        return rewritten if rewritten is not None else url

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
        # Interactive escape hatch: when the caller opted in AND the
        # user is sitting at a TTY, ask for the URL directly instead
        # of dying with a "no URL provided" guided error. The day-one
        # UX overhaul wires this through `init`, `index`, `check`, and
        # the three `*/suggest` commands so a first-time user no
        # longer has to know about `--url-env` or the +psycopg
        # driver suffix.
        if allow_interactive and _stderr_is_interactive_tty():
            from schemabrain._ui import prompt_for_url

            entered = prompt_for_url(_stderr_console(), purpose=interactive_purpose)
            if entered is not None:
                return _apply_silent_rewrite(entered)
            # User pressed Enter without typing — they explicitly
            # declined to provide a URL. Fall through to the guided
            # error so they see the recovery hint, exit 2, and can
            # re-run with the URL on the command line if they prefer.
        # If the user already has a URL in $DATABASE_URL (the most common
        # convention), surface the exact recipe they probably wanted —
        # `--url-env DATABASE_URL`. Without this hint, a first-time user
        # who has DATABASE_URL set still has to guess which flag form to
        # use. Note: we only mention the env var if it appears to be a
        # real-looking URL (has a scheme + colon), not just non-empty,
        # so a misnamed but populated env var doesn't trigger a
        # misleading suggestion.
        env_db = os.environ.get("DATABASE_URL", "")
        has_likely_url_in_env = bool(env_db) and "://" in env_db
        if has_likely_url_in_env:
            fix = (
                "your $DATABASE_URL looks like a URL — try `--url-env DATABASE_URL`. "
                "Otherwise: re-run with --url-env VARNAME (where VARNAME holds the URL), "
                "OR pass the URL positionally (less safe — leaks creds to argv)"
            )
        else:
            fix = (
                "re-run with --url-env VARNAME (where VARNAME holds the URL), "
                "OR pass the URL positionally (less safe — leaks creds to argv)"
            )
        _render_guided(
            GuidedError(
                kind="url_source_missing",
                message="no connection URL provided",
                why="schemabrain needs a Postgres URL to reach your source database",
                fix=fix,
                next_step="see docs/setup.md for the canonical URL format",
            )
        )
        return None
    if url_env is not None:
        # Design shape B (handoff bundle ``cli/errors.jsx:ErrMissingSecret``):
        # the unset and empty paths share the same three-panel
        # surface — only the title wording flips between
        # ``<VAR> not set`` and ``<VAR> is empty``. The renderer
        # owns that flip via the ``state`` discriminator so this
        # callsite stays the obvious place to read the error
        # condition without re-rendering chrome by hand.
        from schemabrain.errors_render import render_missing_secret_error

        value = os.environ.get(url_env)
        if value is None:
            render_missing_secret_error(env_var=url_env, state="unset", console=_stderr_console())
            return None
        if value == "":
            render_missing_secret_error(env_var=url_env, state="empty", console=_stderr_console())
            return None
        return _apply_silent_rewrite(value)
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
    return _apply_silent_rewrite(positional)


# Module-scoped dedup set for the whitespace-key stderr warning.
# Keeps the warning to once per process rather than once per call
# (which would flood the wizard's 2 LLM stages with the same line).
# Tests reset via the public helper below. Defined here, immediately
# before its sole consumer `_resolve_anthropic_key_source`, so the
# function body's first reference to this set resolves visually
# within scrolling distance.
_WARNED_EMPTY_KEY_ENV_VARS: set[str] = set()


def _reset_warned_empty_key_cache_for_tests() -> None:
    """Test-only seam — wipes the once-per-process warned-empty-key set
    so tests can re-trigger the warning in isolation without bleed-over.
    Mirrors the `_reset_warned_empty_cache_for_tests` pattern in `_env.py`.
    """
    _WARNED_EMPTY_KEY_ENV_VARS.clear()


def _resolve_anthropic_key_source(
    *,
    allow_interactive: bool = False,
    interactive_purpose: str = "use Claude",
    interactive_cost_estimate_usd: float = 0.04,
    interactive_cap_usd: float = 0.50,
    interactive_skip_hint: str = "press Enter to skip",
) -> str | None:
    """Resolve ``ANTHROPIC_API_KEY`` from env, optionally prompting on miss.

    Returns the stripped key string when env var is set and non-empty,
    or ``None`` when missing — the caller routes ``None`` based on its
    own degrade-vs-abort policy (entities suggest aborts; the wizard's
    LLM stages skip with a hint).

    When ``allow_interactive=True`` AND stderr is a TTY AND the env
    var is missing or empty, prompts the user via
    ``prompt_for_anthropic_key`` instead of returning ``None`` early.
    The prompt renders a cost / cap / skip-hint disclosure before
    asking for the key so the user understands the bounds before
    pasting. If the prompt returns empty (user pressed Enter to skip),
    falls through and returns ``None``.

    Default ``allow_interactive=False`` preserves the existing
    env-var-or-render-guided-error behavior; callers that opt in
    accept that an interactive run may block on stdin.

    Unlike ``_resolve_url_source`` this helper does NOT render a
    GuidedError on miss — the four existing callsites (index --enrich,
    entities suggest, metrics suggest, wizard stages 3+4) each render
    command-specific guided errors with different `fix` wording. The
    helper returns ``None`` and lets the caller render whichever
    GuidedError it already had.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key is not None and api_key.strip():
        return api_key.strip()
    # When the env var is set but whitespace-only, emit a one-line
    # stderr warning so the operator knows their export is
    # malformed, not just absent. Without this, an
    # `export ANTHROPIC_API_KEY=" "` from a secret manager with a
    # trailing newline produces the same "missing" error path as a
    # genuinely unset var — wasting debug time on the wrong problem.
    # The warning is one-shot per process via the presence check at
    # module scope (would dedupe across multiple resolver calls
    # within the same `_cmd_init` if it grew them).
    if (
        api_key is not None
        and not api_key.strip()
        and "ANTHROPIC_API_KEY" not in _WARNED_EMPTY_KEY_ENV_VARS
    ):
        _WARNED_EMPTY_KEY_ENV_VARS.add("ANTHROPIC_API_KEY")
        print(
            "warning: ANTHROPIC_API_KEY is set but whitespace-only; "
            "treating as unset. Check the export — a trailing newline "
            "or space disqualifies the value.",
            file=sys.stderr,
        )
    if allow_interactive and _stderr_is_interactive_tty():
        from schemabrain._ui import (
            offer_persist_anthropic_key_to_env_file,
            prompt_for_anthropic_key,
        )

        console = _stderr_console()
        prompted_key = prompt_for_anthropic_key(
            console,
            purpose=interactive_purpose,
            cost_estimate_usd=interactive_cost_estimate_usd,
            cap_usd=interactive_cap_usd,
            skip_hint=interactive_skip_hint,
        )
        if prompted_key is not None:
            # D4: offer to persist the freshly-pasted key so the
            # operator doesn't have to paste it again. Opt-in
            # default no. Failures inside the persistence flow
            # MUST NOT block the resolver — the operator's key is
            # in hand; .env is a convenience, not a precondition.
            cwd = Path.cwd()
            try:
                offer_persist_anthropic_key_to_env_file(
                    console,
                    key_value=prompted_key,
                    env_path=cwd / ".env",
                    gitignore_path=cwd / ".gitignore",
                )
            except OSError as exc:
                # Disk-write failure (read-only FS, permission denied,
                # quota). Surface a one-line warning so the operator
                # knows the save didn't happen, but don't error out —
                # they still have the key for this run.
                print(
                    f"warning: could not persist ANTHROPIC_API_KEY to .env: {exc}",
                    file=sys.stderr,
                )
        return prompted_key
    return None


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
    # Silent rewrite for the bare `postgresql://` / `postgres://` schemes
    # that every Postgres tool accepts but SQLAlchemy rejects without
    # `+psycopg`. Forcing a first-time user to learn the driver suffix
    # is pure friction with no security or correctness payoff. Explicit
    # wrong drivers (`postgresql+psycopg2`, `postgresql+asyncpg`) are
    # NOT rewritten — they fall through to `url_wrong_driver` so the
    # user learns their explicit choice can't be honored, rather than
    # being silently overridden.
    rewritten = silent_rewrite_to_psycopg(parsed.scheme, url)
    if rewritten is not None:
        url = rewritten
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
                why="SchemaBrain needs a Postgres URL to connect to your source database",
                fix="use the form postgresql+psycopg://USER:PASSWORD@HOST:5432/DBNAME",
                next_step="see docs/setup.md for the canonical URL format",
            )
        )
        return None


def _canonical_url(url: str) -> str:
    """Return the credential-free, port-normalized form of a connection URL.

    Delegates to :func:`schemabrain.core.source_id.canonical_url` so the
    dashboard sidecar can produce the same canonical form without
    depending on this CLI module.
    """
    from schemabrain.core.source_id import canonical_url

    return canonical_url(url)


def _make_source_id(url: str) -> str:
    """Stable short identifier for the source DB, derived from its URL.

    Delegates to :func:`schemabrain.core.source_id.make_source_id`.
    """
    from schemabrain.core.source_id import make_source_id

    return make_source_id(url)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
