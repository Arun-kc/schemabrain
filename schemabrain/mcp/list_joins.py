"""MCP tool implementation: list_joins.

Surfaces the canonical-join surface as a flat list of summaries.
Returns one `JoinSummary` per confirmed canonical join, plus any
synthesised junction bridges (M:N pivots — `products <-> categories
via product_categories`). Both kinds sort together alphabetically by
name; bridges carry `via_junction` + `via_joins` so an agent can tell
them apart at a glance.

Parity with `list_metrics` and `list_entities` — same lean-summary
philosophy: the agent gets name + the entity pair it connects, and
calls `resolve_join(entity_a, entity_b)` (or passes `name=` to
disambiguate) when it needs the actual SQL skeleton. For a bridge,
the agent calls `resolve_join` on each of `via_joins` to compose the
chain.

Closes a discovery gap alongside `list_metrics`: when canonical
joins live in the store but the agent has no way to enumerate
them, deep-joins-required questions hit a dead end. Bridges close
the same gap for M:N relationships — `categories` is reachable from
`products` via `product_categories`, but without bridge synthesis it
looked orphaned to the agent.
"""

from __future__ import annotations

from schemabrain.core.store_protocol import Store
from schemabrain.joins.bridges import synthesize_bridges
from schemabrain.mcp.shapes import JoinSummary


def list_joins_impl(
    *,
    store: Store,
    source_connection_id: str,
) -> list[JoinSummary]:
    """Return all canonical joins for `source_connection_id`, plus
    junction-bridge summaries.

    Ordering is alphabetical by `name`, with bridges interleaved into
    the same alpha order. Direct joins are returned with `via_junction
    = None`; bridges set both `via_junction` and `via_joins`.

    Direction is preserved per the stored row for direct joins. For
    bridges, `source_entity` / `target_entity` are alphabetically
    ordered (so a single bridge represents the M:N relationship from
    either side rather than appearing twice).
    """
    joins = store.list_canonical_joins(source_connection_id=source_connection_id)
    summaries: list[JoinSummary] = [
        JoinSummary(
            name=j.name,
            description=j.description,
            source_entity=j.source_entity,
            target_entity=j.target_entity,
            origin=j.origin,
            inference_method=j.inference_method,
            validation_state=j.validation_state,
        )
        for j in joins
    ]
    bridges = synthesize_bridges(
        store=store, source_connection_id=source_connection_id
    )
    for b in bridges:
        summaries.append(
            JoinSummary(
                name=b.name,
                description=(
                    f"M:N bridge through {b.via_junction!r}; the agent "
                    f"composes the chain via resolve_join on each leg."
                ),
                source_entity=b.source_entity,
                target_entity=b.target_entity,
                # Bridges are read-only synthetics; `origin` describes
                # the closest stored counterpart, which is `suggested`
                # at write time for FK-derived joins. The 2D signal
                # below carries the precise provenance.
                origin="suggested",
                inference_method=b.inference_method,  # type: ignore[arg-type]
                validation_state=b.validation_state,  # type: ignore[arg-type]
                via_junction=b.via_junction,
                via_joins=b.via_joins,
            )
        )
    summaries.sort(key=lambda s: s.name)
    return summaries
