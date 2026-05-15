"""MCP tool implementation: describe_table."""

from __future__ import annotations

from schemabrain.core.store_protocol import Store
from schemabrain.mcp._helpers import _parse_qualified_name, _with_token_estimate
from schemabrain.mcp.shapes import ColumnInfo, ForeignKeyInfo, TableDescription, TableNotFoundError


def describe_table_impl(
    *,
    store: Store,
    source_connection_id: str,
    qualified_name: str,
) -> TableDescription:
    """Return a full `TableDescription` for `qualified_name`.

    Raises:
        ValueError: if `qualified_name` is not in `schema.name` form.
        TableNotFoundError: if the table is absent from the store under
            the configured `source_connection_id`.
    """
    schema, name = _parse_qualified_name(qualified_name)

    table = store.get_table(schema, name, source_connection_id=source_connection_id)
    if table is None:
        raise TableNotFoundError(
            f"{qualified_name} is not in the store for source "
            f"{source_connection_id!r}. Run `schemabrain index` against the "
            f"source database first."
        )

    descriptions = store.get_table_descriptions(
        schema, name, source_connection_id=source_connection_id
    )

    # `Table.columns` already comes back ordered by ordinal_position
    # from the store; preserve that.
    columns = [
        ColumnInfo(
            name=c.name,
            data_type=c.data_type,
            nullable=c.nullable,
            default=c.default,
            is_primary_key=c.is_primary_key,
            description=descriptions[c.name].text if c.name in descriptions else "",
        )
        for c in table.columns
    ]
    foreign_keys = [
        ForeignKeyInfo(
            name=fk.name,
            source_columns=list(fk.source_columns),
            target_qualified_name=f"{fk.target_schema}.{fk.target_table}",
            target_columns=list(fk.target_columns),
        )
        for fk in table.foreign_keys
    ]

    partial = TableDescription(
        qualified_name=qualified_name,
        schema_name=schema,
        name=name,
        columns=columns,
        foreign_keys=foreign_keys,
        token_estimate=0,  # placeholder; rebuilt by _with_token_estimate
    )
    return _with_token_estimate(partial)
