"""MCP tool implementation: describe_table.

applies the firewall's catastrophic-leak floor at the agent-
facing boundary so an agent that calls ``describe_table`` cannot
retrieve the real names of credential / payment_card / government_id
columns. Without this, an agent that hit ``find_relevant_tables`` (or
its degraded fallback) could read the column names, then emit raw SQL
that referenced them directly — bypassing the ``get_metric`` firewall
entirely. The actual column NAMES live on in the store and on the
operator-facing dashboard; only the MCP wire response is masked.

The redaction policy mirrors ``describe_entity``'s contract: the
effective block set is ``pii_block | CATASTROPHIC_LEAK_CATEGORIES`` —
the operator's explicit policy union with the always-on minimum-decency
floor. Categories in the floor are redacted regardless of operator
policy because no plausible aggregate-analytics use case justifies an
agent reading them, per ADR-0001.
"""

from __future__ import annotations

from schemabrain.core.store_protocol import Store
from schemabrain.mcp._helpers import _parse_qualified_name, _with_token_estimate
from schemabrain.mcp.shapes import ColumnInfo, ForeignKeyInfo, TableDescription, TableNotFoundError
from schemabrain.pii import CATASTROPHIC_LEAK_CATEGORIES, PIICategory


def describe_table_impl(
    *,
    store: Store,
    source_connection_id: str,
    qualified_name: str,
    pii_block: frozenset[PIICategory] = frozenset(),
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
        # Reconstruct from validated parts rather than echoing the raw
        # user input — consistent with the bounded-echo discipline on
        # `_parse_qualified_name`'s error path.
        raise TableNotFoundError(
            f"{schema}.{name} is not in the store for source "
            f"{source_connection_id!r}. Run `schemabrain index` against the "
            f"source database first."
        )

    descriptions = store.get_table_descriptions(
        schema, name, source_connection_id=source_connection_id
    )

    # pull PII tags for the columns we're about to expose and
    # compute the effective block set the same way ``describe_entity``
    # does — operator policy unioned with the catastrophic-leak floor.
    pii_tags = store.get_column_pii_tags(
        source_connection_id=source_connection_id,
        qualified_table=qualified_name,
        columns=[c.name for c in table.columns],
    )
    effective_block: frozenset[PIICategory] = pii_block | CATASTROPHIC_LEAK_CATEGORIES

    # `Table.columns` already comes back ordered by ordinal_position
    # from the store; preserve that.
    columns: list[ColumnInfo] = []
    redacted_real_names: list[str] = []
    # Per-category counter for deterministic placeholder naming. Sorted
    # category iteration so the placeholder index is stable across
    # process restarts (frozenset iteration would vary with
    # PYTHONHASHSEED otherwise).
    category_counters: dict[str, int] = {}
    for c in table.columns:
        _, categories = pii_tags.get(c.name, ("public", frozenset()))
        blocked = categories & effective_block
        if blocked:
            redacted_real_names.append(c.name)
            # Pick the alphabetically-first matching category for the
            # placeholder so a multi-category column has a deterministic
            # label (`<redacted_credential_column_1>` rather than the
            # iteration-order-dependent first match).
            cat = sorted(blocked)[0]
            category_counters[cat] = category_counters.get(cat, 0) + 1
            placeholder = f"<redacted_{cat}_column_{category_counters[cat]}>"
            columns.append(
                ColumnInfo(
                    name=placeholder,
                    data_type=c.data_type,
                    nullable=c.nullable,
                    # ``default`` and ``is_primary_key`` are masked too
                    # because either could reveal the real column to a
                    # determined agent (e.g. a PK card-number column is
                    # rare but possible). Treat the column as opaque.
                    default=None,
                    is_primary_key=False,
                    description="",
                    redacted=True,
                )
            )
        else:
            columns.append(
                ColumnInfo(
                    name=c.name,
                    data_type=c.data_type,
                    nullable=c.nullable,
                    default=c.default,
                    is_primary_key=c.is_primary_key,
                    description=descriptions[c.name].text if c.name in descriptions else "",
                    redacted=False,
                )
            )
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
        redacted_columns=tuple(redacted_real_names),
    )
    return _with_token_estimate(partial)
