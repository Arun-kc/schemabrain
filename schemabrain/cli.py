"""Schema Brain CLI.

Entry point: `schemabrain <subcommand>`.

Week 1 ships only `index`, which connects to a Postgres URL, walks every
user-visible schema and table, and writes the introspected models to a
local SQLite store.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
from urllib.parse import urlparse

from schemabrain.connectors.postgres import PostgresDataSource
from schemabrain.core.store import SQLiteStore

_DEFAULT_STORE_PATH = "./schemabrain.db"

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
    return _cmd_index(args.url, args.store_path)


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
    return parser


def _cmd_index(url: str, store_path: str) -> int:
    try:
        canonical = _canonical_url(url)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    source_id = _make_source_id(url)
    started = time.monotonic()
    count = 0
    with PostgresDataSource(url) as source, SQLiteStore(store_path) as store:
        for schema_name, table_name in source.list_tables():
            table = source.get_table(table_name, schema=schema_name)
            store.write_table(table, source_connection_id=source_id)
            count += 1
    elapsed = time.monotonic() - started
    print(
        f"Indexed {count} table(s) from {canonical} into {store_path} in {elapsed:.1f}s",
        file=sys.stderr,
    )
    if count == 0:
        print(
            "warning: no tables indexed (empty database, or all tables are in "
            "system schemas that were skipped)",
            file=sys.stderr,
        )
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
