"""Junction-bridge synthesis — surface M:N relationships as logical bridges.

A junction (association) table like `product_categories(product_id,
category_id)` is itself an entity in the store, with two canonical
joins out (`product_categories -> products`, `product_categories ->
categories`). The agent calling `list_joins` from the `products`
perspective never sees `categories` as related — there is no direct
`products -> categories` canonical join.

This module synthesises a BRIDGE: a logical join `products <->
categories via product_categories` derived at read time from the
junction-table topology already present in the store. Nothing is
persisted; bridges are computed fresh on every call so a follow-up
edit to either side of the junction reflects immediately.

Detection reuses `Table.is_junction_table()` — the heuristic already
codified in `core/models.py` (composite PK whose columns are all FK
sources to >=2 distinct targets). Operators who want to suppress a
specific bridge can do so today by deleting the junction entity from
the store; a future operator-override surface is out of scope here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from schemabrain.core.entity import Entity
from schemabrain.core.join import CanonicalJoin, JoinColumnPair
from schemabrain.core.store_protocol import Store


@dataclass(frozen=True)
class BridgeJoin:
    """A logical bridge join between two non-junction entities.

    `source_entity` and `target_entity` are the two real-world entities
    the bridge connects (e.g. `products` and `categories`). `via_junction`
    names the junction entity in the middle (e.g. `product_categories`).
    `via_joins` carries the two underlying canonical-join names so the
    agent can resolve each leg via `resolve_join` if it wants the raw
    SQL skeleton.

    `name` is synthesised as `<source>_<target>_via_<junction>` for
    deterministic equality across calls.

    Inference is conservative: the bridge inherits the WORST signal of
    its two legs. If either leg is `llm_suggested`, the bridge is
    `llm_suggested` too — the agent must not trust the bridge more than
    its weakest link.
    """

    name: str
    source_entity: str
    target_entity: str
    via_junction: str
    via_joins: tuple[str, str]
    inference_method: str
    validation_state: str


def find_junction_entities(*, store: Store, source_connection_id: str) -> list[Entity]:
    """Return the entities whose bound table is a junction (M:N pivot).

    Order matches `store.list_entities` (alphabetical by name). An empty
    list is returned when the store contains no junction-shaped tables —
    the common case for schemas that model M:N via embedded arrays or
    do not have junctions at all.
    """
    entities = store.list_entities(source_connection_id=source_connection_id)
    junctions: list[Entity] = []
    for entity in entities:
        schema, name = entity.qualified_table.split(".", 1)
        table = store.get_table(schema, name, source_connection_id=source_connection_id)
        if table is None:
            continue
        if table.is_junction_table():
            junctions.append(entity)
    return junctions


def synthesize_bridges(
    *,
    store: Store,
    source_connection_id: str,
) -> list[BridgeJoin]:
    """Return every bridge implied by the junction topology in the store.

    Algorithm: walk junction entities, group their canonical joins by
    the OTHER end of each edge, and emit one bridge per unordered pair
    of other-ends. A junction connecting N entities yields N*(N-1)/2
    bridges (the most common N=2 yields exactly one).

    The bridge's `source_entity` / `target_entity` are ordered
    alphabetically so a junction connecting `a` and `b` always produces
    `(a, b)` regardless of how the underlying canonical joins are
    oriented. This avoids surfacing two near-duplicate bridges that
    differ only in direction; the renderer can flip if it wants.

    Inference downgrade: bridge inherits the WORST inference_method of
    the two legs (the closest-to-llm_suggested). Same for
    validation_state (closest-to-draft wins).
    """
    junctions = find_junction_entities(store=store, source_connection_id=source_connection_id)
    if not junctions:
        return []

    all_joins = store.list_canonical_joins(source_connection_id=source_connection_id)
    bridges: list[BridgeJoin] = []
    seen_names: set[str] = set()
    for junction in junctions:
        legs = _legs_touching(junction.name, all_joins)
        # Need at least 2 legs to form a bridge — a junction entity
        # with only one canonical join cannot bridge anything.
        if len(legs) < 2:
            continue
        for i in range(len(legs)):
            for j in range(i + 1, len(legs)):
                leg_a, end_a = legs[i]
                leg_b, end_b = legs[j]
                if (
                    end_a == end_b
                ):  # pragma: no cover — defensive self-bridge skip; CanonicalJoin refuses self-joins at leg level
                    continue
                lo, hi = sorted([end_a, end_b])
                # If lo == end_a, leg_lo is leg_a; else leg_b.
                leg_lo = leg_a if lo == end_a else leg_b
                leg_hi = leg_b if lo == end_a else leg_a
                name = f"{lo}_{hi}_via_{junction.name}"
                if (
                    name in seen_names
                ):  # pragma: no cover — defensive dedup; bridge names are unique by (lo, hi, junction)
                    continue
                seen_names.add(name)
                bridges.append(
                    BridgeJoin(
                        name=name,
                        source_entity=lo,
                        target_entity=hi,
                        via_junction=junction.name,
                        via_joins=(leg_lo.name, leg_hi.name),
                        inference_method=_worst_inference(
                            leg_lo.inference_method, leg_hi.inference_method
                        ),
                        validation_state=_worst_validation(
                            leg_lo.validation_state, leg_hi.validation_state
                        ),
                    )
                )
    bridges.sort(key=lambda b: b.name)
    return bridges


def synthesize_bridges_for_entity(
    *,
    store: Store,
    source_connection_id: str,
    entity_name: str,
) -> list[BridgeJoin]:
    """Return bridges where `entity_name` is one of the two endpoints.

    Convenience for the inspect-engine path — drilling into `products`
    wants only the bridges touching `products`, not every bridge in the
    store. Computes the full bridge list then filters; the cost is
    bounded by the number of junctions, which is small in practice.
    """
    all_bridges = synthesize_bridges(store=store, source_connection_id=source_connection_id)
    return [b for b in all_bridges if entity_name in (b.source_entity, b.target_entity)]


def composed_on_pairs(
    *,
    bridge: BridgeJoin,
    leg_lo: CanonicalJoin,
    leg_hi: CanonicalJoin,
) -> tuple[tuple[JoinColumnPair, ...], tuple[JoinColumnPair, ...]]:
    """Return the two on-clause column pair tuples a bridge implies.

    A bridge `A <-> B via J` is two joins under the hood:
      JOIN J ON A.x = J.y       — `a_to_j` pairs
      JOIN B ON J.z = B.w       — `j_to_b` pairs

    Each leg may be stored in either direction (`source = J, target = A`
    or `source = A, target = J`); this helper normalises both so the
    returned pairs always read `<low-end>.col = <junction>.col` then
    `<junction>.col = <high-end>.col`. Composite-key joins are preserved
    intact — the function does not flatten or reorder column pairs.
    """
    a_to_j = _orient_pairs(
        from_entity=bridge.source_entity, to_entity=bridge.via_junction, join=leg_lo
    )
    j_to_b = _orient_pairs(
        from_entity=bridge.via_junction, to_entity=bridge.target_entity, join=leg_hi
    )
    return a_to_j, j_to_b


# ----- internals ------------------------------------------------------------


def _legs_touching(
    junction_name: str, joins: Sequence[CanonicalJoin]
) -> list[tuple[CanonicalJoin, str]]:
    """Return canonical joins where `junction_name` appears, paired with
    the OTHER entity (the non-junction end of the leg).

    Sorted by other-end name for deterministic bridge naming.
    """
    legs: list[tuple[CanonicalJoin, str]] = []
    for join in joins:
        if join.source_entity == junction_name:
            legs.append((join, join.target_entity))
        elif join.target_entity == junction_name:
            legs.append((join, join.source_entity))
    legs.sort(key=lambda x: (x[1], x[0].name))
    return legs


def _orient_pairs(
    *, from_entity: str, to_entity: str, join: CanonicalJoin
) -> tuple[JoinColumnPair, ...]:
    """Return `join.on` re-oriented so the first column in each pair
    belongs to `from_entity` and the second to `to_entity`.

    Raises `ValueError` if the join doesn't connect these two entities
    — a defensive guard since the bridge synthesiser only ever passes
    legs known to touch both ends.
    """
    if join.source_entity == from_entity and join.target_entity == to_entity:
        return join.on
    if join.source_entity == to_entity and join.target_entity == from_entity:
        return tuple(
            JoinColumnPair(source_column=p.target_column, target_column=p.source_column)
            for p in join.on
        )
    raise ValueError(
        f"join {join.name!r} does not connect {from_entity!r} and "
        f"{to_entity!r} (it connects {join.source_entity!r} and "
        f"{join.target_entity!r})"
    )


# Inference-method ranking — lower index is stronger.
_METHOD_RANK: dict[str, int] = {
    "fk_constraint": 0,
    "dbt_import": 1,
    "manually_authored": 2,
    "observed_in_query_log": 3,
    "llm_suggested": 4,
}

_VALIDATION_RANK: dict[str, int] = {
    "confirmed": 0,
    "applied": 1,
    "draft": 2,
}


def _worst_inference(a: str, b: str) -> str:
    """Return the weaker of two inference methods (highest rank wins)."""
    rank_a = _METHOD_RANK.get(a, len(_METHOD_RANK))
    rank_b = _METHOD_RANK.get(b, len(_METHOD_RANK))
    return a if rank_a >= rank_b else b


def _worst_validation(a: str, b: str) -> str:
    """Return the weaker of two validation states (highest rank wins)."""
    rank_a = _VALIDATION_RANK.get(a, len(_VALIDATION_RANK))
    rank_b = _VALIDATION_RANK.get(b, len(_VALIDATION_RANK))
    return a if rank_a >= rank_b else b


__all__ = [
    "BridgeJoin",
    "composed_on_pairs",
    "find_junction_entities",
    "synthesize_bridges",
    "synthesize_bridges_for_entity",
]
