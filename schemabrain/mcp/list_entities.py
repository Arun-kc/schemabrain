"""MCP tool implementation: list_entities.

Surfaces the entity surface as a flat list of summaries. Returns
one `EntitySummary` per confirmed entity, ordered alphabetically by
name (the store provides the ordering so the wire shape is
deterministic across repeat calls).

`list_entities` takes no arguments at this release — at v1 scale
(dozens of entities at most) pagination is unnecessary and the
surface stays narrow. Adding a `limit` parameter later would be a
charter event worth deliberating; today's review ergonomics depend
on the full list being one call away.
"""

from __future__ import annotations

from schemabrain.core.store_protocol import Store
from schemabrain.mcp.shapes import EntitySummary


def list_entities_impl(
    *,
    store: Store,
    source_connection_id: str,
) -> list[EntitySummary]:
    """Return all confirmed entities for `source_connection_id`.

    Ordering is alphabetical by `name`, inherited from the store —
    callers (MCP envelope wrapper, CLI list view) depend on this so
    they don't have to re-sort.

    Each summary joins the binding's `(binding_schema, binding_table)`
    back into the YAML-grammar `schema.table` form so the wire shape
    matches the file shape an agent might be asked to author.
    """
    entities = store.list_entities(source_connection_id=source_connection_id)
    return [
        EntitySummary(
            name=e.name,
            description=e.description,
            qualified_table=e.qualified_table,
            identity=e.identity,
            origin=e.origin,
            inference_method=e.inference_method,
            validation_state=e.validation_state,
        )
        for e in entities
    ]
