"""MCP tool implementation: list_joins.

Surfaces the canonical-join surface as a flat list of summaries.
Returns one `JoinSummary` per confirmed canonical join, ordered
alphabetically by name (the store provides the ordering so the
wire shape is deterministic across repeat calls).

Parity with `list_metrics` and `list_entities` — same lean-summary
philosophy: the agent gets name + the entity pair it connects, and
calls `resolve_join(entity_a, entity_b)` (or passes `name=` to
disambiguate) when it needs the actual SQL skeleton.

Closes the day-one discovery gap surfaced by the 2026-05-21 manual
smoke alongside `list_metrics`: five canonical joins lived in the
store, the agent had no way to enumerate them.
"""

from __future__ import annotations

from schemabrain.core.store_protocol import Store
from schemabrain.mcp.shapes import JoinSummary


def list_joins_impl(
    *,
    store: Store,
    source_connection_id: str,
) -> list[JoinSummary]:
    """Return all canonical joins for `source_connection_id`.

    Ordering is alphabetical by `name`, inherited from the store.

    Direction is preserved per the stored row — `(source_entity,
    target_entity)` reflects how the operator originally confirmed
    the join. `resolve_join` is direction-insensitive at the lookup
    layer, but the summary carries the canonical orientation for
    UI rendering and for the SQL skeleton.
    """
    joins = store.list_canonical_joins(source_connection_id=source_connection_id)
    return [
        JoinSummary(
            name=j.name,
            description=j.description,
            source_entity=j.source_entity,
            target_entity=j.target_entity,
            origin=j.origin,
        )
        for j in joins
    ]
