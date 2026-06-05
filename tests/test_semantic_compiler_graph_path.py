"""Tests for the public canonical-path builder in `semantic/compiler/resolve.py`.

The graph projection (PR-16, ADR 0010) needs a path builder that:

  - reuses the resolver's structural BFS (`_build_join_graph` /
    `_structural_shortest_paths`) with NO `get_metric` / metric-plan
    dependency,
  - NEVER raises (unlike `resolve_metric_plan`, which surfaces four
    refusal classes) — an absent / equal / unreachable endpoint yields
    an empty path,
  - is deterministic (BFS order + first-alphabetical canonical per hop),
  - and exposes `longest_canonical_path` (the graph diameter) as the
    single honest "canonical path" of a whole schema.

These tests are PURE: the builder takes a `joins` list, not a store.
"""

from __future__ import annotations

from schemabrain.core.join import CanonicalJoin, JoinColumnPair
from schemabrain.semantic.compiler import (
    CanonicalPath,
    build_canonical_path,
    longest_canonical_path,
)


def _join(
    name: str,
    source_entity: str,
    target_entity: str,
    *,
    inference_method: str = "fk_constraint",
) -> CanonicalJoin:
    return CanonicalJoin(
        name=name,
        description="",
        source_entity=source_entity,
        target_entity=target_entity,
        on=(JoinColumnPair(source_column="id", target_column="id"),),
        cardinality="many_to_one",
        inference_method=inference_method,  # type: ignore[arg-type]
    )


# A 4-node hierarchical spine: order_item → order → user → tenant.
def _saas_spine() -> list[CanonicalJoin]:
    return [
        _join("order_item_to_order", "order_item", "order"),
        _join("order_to_user", "order", "user"),
        _join("user_to_tenant", "user", "tenant"),
    ]


class TestBuildCanonicalPath:
    def test_empty_joins_yields_empty_path(self) -> None:
        path = build_canonical_path(joins=[], anchor="a", target="b")
        assert path == CanonicalPath(nodes=(), edges=())
        assert path.hops == 0

    def test_single_hop(self) -> None:
        joins = [_join("a_to_b", "a", "b")]
        path = build_canonical_path(joins=joins, anchor="a", target="b")
        assert path.nodes == ("a", "b")
        assert path.edges == ("a_to_b",)
        assert path.hops == 1

    def test_multi_hop_chain_is_ordered(self) -> None:
        path = build_canonical_path(joins=_saas_spine(), anchor="order_item", target="tenant")
        assert path.nodes == ("order_item", "order", "user", "tenant")
        assert path.edges == (
            "order_item_to_order",
            "order_to_user",
            "user_to_tenant",
        )
        assert path.hops == 3

    def test_reverse_direction_walks_backwards(self) -> None:
        # The canonical-join graph is undirected for traversal — a path
        # from tenant back to order_item is the reverse node sequence AND
        # the reverse edge-name sequence (edge order is the wire contract
        # PR-17 draws arrows from, so pin it both directions).
        path = build_canonical_path(joins=_saas_spine(), anchor="tenant", target="order_item")
        assert path.nodes == ("tenant", "user", "order", "order_item")
        assert path.edges == ("user_to_tenant", "order_to_user", "order_item_to_order")
        assert path.hops == 3

    def test_deep_chain_beyond_metric_cap_is_not_truncated(self) -> None:
        # A 9-node chain (8 hops) is deeper than the metric resolver's
        # 6-hop default. The read-model must return the FULL path, not a
        # truncated slice or an empty "unreachable" result (ADR 0010).
        joins = [_join(f"e{i}_to_e{i + 1}", f"e{i}", f"e{i + 1}") for i in range(8)]
        path = build_canonical_path(joins=joins, anchor="e0", target="e8")
        assert path.nodes == tuple(f"e{i}" for i in range(9))
        assert path.hops == 8

    def test_missing_endpoint_yields_empty(self) -> None:
        path = build_canonical_path(joins=_saas_spine(), anchor="order_item", target="ghost")
        assert path == CanonicalPath(nodes=(), edges=())

    def test_anchor_equals_target_yields_empty(self) -> None:
        path = build_canonical_path(joins=_saas_spine(), anchor="order", target="order")
        assert path == CanonicalPath(nodes=(), edges=())

    def test_disconnected_components_yield_empty(self) -> None:
        joins = [_join("a_to_b", "a", "b"), _join("c_to_d", "c", "d")]
        path = build_canonical_path(joins=joins, anchor="a", target="d")
        assert path == CanonicalPath(nodes=(), edges=())

    def test_is_deterministic_across_runs(self) -> None:
        joins = _saas_spine()
        first = build_canonical_path(joins=joins, anchor="order_item", target="tenant")
        second = build_canonical_path(joins=joins, anchor="order_item", target="tenant")
        assert first == second

    def test_parallel_canonicals_collapse_to_first_alpha_never_raises(self) -> None:
        # `order → address` via billing AND shipping. The metric compiler
        # would raise AmbiguousJoinError; the read-model projection must
        # pick deterministically and never raise.
        joins = [
            _join("order_shipping_address", "order", "address"),
            _join("order_billing_address", "order", "address"),
        ]
        path = build_canonical_path(joins=joins, anchor="order", target="address")
        assert path.nodes == ("order", "address")
        # First alphabetical canonical-join name on the hop.
        assert path.edges == ("order_billing_address",)


class TestLongestCanonicalPath:
    def test_empty_joins_yields_empty(self) -> None:
        assert longest_canonical_path(joins=[]) == CanonicalPath(nodes=(), edges=())

    def test_picks_the_diameter_spine_deterministically(self) -> None:
        path = longest_canonical_path(joins=_saas_spine())
        assert path.nodes == ("order_item", "order", "user", "tenant")
        assert path.edges == (
            "order_item_to_order",
            "order_to_user",
            "user_to_tenant",
        )
        assert path.hops == 3

    def test_tie_breaks_lexicographically_on_nodes(self) -> None:
        # Two disjoint 1-hop chains of equal length; the lexicographically
        # smaller node tuple wins deterministically.
        joins = [_join("m_to_z", "m", "z"), _join("a_to_b", "a", "b")]
        path = longest_canonical_path(joins=joins)
        assert path.nodes == ("a", "b")
        assert path.edges == ("a_to_b",)

    def test_single_join_is_its_own_diameter(self) -> None:
        path = longest_canonical_path(joins=[_join("a_to_b", "a", "b")])
        assert path.nodes == ("a", "b")
        assert path.hops == 1

    def test_longest_component_wins_over_disconnected_shorter(self) -> None:
        # A 3-hop component + an isolated 1-hop component. The diameter is
        # the longer component regardless of scan order (locks the global
        # longest-component selection, not a per-component reset).
        joins = [
            _join("a_to_b", "a", "b"),
            _join("b_to_c", "b", "c"),
            _join("c_to_d", "c", "d"),
            _join("x_to_y", "x", "y"),
        ]
        path = longest_canonical_path(joins=joins)
        assert path.nodes == ("a", "b", "c", "d")
        assert path.hops == 3

    def test_deep_chain_diameter_is_not_truncated(self) -> None:
        # An 8-hop chain: longest_canonical_path must return the full
        # diameter, not the metric resolver's 6-hop cap (ADR 0010).
        joins = [_join(f"e{i}_to_e{i + 1}", f"e{i}", f"e{i + 1}") for i in range(8)]
        path = longest_canonical_path(joins=joins)
        assert path.hops == 8
        assert path.nodes == tuple(f"e{i}" for i in range(9))
