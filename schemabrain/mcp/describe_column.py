"""MCP tool implementation: describe_column."""

from __future__ import annotations

from schemabrain.core.store import SQLiteStore
from schemabrain.mcp._helpers import _parse_column_qualified_name, _with_token_estimate
from schemabrain.mcp.shapes import (
    ColumnDetail,
    ColumnNotFoundError,
    ForeignKeyInfo,
    IncomingForeignKeyInfo,
    TableNotFoundError,
)


def describe_column_impl(
    *,
    store: SQLiteStore,
    source_connection_id: str,
    qualified_name: str,
) -> ColumnDetail:
    """Return a full `ColumnDetail` for `schema.table.column`.

    Returns the column's structural metadata, LLM description (if any),
    and BOTH directions of FK participation: outgoing FKs where this
    column is in the source list, and incoming FKs where this column is
    in another table's target list (back-references).

    Raises:
        ValueError: if `qualified_name` is not in `schema.table.column` form.
        TableNotFoundError: if the table is absent from the store under
            the configured `source_connection_id`.
        ColumnNotFoundError: if the table exists but the column doesn't.
    """
    schema, table_name, column_name = _parse_column_qualified_name(qualified_name)

    table = store.get_table(schema, table_name, source_connection_id=source_connection_id)
    if table is None:
        raise TableNotFoundError(
            f"{schema}.{table_name} is not in the store for source "
            f"{source_connection_id!r}. Run `schemabrain index` against the "
            f"source database first."
        )

    column = table.get_column(column_name)
    if column is None:
        raise ColumnNotFoundError(
            f"{qualified_name} does not exist on {schema}.{table_name}. "
            f"Existing columns: {sorted(c.name for c in table.columns)}"
        )

    descriptions = store.get_table_descriptions(
        schema, table_name, source_connection_id=source_connection_id
    )
    description = descriptions[column_name].text if column_name in descriptions else ""

    # Outgoing FKs: walk the table's FK list and pick any whose
    # `source_columns` includes our column name. Composite FKs are
    # surfaced whole — sibling source columns are kept so the agent
    # sees the full join shape.
    outgoing: list[ForeignKeyInfo] = [
        ForeignKeyInfo(
            name=fk.name,
            source_columns=list(fk.source_columns),
            target_qualified_name=f"{fk.target_schema}.{fk.target_table}",
            target_columns=list(fk.target_columns),
        )
        for fk in table.foreign_keys
        if column_name in fk.source_columns
    ]

    # Incoming FKs: ask the store for everything pointing at us.
    # Returns IncomingForeignKey domain objects already filtered to FKs
    # whose `target_columns` includes this column.
    incoming_raw = store.get_foreign_keys_targeting(
        schema, table_name, column_name, source_connection_id=source_connection_id
    )
    incoming: list[IncomingForeignKeyInfo] = [
        IncomingForeignKeyInfo(
            name=ifk.name,
            source_qualified_name=ifk.source_qualified_name,
            source_columns=list(ifk.source_columns),
            target_columns=list(ifk.target_columns),
        )
        for ifk in incoming_raw
    ]

    # Reconstruct from parsed parts rather than echoing the user's raw
    # input. Today the parser doesn't normalize, so this is identical to
    # the input string for any successfully-parsed name — but if a future
    # parser ever lowercases/strips, this keeps `qualified_name` aligned
    # with `schema_name`/`table_name`/`name` instead of silently
    # diverging.
    partial = ColumnDetail(
        qualified_name=f"{schema}.{table_name}.{column_name}",
        schema_name=schema,
        table_name=table_name,
        name=column_name,
        data_type=column.data_type,
        nullable=column.nullable,
        default=column.default,
        is_primary_key=column.is_primary_key,
        description=description,
        outgoing_foreign_keys=outgoing,
        incoming_foreign_keys=incoming,
        token_estimate=0,  # placeholder; rebuilt by _with_token_estimate
    )
    return _with_token_estimate(partial)
