"""MCP tool implementation: describe_entity.

Surfaces the full shape of one confirmed entity — its YAML-grammar
fields (name, description, binding, identity, origin) plus the
columns of the bound table with their LLM-enriched descriptions and
PII sensitivity classifications.

At this release every column carries the inert
`pii_sensitivity="public"` default — the shape is locked so future
PII-redaction work can populate real values without retrofitting
the envelope.

The bound-table lookup relies on the store-side FK invariant: an
entity can only reference an indexed table, so a non-None entity
implies a non-None bound table. Defensive None-handling would be
unreachable code under the v8 schema contract.
"""

from __future__ import annotations

from schemabrain.core.store_protocol import Store
from schemabrain.mcp._helpers import _validate_ident, _with_token_estimate
from schemabrain.mcp.shapes import (
    EntityColumn,
    EntityDetail,
    EntityNotFoundError,
)


def describe_entity_impl(
    *,
    store: Store,
    source_connection_id: str,
    name: str,
) -> EntityDetail:
    """Return the full `EntityDetail` for `name`.

    Raises `ValueError` for a malformed name (empty, too long, non-
    identifier shape) and `EntityNotFoundError` when no entity by
    that name exists for `source_connection_id`. The two error
    surfaces are distinct so the MCP wrapper can route them to
    `malformed_name` vs `unknown_name` envelopes respectively.

    Every column of the bound table is exposed (no YAML allowlist
    at this release). Per-column descriptions come from the same
    store layer that backs `describe_table`, so an entity inherits
    whatever enrichment its bound table has received.
    """
    _validate_ident(name, role="entity")

    entity = store.get_entity(name, source_connection_id=source_connection_id)
    if entity is None:
        raise EntityNotFoundError(
            f"No entity named {name!r} for this source. Call `list_entities` to see what's defined."
        )

    # Bound-table lookup. The v8 schema's FK constraint guarantees
    # this is non-None when the entity is non-None — but we raise
    # a real exception (not an `assert`, which `python -O` strips)
    # so a corrupt store still surfaces a diagnostic message through
    # the server's `_wrap_internal_error` catch-all rather than a
    # bare AttributeError on the next line.
    schema, table_name = entity.qualified_table.split(".", 1)
    table = store.get_table(schema, table_name, source_connection_id=source_connection_id)
    if table is None:
        raise RuntimeError(
            f"FK invariant violated: entity {name!r} references "
            f"missing table {entity.qualified_table!r}"
        )

    descriptions = store.get_table_descriptions(
        schema_name=schema,
        name=table_name,
        source_connection_id=source_connection_id,
    )

    columns = [
        EntityColumn(
            name=col.name,
            data_type=col.data_type,
            nullable=col.nullable,
            description=(descriptions[col.name].text if col.name in descriptions else ""),
            # Hardcoded inert default; a future release will populate
            # from real per-column PII classification.
            pii_sensitivity="public",
        )
        for col in table.columns
    ]

    partial = EntityDetail(
        name=entity.name,
        description=entity.description,
        qualified_table=entity.qualified_table,
        identity=entity.identity,
        origin=entity.origin,
        columns=columns,
        token_estimate=0,  # placeholder; `_with_token_estimate` rebuilds
        inference_method=entity.inference_method,
        validation_state=entity.validation_state,
    )
    return _with_token_estimate(partial)
