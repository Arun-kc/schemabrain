"""MCP tool implementation: describe_column.

refuses with ``PiiBlockedError`` when the requested column's
PII categories intersect the effective firewall block set (operator's
``pii_block`` unioned with the catastrophic-leak floor). The MCP
wrapper translates this into a ``pii_blocked`` refusal envelope with
``recovery.suggested_tool="describe_table"`` so the agent's natural
pivot path stays inside the firewall surface.

The whole-column refusal is symmetric to ``describe_table``'s per-
column name-masking: if an agent reaches ``describe_column`` knowing
the real catastrophic-leak column name, it must have learned it from
outside the MCP surface — at which point the firewall's job is to
refuse cleanly rather than leak more shape.
"""

from __future__ import annotations

from schemabrain.core.store_protocol import Store
from schemabrain.mcp._helpers import _parse_column_qualified_name, _with_token_estimate
from schemabrain.mcp._redaction import redact_blocked_fk_columns
from schemabrain.mcp.shapes import (
    ColumnDetail,
    ColumnNotFoundError,
    ForeignKeyInfo,
    IncomingForeignKeyInfo,
    TableNotFoundError,
)
from schemabrain.pii import CATASTROPHIC_LEAK_CATEGORIES, PIICategory
from schemabrain.semantic.compiler import PiiBlockedError


def describe_column_impl(
    *,
    store: Store,
    source_connection_id: str,
    qualified_name: str,
    pii_block: frozenset[PIICategory] = frozenset(),
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
        PiiBlockedError: — if the column's PII categories
            intersect ``pii_block | CATASTROPHIC_LEAK_CATEGORIES``.
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

    # refuse before returning anything if the column's tags
    # intersect the effective firewall block set. The check happens
    # BEFORE we read the description / FK graph so a refusal never
    # carries semantic context that could leak the column's role.
    qualified_table = f"{schema}.{table_name}"
    pii_tags = store.get_column_pii_tags(
        source_connection_id=source_connection_id,
        qualified_table=qualified_table,
        columns=[column_name],
    )
    _, categories = pii_tags.get(column_name, ("public", frozenset()))
    effective_block: frozenset[PIICategory] = pii_block | CATASTROPHIC_LEAK_CATEGORIES
    blocked = categories & effective_block
    if blocked:
        # ``attempted`` is the full propagated category set the agent
        # would have seen on the column; ``blocked`` is the intersection
        # that triggered the refusal. We surface ``blocked`` only in
        # the agent-facing envelope (the server wrapper handles this)
        # for the same probe-resistance reason ``get_metric`` does.
        raise PiiBlockedError(
            f"describe_column refused: column {qualified_name!r} touches "
            f"PII categories {sorted(blocked)} that this server policy blocks",
            attempted_categories=tuple(sorted(categories)),
            blocked_categories=tuple(sorted(blocked)),
            # No anchor entity — describe_column has no semantic-layer
            # binding. The server wrapper routes recovery to
            # ``describe_table`` instead.
            anchor_entity=None,
        )

    descriptions = store.get_table_descriptions(
        schema, table_name, source_connection_id=source_connection_id
    )
    description = descriptions[column_name].text if column_name in descriptions else ""

    # Outgoing FKs: walk the table's FK list and pick any whose
    # `source_columns` includes our column name. Composite FKs are
    # surfaced whole — sibling source columns are kept so the agent
    # sees the full join shape. Both source and target column lists
    # are scrubbed against the effective_block: sibling sources may
    # name another catastrophic-leak column on this table, and
    # target_columns may name one on the referenced table.
    outgoing: list[ForeignKeyInfo] = [
        ForeignKeyInfo(
            name=fk.name,
            source_columns=redact_blocked_fk_columns(
                list(fk.source_columns),
                qualified_table=qualified_table,
                store=store,
                source_connection_id=source_connection_id,
                effective_block=effective_block,
            ),
            target_qualified_name=f"{fk.target_schema}.{fk.target_table}",
            target_columns=redact_blocked_fk_columns(
                list(fk.target_columns),
                qualified_table=f"{fk.target_schema}.{fk.target_table}",
                store=store,
                source_connection_id=source_connection_id,
                effective_block=effective_block,
            ),
        )
        for fk in table.foreign_keys
        if column_name in fk.source_columns
    ]

    # Incoming FKs: ask the store for everything pointing at us.
    # Returns IncomingForeignKey domain objects already filtered to FKs
    # whose `target_columns` includes this column. Scrub both
    # directions: source_columns live on the back-referencing table;
    # target_columns are on THIS table (and may name siblings of the
    # current column that fall in the catastrophic-leak set).
    incoming_raw = store.get_foreign_keys_targeting(
        schema, table_name, column_name, source_connection_id=source_connection_id
    )
    incoming: list[IncomingForeignKeyInfo] = [
        IncomingForeignKeyInfo(
            name=ifk.name,
            source_qualified_name=ifk.source_qualified_name,
            source_columns=redact_blocked_fk_columns(
                list(ifk.source_columns),
                qualified_table=ifk.source_qualified_name,
                store=store,
                source_connection_id=source_connection_id,
                effective_block=effective_block,
            ),
            target_columns=redact_blocked_fk_columns(
                list(ifk.target_columns),
                qualified_table=qualified_table,
                store=store,
                source_connection_id=source_connection_id,
                effective_block=effective_block,
            ),
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
