"""Schema Brain MCP server.

Exposes the local Schema Brain store to AI agents over the Model Context
Protocol. Two tools ship in v0:

  - `find_relevant_tables(query, limit=10)` — embedding-cosine retrieval
    that returns the most relevant tables for a natural-language question,
    each annotated with the matched column and a token estimate.
  - `describe_table(qualified_name)` — full structural + semantic
    description of one table, including columns, types, descriptions,
    and foreign keys.

The server runs stdio transport for Claude Desktop integration. HTTP /
SSE transports are exposed by the underlying `mcp` SDK and could be
wired in later if needed.

Public API:
  - `build_server(store, source_connection_id, embedder)` — returns a
    configured `FastMCP` app ready to `.run("stdio")`.
  - `TableHit`, `TableDescription`, `ColumnInfo`, `ForeignKeyInfo` —
    typed return shapes the tools produce.
  - `TableNotFoundError` — raised by `describe_table` when the
    qualified_name doesn't exist in the store for the given source.
"""

from schemabrain.mcp.server import build_server
from schemabrain.mcp.tools import (
    ColumnInfo,
    ForeignKeyInfo,
    TableDescription,
    TableHit,
    TableNotFoundError,
    describe_table_impl,
    find_relevant_tables_impl,
)

__all__ = [
    "ColumnInfo",
    "ForeignKeyInfo",
    "TableDescription",
    "TableHit",
    "TableNotFoundError",
    "build_server",
    "describe_table_impl",
    "find_relevant_tables_impl",
]
