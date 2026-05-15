"""MCP tool implementation: get_example_queries.

Surfaces observed SQL patterns for a single indexed table. v0.5 reads
from the `example_queries` store table; the writer
(`pg_stat_statements` mining + `sqlglot` parsing) lands in the next
PR. Until mining writes, every table returns an empty list — the MCP
boundary maps that to a charter-compliant `status="empty"` envelope.

The implementation is intentionally thin: parse the qualified name,
confirm the parent table is in the index (so unknown-name and empty-
examples are distinct conditions), then ask the store for ranked
example rows. The Phase B PII fields surface unchanged through the
Pydantic shape.
"""

from __future__ import annotations

from schemabrain.core.store_protocol import Store
from schemabrain.mcp._helpers import _parse_qualified_name, _with_token_estimate
from schemabrain.mcp.shapes import (
    ExampleQueriesResult,
    ExampleQueryItem,
    TableNotFoundError,
)

# Default number of examples returned per call. Large enough that an
# agent rarely needs to paginate at v0.5 scale (one workload's worth
# of mined SQL per table); small enough that the response stays well
# inside MCP token budgets even when every SQL is long.
_DEFAULT_LIMIT = 10


def get_example_queries_impl(
    *,
    store: Store,
    source_connection_id: str,
    qualified_name: str,
    limit: int = _DEFAULT_LIMIT,
) -> ExampleQueriesResult:
    """Return observed example SQL for `qualified_name` ranked by
    `(observation_count DESC, last_seen_at DESC, id ASC)`.

    Raises `ValueError` for a malformed qualified name (not
    `schema.table`), and `TableNotFoundError` when the table isn't in
    the index for `source_connection_id`. An indexed table with no
    examples returns a result with `queries=[]` — the MCP boundary
    distinguishes that empty state from the unknown-table error.

    `limit` defaults to `_DEFAULT_LIMIT` and propagates to the store's
    `list_example_queries` contract; the store raises `ValueError` on
    `limit < 1`.
    """
    schema, table = _parse_qualified_name(qualified_name)

    # Confirm the table exists in the index. The store's
    # `list_example_queries` would otherwise return `[]` regardless,
    # collapsing the "unknown table" and "no examples yet" cases into
    # one — the MCP layer wants them as separate envelopes.
    if store.get_table(schema, table, source_connection_id=source_connection_id) is None:
        raise TableNotFoundError(
            f"No indexed table named {qualified_name!r} for this source. "
            f"Call `find_relevant_tables` to discover the qualified names "
            f"of indexed tables."
        )

    rows = store.list_example_queries(
        schema=schema,
        table=table,
        source_connection_id=source_connection_id,
        limit=limit,
    )
    items = [
        ExampleQueryItem(
            sql_text=row.sql_text,
            observation_count=row.observation_count,
            source=row.source,
            sensitivity=row.sensitivity,
            # frozenset → sorted list so the JSON the agent sees has a
            # stable, deterministic order.
            pii_categories=sorted(row.pii_categories),
        )
        for row in rows
    ]
    partial = ExampleQueriesResult(
        qualified_name=qualified_name,
        queries=items,
        token_estimate=0,  # placeholder; _with_token_estimate rebuilds
    )
    return _with_token_estimate(partial)
