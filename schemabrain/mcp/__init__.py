"""SchemaBrain MCP server.

Exposes the local SchemaBrain store to AI agents over the Model Context
Protocol. Four tools ship in v0:

  - `find_relevant_tables(query, limit=10)` — embedding-cosine retrieval
    that returns the most relevant tables for a natural-language question,
    each annotated with the matched column and a token estimate.
  - `describe_table(qualified_name)` — full structural + semantic
    description of one table, including columns, types, descriptions,
    and foreign keys.
  - `describe_column(qualified_name)` — drill-down on one column,
    including outgoing FKs (this column joins to where) AND incoming
    FKs (which other tables reference this column).
  - `suggest_joins(tables, max_hops=4)` — shortest FK-graph paths
    between every pair of input tables, including reverse traversals
    and multi-hop paths via intermediate tables.

The server runs stdio transport for Claude Desktop integration. HTTP /
SSE transports are exposed by the underlying `mcp` SDK and could be
wired in later if needed.

Public API:
  - `build_server(store, source_connection_id, embedder)` — returns a
    configured `FastMCP` app ready to `.run("stdio")`.
  - `TableHit`, `TableDescription`, `ColumnDetail`, `ColumnInfo`,
    `ForeignKeyInfo`, `IncomingForeignKeyInfo`, `JoinEdge`, `JoinPath`,
    `SuggestJoinsResult` — typed return shapes the tools produce.
  - `TableNotFoundError`, `ColumnNotFoundError` — raised by the
    `describe_*` tools when a name doesn't exist in the store.
"""

from schemabrain.mcp.describe_column import describe_column_impl
from schemabrain.mcp.describe_table import describe_table_impl
from schemabrain.mcp.find_relevant_tables import find_relevant_tables_impl
from schemabrain.mcp.server import build_server
from schemabrain.mcp.shapes import (
    ColumnDetail,
    ColumnInfo,
    ColumnNotFoundError,
    ForeignKeyInfo,
    IncomingForeignKeyInfo,
    JoinEdge,
    JoinPath,
    SuggestJoinsResult,
    TableDescription,
    TableHit,
    TableNotFoundError,
)
from schemabrain.mcp.suggest_joins import suggest_joins_impl

__all__ = [
    "ColumnDetail",
    "ColumnInfo",
    "ColumnNotFoundError",
    "ForeignKeyInfo",
    "IncomingForeignKeyInfo",
    "JoinEdge",
    "JoinPath",
    "SuggestJoinsResult",
    "TableDescription",
    "TableHit",
    "TableNotFoundError",
    "build_server",
    "describe_column_impl",
    "describe_table_impl",
    "find_relevant_tables_impl",
    "suggest_joins_impl",
]
