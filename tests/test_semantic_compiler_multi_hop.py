"""Tests for multi-hop canonical-join resolution in the metric compiler.

Locks the contract introduced in PR-6h.1:

  - group_by on an entity 2+ hops from anchor resolves via BFS over
    the canonical-join graph; intermediate hops appear as ResolvedJoins
    in TOPOLOGICAL chain order.
  - A canonical join traversed in the REVERSE of its stored direction
    emits with SWAPPED on-pair columns so the SQL skeleton remains
    valid regardless of which side the metric is anchored on.
  - Two group_by columns whose chains share an intermediate (e.g.
    both transit `order`) produce ONE shared JOIN for the intermediate.
  - Two equal-length paths refuse with `AmbiguousPathError`, listing
    candidate paths in canonical-name-sorted form.
  - `via=(join_name, ...)` constrains the chain to traverse those
    joins. Unknown via raises `UnknownViaJoinError`.
  - Single-edge parallel-join ambiguity inside a multi-hop chain
    still raises `AmbiguousJoinError` (preserves the v1 contract for
    that case).

Closes the day-one product gap surfaced by the 2026-05-21 live smoke:
`get_metric(name="total_items_sold", group_by=["user.email"])` against
the order_item-anchored metric must traverse
`order_item → order → user` instead of refusing as unreachable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.join import CanonicalJoin, JoinColumnPair
from schemabrain.core.metric import Metric, MetricMeasure
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore
from schemabrain.semantic.compiler import (
    AmbiguousJoinError,
    AmbiguousPathError,
    UnknownViaJoinError,
    UnreachableEntityError,
    emit_sql,
    resolve_metric_plan,
)

SOURCE = "src_a"


# ----- fixtures --------------------------------------------------------------


def _id_col(table: str) -> Column:
    return Column(
        name="id",
        table_name=table,
        schema_name="public",
        data_type="bigint",
        nullable=False,
        ordinal_position=1,
        is_primary_key=True,
    )


def _seed_two_hop_chain(store: SQLiteStore) -> None:
    """`order_item → order → user`. Mirrors the live-smoke schema.

    Metric `total_items_sold` is anchored on `order_item`; `user` is
    only reachable through `order`.
    """
    store.write_table(
        Table(name="order_items", schema_name="public", columns=(_id_col("order_items"),)),
        source_connection_id=SOURCE,
    )
    store.write_table(
        Table(name="orders", schema_name="public", columns=(_id_col("orders"),)),
        source_connection_id=SOURCE,
    )
    store.write_table(
        Table(name="users", schema_name="public", columns=(_id_col("users"),)),
        source_connection_id=SOURCE,
    )
    for name, table in (
        ("order_item", "public.order_items"),
        ("order", "public.orders"),
        ("user", "public.users"),
    ):
        store.write_entity(
            Entity(
                name=name,
                description="",
                binding=SingleTableBinding(qualified_table=table),
                identity="id",
            ),
            source_connection_id=SOURCE,
        )
    store.write_canonical_join(
        CanonicalJoin(
            name="order_items_order_id",
            description="",
            source_entity="order_item",
            target_entity="order",
            on=(JoinColumnPair(source_column="order_id", target_column="id"),),
            cardinality="many_to_one",
        ),
        source_connection_id=SOURCE,
    )
    store.write_canonical_join(
        CanonicalJoin(
            name="orders_user_id",
            description="",
            source_entity="order",
            target_entity="user",
            on=(JoinColumnPair(source_column="user_id", target_column="id"),),
            cardinality="many_to_one",
        ),
        source_connection_id=SOURCE,
    )
    store.write_metric(
        Metric(
            name="total_items_sold",
            description="",
            entity="order_item",
            measure=MetricMeasure(agg="sum", column="quantity"),
            time_dimension=None,
            time_grains=(),
        ),
        source_connection_id=SOURCE,
    )


def _seed_diamond_paths(store: SQLiteStore) -> None:
    """Diamond: `order_item → order → (billing_address | shipping_address) → user`.

    Both 3-hop chains reach `user`; without `via=` this is an
    AmbiguousPathError.
    """
    for name in ("order_items", "orders", "addresses", "users"):
        store.write_table(
            Table(name=name, schema_name="public", columns=(_id_col(name),)),
            source_connection_id=SOURCE,
        )
    entity_bindings = (
        ("order_item", "public.order_items"),
        ("order", "public.orders"),
        ("billing_address", "public.addresses"),
        ("shipping_address", "public.addresses"),
        ("user", "public.users"),
    )
    for name, table in entity_bindings:
        store.write_entity(
            Entity(
                name=name,
                description="",
                binding=SingleTableBinding(qualified_table=table),
                identity="id",
            ),
            source_connection_id=SOURCE,
        )
    store.write_canonical_join(
        CanonicalJoin(
            name="order_items_order_id",
            description="",
            source_entity="order_item",
            target_entity="order",
            on=(JoinColumnPair(source_column="order_id", target_column="id"),),
            cardinality="many_to_one",
        ),
        source_connection_id=SOURCE,
    )
    # Two parallel canonical joins from order — one to billing, one to
    # shipping. Then each address links to a user via a separate join
    # name. Total: 5 canonical joins forming a diamond.
    store.write_canonical_join(
        CanonicalJoin(
            name="orders_billing_address",
            description="",
            source_entity="order",
            target_entity="billing_address",
            on=(JoinColumnPair(source_column="billing_address_id", target_column="id"),),
            cardinality="many_to_one",
        ),
        source_connection_id=SOURCE,
    )
    store.write_canonical_join(
        CanonicalJoin(
            name="orders_shipping_address",
            description="",
            source_entity="order",
            target_entity="shipping_address",
            on=(JoinColumnPair(source_column="shipping_address_id", target_column="id"),),
            cardinality="many_to_one",
        ),
        source_connection_id=SOURCE,
    )
    store.write_canonical_join(
        CanonicalJoin(
            name="billing_address_users",
            description="",
            source_entity="billing_address",
            target_entity="user",
            on=(JoinColumnPair(source_column="user_id", target_column="id"),),
            cardinality="many_to_one",
        ),
        source_connection_id=SOURCE,
    )
    store.write_canonical_join(
        CanonicalJoin(
            name="shipping_address_users",
            description="",
            source_entity="shipping_address",
            target_entity="user",
            on=(JoinColumnPair(source_column="user_id", target_column="id"),),
            cardinality="many_to_one",
        ),
        source_connection_id=SOURCE,
    )
    store.write_metric(
        Metric(
            name="total_items_sold",
            description="",
            entity="order_item",
            measure=MetricMeasure(agg="sum", column="quantity"),
            time_dimension=None,
            time_grains=(),
        ),
        source_connection_id=SOURCE,
    )


def _seed_parallel_single_edge(store: SQLiteStore) -> None:
    """`order_item → order → user` with TWO parallel canonical joins
    on the `order → user` hop (e.g. placing user vs. fulfilment user).

    Single-edge ambiguity inside a multi-hop chain — must raise the
    existing AmbiguousJoinError, not AmbiguousPathError, because the
    issue is a single hop with multiple canonical joins, not multiple
    paths.
    """
    for name in ("order_items", "orders", "users"):
        store.write_table(
            Table(name=name, schema_name="public", columns=(_id_col(name),)),
            source_connection_id=SOURCE,
        )
    for name, table in (
        ("order_item", "public.order_items"),
        ("order", "public.orders"),
        ("user", "public.users"),
    ):
        store.write_entity(
            Entity(
                name=name,
                description="",
                binding=SingleTableBinding(qualified_table=table),
                identity="id",
            ),
            source_connection_id=SOURCE,
        )
    store.write_canonical_join(
        CanonicalJoin(
            name="order_items_order_id",
            description="",
            source_entity="order_item",
            target_entity="order",
            on=(JoinColumnPair(source_column="order_id", target_column="id"),),
            cardinality="many_to_one",
        ),
        source_connection_id=SOURCE,
    )
    store.write_canonical_join(
        CanonicalJoin(
            name="orders_placing_user",
            description="",
            source_entity="order",
            target_entity="user",
            on=(JoinColumnPair(source_column="user_id", target_column="id"),),
            cardinality="many_to_one",
        ),
        source_connection_id=SOURCE,
    )
    store.write_canonical_join(
        CanonicalJoin(
            name="orders_fulfilment_user",
            description="",
            source_entity="order",
            target_entity="user",
            on=(JoinColumnPair(source_column="fulfilment_user_id", target_column="id"),),
            cardinality="many_to_one",
        ),
        source_connection_id=SOURCE,
    )
    store.write_metric(
        Metric(
            name="total_items_sold",
            description="",
            entity="order_item",
            measure=MetricMeasure(agg="sum", column="quantity"),
            time_dimension=None,
            time_grains=(),
        ),
        source_connection_id=SOURCE,
    )


# ----- 2-hop happy path ------------------------------------------------------


class TestTwoHopChain:
    def test_two_hop_resolves_with_intermediate_join(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_two_hop_chain(store)
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_items_sold",
                group_by=("user.email",),
            )
        # Two ResolvedJoins, in chain order: anchor → order, order → user.
        assert plan.required_join_names == (
            "order_items_order_id",
            "orders_user_id",
        )
        # First hop: anchor on the left.
        first = plan.joins[0]
        assert first.source_alias == "order_item"
        assert first.target_alias == "order"
        # Second hop: previous hop's alias on the left.
        second = plan.joins[1]
        assert second.source_alias == "order"
        assert second.target_alias == "user"

    def test_two_hop_emits_chained_sql(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_two_hop_chain(store)
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_items_sold",
                group_by=("user.email",),
            )
            sql_text, _params = emit_sql(plan)
        # Order matters: the order_item→order JOIN must precede
        # order→user JOIN, because the second JOIN references "order"
        # on its left side.
        first_join_pos = sql_text.find('JOIN "public"."orders"')
        second_join_pos = sql_text.find('JOIN "public"."users"')
        assert first_join_pos != -1, sql_text
        assert second_join_pos != -1, sql_text
        assert first_join_pos < second_join_pos, sql_text
        # Second JOIN's ON clause references "order" on the left, not
        # the anchor "order_item" — the bug PR-6h.1 fixes.
        on_clause_for_users = sql_text[second_join_pos:]
        assert '"order"."user_id" = "user"."id"' in on_clause_for_users, on_clause_for_users
        # First JOIN's ON clause references the anchor.
        on_clause_for_orders = sql_text[first_join_pos:second_join_pos]
        assert '"order_item"."order_id" = "order"."id"' in on_clause_for_orders, (
            on_clause_for_orders
        )

    def test_shared_intermediate_dedupes(self, tmp_path: Path) -> None:
        """group_by on two columns whose chains share the same
        intermediate (`order`) must produce ONE JOIN to that
        intermediate, not duplicate it."""
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_two_hop_chain(store)
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_items_sold",
                # `user.email` requires the order_item→order→user chain;
                # `order.placed_at` requires just order_item→order.
                # The intermediate `order` JOIN must be shared.
                group_by=("user.email", "order.placed_at"),
            )
        names = plan.required_join_names
        # Exactly 2 distinct canonical joins — order_items_order_id
        # and orders_user_id. NO duplication of order_items_order_id
        # even though both chains start with it.
        assert names.count("order_items_order_id") == 1
        assert names.count("orders_user_id") == 1


# ----- reverse-direction traversal ------------------------------------------


class TestReverseDirectionEmit:
    def test_reverse_traversal_swaps_on_pairs(self, tmp_path: Path) -> None:
        """`order_item → order` is stored as source=order_item,
        target=order, on=(order_id, id). A metric anchored on `order`
        joining to `order_item` traverses the SAME canonical join in
        REVERSE direction — emit must produce
        `order.id = order_item.order_id`, not the literal stored
        column orientation.
        """
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_two_hop_chain(store)
            # Add a metric anchored on `order` (the reverse-side of
            # `order_items_order_id`).
            store.write_metric(
                Metric(
                    name="order_count",
                    description="",
                    entity="order",
                    measure=MetricMeasure(agg="count", column="id"),
                    time_dimension=None,
                    time_grains=(),
                ),
                source_connection_id=SOURCE,
            )
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="order_count",
                group_by=("order_item.product_id",),
            )
            sql_text, _params = emit_sql(plan)
        join = plan.joins[0]
        assert join.source_alias == "order"
        assert join.target_alias == "order_item"
        # on_pairs are swapped relative to the stored direction.
        assert join.on_pairs[0].source_column == "id"  # was "order_id" stored
        assert join.on_pairs[0].target_column == "order_id"  # was "id" stored
        assert '"order"."id" = "order_item"."order_id"' in sql_text


# ----- path-level ambiguity --------------------------------------------------


class TestPathAmbiguity:
    def test_two_equal_paths_raises_ambiguous_path(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_diamond_paths(store)
            with pytest.raises(AmbiguousPathError) as exc_info:
                resolve_metric_plan(
                    store=store,
                    source_connection_id=SOURCE,
                    metric_name="total_items_sold",
                    group_by=("user.email",),
                )
        assert exc_info.value.anchor_entity == "order_item"
        assert exc_info.value.target_entity == "user"
        # Both 3-hop paths surfaced — each starts with
        # `order_items_order_id`, then diverges through the address pair.
        path_names = {tuple(p) for p in exc_info.value.candidate_paths}
        assert (
            "order_items_order_id",
            "orders_billing_address",
            "billing_address_users",
        ) in path_names
        assert (
            "order_items_order_id",
            "orders_shipping_address",
            "shipping_address_users",
        ) in path_names

    def test_via_constrains_path(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_diamond_paths(store)
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_items_sold",
                group_by=("user.email",),
                via=("orders_billing_address",),
            )
        # Exactly the billing path resolved.
        assert plan.required_join_names == (
            "order_items_order_id",
            "orders_billing_address",
            "billing_address_users",
        )

    def test_via_unknown_name_raises(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_diamond_paths(store)
            with pytest.raises(UnknownViaJoinError) as exc_info:
                resolve_metric_plan(
                    store=store,
                    source_connection_id=SOURCE,
                    metric_name="total_items_sold",
                    group_by=("user.email",),
                    via=("nonexistent_join",),
                )
        assert "nonexistent_join" in exc_info.value.requested_via
        assert exc_info.value.anchor_entity == "order_item"

    def test_caller_ambiguous_via_with_multiple_parallel_matches_raises(
        self, tmp_path: Path
    ) -> None:
        """When the parallel-edge branch finds 2+ via names matching
        the candidate joins (e.g. caller passes BOTH `placing_user`
        AND `fulfilment_user` at the same hop), the resolver refuses
        with `AmbiguousJoinError` rather than picking one — caller's
        constraint is itself ambiguous, recovery shape stays
        single-edge-shaped.
        """
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_parallel_single_edge(store)
            with pytest.raises(AmbiguousJoinError) as exc_info:
                resolve_metric_plan(
                    store=store,
                    source_connection_id=SOURCE,
                    metric_name="total_items_sold",
                    group_by=("user.email",),
                    via=("orders_placing_user", "orders_fulfilment_user"),
                )
        # Both names surface as candidates (the caller selected both).
        assert set(exc_info.value.candidate_join_names) == {
            "orders_placing_user",
            "orders_fulfilment_user",
        }


# ----- empty-graph guard ----------------------------------------------------


class TestEmptyJoinGraph:
    def test_unreachable_when_store_has_no_canonical_joins(self, tmp_path: Path) -> None:
        """Both endpoints absent from the join graph (store has zero
        canonical joins) → `UnreachableEntityError` from the early
        guard, not from BFS exhaustion.
        """
        with SQLiteStore(tmp_path / "store.db") as store:
            # Seed the two-hop schema but DELIBERATELY skip the
            # `write_canonical_join` calls so the graph is empty.
            store.write_table(
                Table(
                    name="order_items",
                    schema_name="public",
                    columns=(_id_col("order_items"),),
                ),
                source_connection_id=SOURCE,
            )
            store.write_table(
                Table(name="users", schema_name="public", columns=(_id_col("users"),)),
                source_connection_id=SOURCE,
            )
            for name, table in (
                ("order_item", "public.order_items"),
                ("user", "public.users"),
            ):
                store.write_entity(
                    Entity(
                        name=name,
                        description="",
                        binding=SingleTableBinding(qualified_table=table),
                        identity="id",
                    ),
                    source_connection_id=SOURCE,
                )
            store.write_metric(
                Metric(
                    name="total_items_sold",
                    description="",
                    entity="order_item",
                    measure=MetricMeasure(agg="sum", column="quantity"),
                    time_dimension=None,
                    time_grains=(),
                ),
                source_connection_id=SOURCE,
            )
            with pytest.raises(UnreachableEntityError) as exc_info:
                resolve_metric_plan(
                    store=store,
                    source_connection_id=SOURCE,
                    metric_name="total_items_sold",
                    group_by=("user.email",),
                )
        assert exc_info.value.anchor_entity == "order_item"
        assert exc_info.value.target_entity == "user"


# ----- shared intermediate to different targets -----------------------------


class TestSharedIntermediateToDifferentTargets:
    def test_two_chains_share_intermediate_dedupe_at_iteration(self, tmp_path: Path) -> None:
        """Two group_by columns reaching DIFFERENT final targets but
        through the SAME intermediate (`order_item → order → A` and
        `order_item → order → B`). The second chain's iteration must
        hit the cache-hit branch for the intermediate (`order`) and
        skip re-building its ResolvedJoin, producing one join entry
        for `order`, one for each final target — three total, not
        four.
        """
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_diamond_paths(store)
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_items_sold",
                group_by=("billing_address.country", "shipping_address.country"),
            )
        names = plan.required_join_names
        # `order_items_order_id` must appear exactly once even though
        # both chains transit it.
        assert names.count("order_items_order_id") == 1
        # Both address-side joins resolved, one per chain.
        assert "orders_billing_address" in names
        assert "orders_shipping_address" in names
        # 3 joins total: one shared intermediate + two leaf hops.
        assert len(plan.joins) == 3


# ----- single-edge parallel inside multi-hop --------------------------------


class TestParallelInsideMultiHop:
    def test_parallel_canonical_joins_raise_ambiguous_join(self, tmp_path: Path) -> None:
        """Two canonical joins between `order` and `user` (placing vs.
        fulfilment user) — when the chain reaches `order` and needs
        to step to `user`, the single-edge ambiguity surfaces.
        AmbiguousJoinError, not AmbiguousPathError: the ambiguity is
        at a single hop, not between distinct paths.
        """
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_parallel_single_edge(store)
            with pytest.raises(AmbiguousJoinError) as exc_info:
                resolve_metric_plan(
                    store=store,
                    source_connection_id=SOURCE,
                    metric_name="total_items_sold",
                    group_by=("user.email",),
                )
        assert set(exc_info.value.candidate_join_names) == {
            "orders_placing_user",
            "orders_fulfilment_user",
        }

    def test_via_resolves_parallel_edge(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_parallel_single_edge(store)
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_items_sold",
                group_by=("user.email",),
                via=("orders_placing_user",),
            )
        assert plan.required_join_names == (
            "order_items_order_id",
            "orders_placing_user",
        )


# ----- unreachable -----------------------------------------------------------


class TestUnreachableNoPath:
    def test_disconnected_entity_unreachable(self, tmp_path: Path) -> None:
        """An entity that exists but has no canonical-join edges
        connecting it to the anchor's component must surface
        `UnreachableEntityError` even with BFS — BFS proves no path
        exists, single-hop just couldn't see the edge anyway.
        """
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_two_hop_chain(store)
            store.write_table(
                Table(
                    name="products",
                    schema_name="public",
                    columns=(_id_col("products"),),
                ),
                source_connection_id=SOURCE,
            )
            store.write_entity(
                Entity(
                    name="product",
                    description="",
                    binding=SingleTableBinding(qualified_table="public.products"),
                    identity="id",
                ),
                source_connection_id=SOURCE,
            )
            with pytest.raises(UnreachableEntityError) as exc_info:
                resolve_metric_plan(
                    store=store,
                    source_connection_id=SOURCE,
                    metric_name="total_items_sold",
                    group_by=("product.name",),
                )
        assert exc_info.value.anchor_entity == "order_item"
        assert exc_info.value.target_entity == "product"
