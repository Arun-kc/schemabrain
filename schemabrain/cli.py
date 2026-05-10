"""Schema Brain CLI.

Entry point: `schemabrain <subcommand>`.

`index` connects to a Postgres URL, introspects every user-visible
schema and table, profiles columns whose schema changed since the last
run, generates LLM descriptions for changed columns (unless
`--no-enrich`), and writes structural metadata, fingerprints, and
descriptions to a local SQLite store.

Re-running `index` against an unchanged source is a no-op: the
fingerprint cache lets us skip introspection writes, profiler queries,
AND LLM calls.

Cost discipline: `--max-cost N` (default $10) hard-caps LLM spend per
run. Exceeding the cap aborts cleanly before the next call.
ANTHROPIC_API_KEY must be set in the environment unless `--no-enrich`
is passed.

`eval` scores a `Retriever` against a hand-curated `GoldenSet` and
prints recall@1/@3/@10. Today the only retriever is `KeywordRetriever`
(a placeholder until embedding-based retrieval ships). The harness is
schema-agnostic: pass `--golden /path/to/your-schema.json` for a real
schema. The bundled default is just one starter example
(`schemabrain/eval/golden_sets/ecommerce.json`, paired with the
synthetic fixture in `schemabrain/eval/fixtures/ecommerce.sql`) so the
CLI works out of the box.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from urllib.parse import urlparse

from schemabrain.connectors.postgres import PostgresDataSource
from schemabrain.core.store import SQLiteStore
from schemabrain.enrichment.anthropic_client import (
    anthropic_haiku_45_client,
    anthropic_sonnet_46_client,
)
from schemabrain.enrichment.pipeline import CostCapExceeded, EnrichmentPipeline
from schemabrain.eval.golden import DEFAULT_GOLDEN_PATH, load_golden
from schemabrain.eval.retriever import KeywordRetriever
from schemabrain.eval.runner import format_report, run_eval
from schemabrain.indexer import index
from schemabrain.profiler.postgres import PostgresProfiler

_DEFAULT_STORE_PATH = "./schemabrain.db"
_DEFAULT_MAX_COST_USD = 10.0
_DEFAULT_EVAL_LIMIT = 10

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


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "index":
        return _cmd_index(
            args.url,
            args.store_path,
            no_enrich=args.no_enrich,
            max_cost_usd=args.max_cost,
            enable_sonnet=args.enable_sonnet,
        )
    if args.command == "eval":
        return _cmd_eval(
            golden_path=args.golden,
            store_path=args.store_path,
            source_url=args.source,
            limit=args.limit,
        )
    # argparse `required=True` on subparsers prevents reaching here, but
    # leaving an explicit branch is cheaper than a guarded assertion.
    parser.error(f"unknown command: {args.command}")  # pragma: no cover


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="schemabrain",
        description="MCP-ready semantic understanding of any production database.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_index = sub.add_parser("index", help="Index a database into the local SQLite store")
    p_index.add_argument(
        "url",
        help="Source database connection URL (e.g. postgresql://user:pass@host:5432/db)",
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
        "Aborts cleanly when reached; no effect with --no-enrich.",
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

    p_eval = sub.add_parser(
        "eval",
        help="Score a Retriever against the bundled golden set; print recall@1/@3/@10",
    )
    p_eval.add_argument(
        "--source",
        required=True,
        help="The same source URL passed to `index` — used to resolve which "
        "tables in the local store to score against.",
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
        "--limit",
        type=int,
        default=_DEFAULT_EVAL_LIMIT,
        help=f"Top-K cap passed to the retriever (default: {_DEFAULT_EVAL_LIMIT})",
    )
    return parser


def _cmd_index(
    url: str,
    store_path: str,
    *,
    no_enrich: bool,
    max_cost_usd: float,
    enable_sonnet: bool,
) -> int:
    try:
        canonical = _canonical_url(url)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    pipeline: EnrichmentPipeline | None = None
    if not no_enrich:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            print(
                "error: ANTHROPIC_API_KEY not set. Either set it in the "
                "environment or re-run with --no-enrich.",
                file=sys.stderr,
            )
            return 2
        cryptic_client = anthropic_sonnet_46_client(api_key=api_key) if enable_sonnet else None
        pipeline = EnrichmentPipeline(
            client=anthropic_haiku_45_client(api_key=api_key),
            cryptic_client=cryptic_client,
            max_cost_usd=max_cost_usd,
        )

    source_id = _make_source_id(url)
    started = time.monotonic()
    try:
        with (
            PostgresDataSource(url) as source,
            PostgresProfiler(url) as profiler,
            SQLiteStore(store_path) as store,
        ):
            result = index(
                source=source,
                profiler=profiler,
                store=store,
                source_connection_id=source_id,
                pipeline=pipeline,
            )
    except CostCapExceeded as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
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


def _cmd_eval(
    *,
    golden_path: str,
    store_path: str,
    source_url: str,
    limit: int,
) -> int:
    try:
        source_id = _make_source_id(source_url)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    try:
        golden = load_golden(golden_path)
    except FileNotFoundError:
        print(f"error: golden file not found: {golden_path}", file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"error: invalid golden file: {e}", file=sys.stderr)
        return 2

    with SQLiteStore(store_path) as store:
        retriever = KeywordRetriever(store=store, source_connection_id=source_id)
        report = run_eval(golden=golden, retriever=retriever, limit=limit)

    print(format_report(report))
    return 0


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
