"""FastMCP server wiring for Schema Brain.

`build_server(store, source_connection_id, embedder)` returns a
configured `FastMCP` instance with four tools registered:
`find_relevant_tables`, `describe_table`, `describe_column`, and
`suggest_joins`. The wiring is intentionally thin — all logic lives in
`mcp/tools.py` so it's testable without touching the MCP transport.

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
from schemabrain.mcp.describe_column import describe_column_impl
from schemabrain.mcp.describe_table import describe_table_impl
from schemabrain.mcp.find_relevant_tables import find_relevant_tables_impl
from schemabrain.mcp.shapes import ColumnDetail, SuggestJoinsResult, TableDescription, TableHit
from schemabrain.mcp.suggest_joins import suggest_joins_impl

_DEFAULT_LIMIT = 10
_DEFAULT_MAX_HOPS = 4
_SERVER_NAME = "schemabrain"
_SERVER_INSTRUCTIONS = (
    "Schema Brain — semantic understanding of an indexed database. "
    "Use `find_relevant_tables` to discover which tables are relevant to "
    "a question, `describe_table` to get the full structural and semantic "
    "shape of one table, `describe_column` to drill into a single column "
    "(including which other tables join in to it), and `suggest_joins` "
    "to get ranked FK-graph join paths between two or more tables. All "
    "four tools return token estimates so you can budget context."
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
            "table given its qualified name (`schema.name`, e.g. "
            "`public.orders`). Includes every column with its data type, "
            "nullability, default, primary-key flag, and LLM-generated "
            "description, plus all foreign keys with their target tables. "
            "If you don't know the schema, call `find_relevant_tables` "
            "first to discover the qualified name — passing a bare table "
            "name (`orders`) without the schema prefix returns an error."
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
            "(`schema.table.column`, e.g. `public.orders.user_id`). "
            "Returns the column's structural metadata, LLM description, "
            "and the join graph it participates in: outgoing foreign keys "
            "(this column joins to where) AND incoming foreign keys "
            "(which other tables reference this column). Use this after "
            "`describe_table` when you need to understand a single "
            "column's role across the schema, especially primary keys "
            "whose back-references describe the full join surface. If you "
            "don't know the schema or table, call `find_relevant_tables` "
            "or `describe_table` first to discover them."
        )
    )
    def describe_column(qualified_name: str) -> ColumnDetail:
        return describe_column_impl(
            store=store,
            source_connection_id=source_connection_id,
            qualified_name=qualified_name,
        )

    @app.tool(
        description=(
            "Find shortest foreign-key join paths between two or more "
            "tables. Pass qualified names (`schema.table`) and get back "
            "one path per pair, including the FK columns on each side "
            "ready to drop into a SQL JOIN. Multi-hop paths via "
            "intermediate tables are returned when there's no direct FK. "
            "Pairs that cannot be connected within `max_hops` (default 4) "
            "appear in `unreachable_pairs`. Use this after "
            "`find_relevant_tables` to wire chosen tables into a real "
            "query."
        )
    )
    def suggest_joins(tables: list[str], max_hops: int = _DEFAULT_MAX_HOPS) -> SuggestJoinsResult:
        return suggest_joins_impl(
            store=store,
            source_connection_id=source_connection_id,
            tables=tables,
            max_hops=max_hops,
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
