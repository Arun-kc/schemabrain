"""FastMCP server wiring for Schema Brain.

`build_server(store, source_connection_id, embedder)` returns a
configured `FastMCP` instance with three tools registered:
`find_relevant_tables`, `describe_table`, and `describe_column`. The
wiring is intentionally thin — all logic lives in `mcp/tools.py` so
it's testable without touching the MCP transport.

`run_stdio()` is a convenience that builds the server and runs it on
stdio (the transport Claude Desktop and most local-MCP clients use).
HTTP / SSE transports are exposed by the underlying `mcp` SDK and could
be wired in later by passing `transport="streamable-http"` to
`FastMCP.run`.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from schemabrain.core.store import SQLiteStore
from schemabrain.enrichment.embeddings import Embedder
from schemabrain.mcp.tools import (
    ColumnDetail,
    TableDescription,
    TableHit,
    describe_column_impl,
    describe_table_impl,
    find_relevant_tables_impl,
)

_DEFAULT_LIMIT = 10
_SERVER_NAME = "schemabrain"
_SERVER_INSTRUCTIONS = (
    "Schema Brain — semantic understanding of an indexed database. "
    "Use `find_relevant_tables` to discover which tables are relevant to "
    "a question, `describe_table` to get the full structural and semantic "
    "shape of one table, and `describe_column` to drill into a single "
    "column (including which other tables join in to it). All three "
    "tools return token estimates so you can budget context."
)


def build_server(
    *,
    store: SQLiteStore,
    source_connection_id: str,
    embedder: Embedder,
) -> FastMCP:
    """Build (but do not run) a configured `FastMCP` app.

    The store, source ID, and embedder are captured by closure on the
    registered tool callables — that's the only state the tools share.
    Returned app is ready to `.run("stdio")` or to be exercised
    in-process via `app.call_tool(...)` for tests.
    """
    app = FastMCP(_SERVER_NAME, instructions=_SERVER_INSTRUCTIONS)

    @app.tool(
        description=(
            "Find tables in the indexed database most relevant to a natural-"
            "language question. Returns ranked hits, each with a cosine-"
            "similarity score, the matched column name, and that column's "
            "description. Use this first to discover which tables to dig "
            "into via `describe_table`."
        )
    )
    def find_relevant_tables(query: str, limit: int = _DEFAULT_LIMIT) -> list[TableHit]:
        return find_relevant_tables_impl(
            store=store,
            source_connection_id=source_connection_id,
            embedder=embedder,
            query=query,
            limit=limit,
        )

    @app.tool(
        description=(
            "Return the full structural and semantic description of one "
            "table given its qualified name (`schema.name`). Includes every "
            "column with its data type, nullability, default, primary-key "
            "flag, and LLM-generated description, plus all foreign keys "
            "with their target tables."
        )
    )
    def describe_table(qualified_name: str) -> TableDescription:
        return describe_table_impl(
            store=store,
            source_connection_id=source_connection_id,
            qualified_name=qualified_name,
        )

    @app.tool(
        description=(
            "Drill into one column given its three-part qualified name "
            "(`schema.table.column`). Returns the column's structural "
            "metadata, LLM description, and the join graph it participates "
            "in: outgoing foreign keys (this column joins to where) AND "
            "incoming foreign keys (which other tables reference this "
            "column). Use this after `describe_table` when you need to "
            "understand a single column's role across the schema, "
            "especially primary keys whose back-references describe the "
            "full join surface."
        )
    )
    def describe_column(qualified_name: str) -> ColumnDetail:
        return describe_column_impl(
            store=store,
            source_connection_id=source_connection_id,
            qualified_name=qualified_name,
        )

    return app


def run_stdio(
    *,
    store: SQLiteStore,
    source_connection_id: str,
    embedder: Embedder,
) -> None:
    """Build the server and run it forever on stdio.

    Blocks until the client disconnects. Used by the `schemabrain serve`
    CLI subcommand.
    """
    app = build_server(store=store, source_connection_id=source_connection_id, embedder=embedder)
    app.run(transport="stdio")
