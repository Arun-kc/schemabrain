"""Tests for junction-bridge synthesis (`schemabrain.joins.bridges`).

Bridges turn an M:N junction topology — `products` and `categories`
linked through a `product_categories(product_id, category_id)` table
that is itself an entity — into a logical `products <-> categories
via product_categories` summary the agent can see at `list_joins`
time. Detection reuses `Table.is_junction_table()`; the synthesis
step pairs canonical-join legs and orients column pairs.

Coverage:
  - Junction detection over the entity table list
  - Single bridge from a 2-leg junction
  - Multiple bridges from a 3-leg junction (N*(N-1)/2)
  - Bridge skipped when junction has < 2 legs
  - Worst-of-two inference downgrade
  - Alphabetical end ordering
  - composed_on_pairs orientation
  - synthesize_bridges_for_entity filter
"""

from __future__ import annotations

from pathlib import Path

import pytest

from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.join import CanonicalJoin, JoinColumnPair
from schemabrain.core.models import Column, ForeignKey, Table
from schemabrain.core.store import SQLiteStore
from schemabrain.joins.bridges import (
    BridgeJoin,
    composed_on_pairs,
    find_junction_entities,
    synthesize_bridges,
    synthesize_bridges_for_entity,
)

# ----- helpers -----------------------------------------------------------------


def _column(
    name: str,
    *,
    table: str,
    schema: str = "public",
    data_type: str = "bigint",
    ord_: int = 1,
    pk: bool = False,
    nullable: bool = False,
) -> Column:
    return Column(
        name=name,
        table_name=table,
        schema_name=schema,
        data_type=data_type,
        nullable=nullable,
        ordinal_position=ord_,
        is_primary_key=pk,
    )


def _products_table() -> Table:
    return Table(
        name="products",
        schema_name="public",
        columns=(_column("id", table="products", pk=True),),
    )


def _categories_table() -> Table:
    return Table(
        name="categories",
        schema_name="public",
        columns=(_column("id", table="categories", pk=True),),
    )


def _tags_table() -> Table:
    return Table(
        name="tags",
        schema_name="public",
        columns=(_column("id", table="tags", pk=True),),
    )


def _product_categories_table() -> Table:
    """Junction table — composite PK whose columns are both FK sources."""
    return Table(
        name="product_categories",
        schema_name="public",
        columns=(
            _column("product_id", table="product_categories", ord_=1, pk=True),
            _column("category_id", table="product_categories", ord_=2, pk=True),
        ),
        foreign_keys=(
            ForeignKey(
                name="product_categories_product_fkey",
                source_columns=("product_id",),
                target_schema="public",
                target_table="products",
                target_columns=("id",),
            ),
            ForeignKey(
                name="product_categories_category_fkey",
                source_columns=("category_id",),
                target_schema="public",
                target_table="categories",
                target_columns=("id",),
            ),
        ),
    )


def _three_way_junction_table() -> Table:
    """Junction connecting products, categories, AND tags (3-way)."""
    return Table(
        name="product_facets",
        schema_name="public",
        columns=(
            _column("product_id", table="product_facets", ord_=1, pk=True),
            _column("category_id", table="product_facets", ord_=2, pk=True),
            _column("tag_id", table="product_facets", ord_=3, pk=True),
        ),
        foreign_keys=(
            ForeignKey(
                name="pf_product_fkey",
                source_columns=("product_id",),
                target_schema="public",
                target_table="products",
                target_columns=("id",),
            ),
            ForeignKey(
                name="pf_category_fkey",
                source_columns=("category_id",),
                target_schema="public",
                target_table="categories",
                target_columns=("id",),
            ),
            ForeignKey(
                name="pf_tag_fkey",
                source_columns=("tag_id",),
                target_schema="public",
                target_table="tags",
                target_columns=("id",),
            ),
        ),
    )


def _entity(name: str, *, table: str) -> Entity:
    return Entity(
        name=name,
        description="",
        binding=SingleTableBinding(qualified_table=f"public.{table}"),
        identity="id" if name != "product_categories" else "product_id",
        origin="manual",
    )


def _seed_two_way_junction(tmp_path: Path) -> SQLiteStore:
    """Store with products + categories + product_categories junction,
    plus the two canonical-join legs.
    """
    store = SQLiteStore(tmp_path / "s.db")
    store.write_table(_products_table(), source_connection_id="sid")
    store.write_table(_categories_table(), source_connection_id="sid")
    store.write_table(_product_categories_table(), source_connection_id="sid")
    store.write_entity(_entity("product", table="products"), source_connection_id="sid")
    store.write_entity(_entity("category", table="categories"), source_connection_id="sid")
    # Junction entity binds to the pivot table.
    store.write_entity(
        Entity(
            name="product_categories",
            description="",
            binding=SingleTableBinding(qualified_table="public.product_categories"),
            identity="product_id",
            origin="suggested",
            inference_method="fk_constraint",
            validation_state="applied",
        ),
        source_connection_id="sid",
    )
    # Two FK-derived canonical joins out of the junction.
    store.write_canonical_join(
        CanonicalJoin(
            name="product_categories_product",
            description="",
            source_entity="product_categories",
            target_entity="product",
            on=(JoinColumnPair(source_column="product_id", target_column="id"),),
            origin="suggested",
            cardinality="many_to_one",
            inference_method="fk_constraint",
            validation_state="applied",
        ),
        source_connection_id="sid",
    )
    store.write_canonical_join(
        CanonicalJoin(
            name="product_categories_category",
            description="",
            source_entity="product_categories",
            target_entity="category",
            on=(JoinColumnPair(source_column="category_id", target_column="id"),),
            origin="suggested",
            cardinality="many_to_one",
            inference_method="fk_constraint",
            validation_state="applied",
        ),
        source_connection_id="sid",
    )
    return store


# ----- junction detection -----------------------------------------------------


class TestFindJunctionEntities:
    def test_no_junctions_returns_empty(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "s.db")
        store.write_table(_products_table(), source_connection_id="sid")
        store.write_entity(_entity("product", table="products"), source_connection_id="sid")
        with store:
            result = find_junction_entities(store=store, source_connection_id="sid")
        assert result == []

    def test_detects_two_way_junction(self, tmp_path: Path) -> None:
        store = _seed_two_way_junction(tmp_path)
        with store:
            result = find_junction_entities(store=store, source_connection_id="sid")
        assert [e.name for e in result] == ["product_categories"]

    def test_non_junction_entities_excluded(self, tmp_path: Path) -> None:
        store = _seed_two_way_junction(tmp_path)
        with store:
            junctions = find_junction_entities(store=store, source_connection_id="sid")
        # Sanity: products + categories must NOT be reported as junctions.
        names = {e.name for e in junctions}
        assert "product" not in names
        assert "category" not in names


# ----- single-bridge synthesis -------------------------------------------------


class TestSynthesizeBridges:
    def test_two_way_junction_yields_one_bridge(self, tmp_path: Path) -> None:
        store = _seed_two_way_junction(tmp_path)
        with store:
            bridges = synthesize_bridges(store=store, source_connection_id="sid")
        assert len(bridges) == 1
        b = bridges[0]
        assert b.via_junction == "product_categories"
        # Endpoints alpha-ordered.
        assert b.source_entity == "category"
        assert b.target_entity == "product"
        assert b.name == "category_product_via_product_categories"
        # Both legs are FK-derived → bridge inherits fk_constraint.
        assert b.inference_method == "fk_constraint"
        assert b.validation_state == "applied"

    def test_three_way_junction_yields_three_bridges(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "s.db")
        store.write_table(_products_table(), source_connection_id="sid")
        store.write_table(_categories_table(), source_connection_id="sid")
        store.write_table(_tags_table(), source_connection_id="sid")
        store.write_table(_three_way_junction_table(), source_connection_id="sid")
        store.write_entity(_entity("product", table="products"), source_connection_id="sid")
        store.write_entity(_entity("category", table="categories"), source_connection_id="sid")
        store.write_entity(_entity("tag", table="tags"), source_connection_id="sid")
        store.write_entity(
            Entity(
                name="product_facets",
                description="",
                binding=SingleTableBinding(qualified_table="public.product_facets"),
                identity="product_id",
                origin="suggested",
                inference_method="fk_constraint",
                validation_state="applied",
            ),
            source_connection_id="sid",
        )
        for target, target_col in (
            ("product", "product_id"),
            ("category", "category_id"),
            ("tag", "tag_id"),
        ):
            store.write_canonical_join(
                CanonicalJoin(
                    name=f"product_facets_{target}",
                    description="",
                    source_entity="product_facets",
                    target_entity=target,
                    on=(JoinColumnPair(source_column=target_col, target_column="id"),),
                    origin="suggested",
                    cardinality="many_to_one",
                    inference_method="fk_constraint",
                    validation_state="applied",
                ),
                source_connection_id="sid",
            )
        with store:
            bridges = synthesize_bridges(store=store, source_connection_id="sid")
        # 3 endpoints → C(3,2) = 3 bridges.
        assert len(bridges) == 3
        pairs = {(b.source_entity, b.target_entity) for b in bridges}
        assert pairs == {
            ("category", "product"),
            ("category", "tag"),
            ("product", "tag"),
        }

    def test_junction_with_one_leg_yields_no_bridges(self, tmp_path: Path) -> None:
        """A junction-shaped table with only one stored canonical join
        leg can't bridge anything — the synthesiser skips it cleanly.
        """
        store = _seed_two_way_junction(tmp_path)
        # Remove one of the two legs to simulate a half-defined junction.
        with store:
            # Use a direct execute to avoid public API for surgical setup.
            conn = store._require_conn()
            conn.execute(
                "DELETE FROM canonical_joins WHERE name = ?",
                ("product_categories_category",),
            )
            conn.commit()
            bridges = synthesize_bridges(store=store, source_connection_id="sid")
        assert bridges == []

    def test_worst_inference_downgrades_to_llm_suggested(self, tmp_path: Path) -> None:
        """If one leg is `llm_suggested`, the bridge inherits that — the
        agent must not trust the bridge more than its weakest link.
        """
        store = _seed_two_way_junction(tmp_path)
        with store:
            # Overwrite one leg to be LLM-suggested.
            store.write_canonical_join(
                CanonicalJoin(
                    name="product_categories_product",
                    description="",
                    source_entity="product_categories",
                    target_entity="product",
                    on=(JoinColumnPair(source_column="product_id", target_column="id"),),
                    origin="suggested",
                    cardinality="many_to_one",
                    inference_method="llm_suggested",
                    validation_state="applied",
                ),
                source_connection_id="sid",
            )
            bridges = synthesize_bridges(store=store, source_connection_id="sid")
        assert len(bridges) == 1
        assert bridges[0].inference_method == "llm_suggested"

    def test_empty_store_returns_empty(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "s.db")
        with store:
            bridges = synthesize_bridges(store=store, source_connection_id="sid")
        assert bridges == []


# ----- per-entity filter -------------------------------------------------------


class TestSynthesizeBridgesForEntity:
    def test_returns_only_bridges_touching_entity(self, tmp_path: Path) -> None:
        store = _seed_two_way_junction(tmp_path)
        with store:
            bridges = synthesize_bridges_for_entity(
                store=store, source_connection_id="sid", entity_name="product"
            )
        assert len(bridges) == 1
        assert "product" in (bridges[0].source_entity, bridges[0].target_entity)

    def test_entity_not_in_any_bridge_returns_empty(self, tmp_path: Path) -> None:
        store = _seed_two_way_junction(tmp_path)
        with store:
            bridges = synthesize_bridges_for_entity(
                store=store,
                source_connection_id="sid",
                entity_name="unrelated_entity",
            )
        assert bridges == []

    def test_excludes_junction_self(self, tmp_path: Path) -> None:
        """Calling for the junction entity itself never surfaces a
        self-bridge — that would be a degenerate case.
        """
        store = _seed_two_way_junction(tmp_path)
        with store:
            bridges = synthesize_bridges_for_entity(
                store=store,
                source_connection_id="sid",
                entity_name="product_categories",
            )
        # The junction is the via, not an endpoint, so per-endpoint
        # filter returns nothing.
        assert bridges == []


# ----- composed_on_pairs orientation ------------------------------------------


class TestComposedOnPairs:
    def test_pairs_oriented_low_to_junction_to_high(self, tmp_path: Path) -> None:
        store = _seed_two_way_junction(tmp_path)
        with store:
            bridges = synthesize_bridges(store=store, source_connection_id="sid")
            assert len(bridges) == 1
            bridge = bridges[0]
            leg_lo = store.get_canonical_join(bridge.via_joins[0], source_connection_id="sid")
            leg_hi = store.get_canonical_join(bridge.via_joins[1], source_connection_id="sid")
            assert leg_lo is not None
            assert leg_hi is not None
            a_to_j, j_to_b = composed_on_pairs(bridge=bridge, leg_lo=leg_lo, leg_hi=leg_hi)
        # First leg connects bridge.source_entity → via_junction.
        # Stored shape is `source=product_categories, target=category`
        # so the orienter flips it to read `category.id =
        # product_categories.category_id`.
        assert a_to_j == (JoinColumnPair(source_column="id", target_column="category_id"),)
        # Second leg connects via_junction → bridge.target_entity.
        # Stored as `source=product_categories, target=product`, no flip
        # needed; reads `product_categories.product_id = product.id`.
        assert j_to_b == (JoinColumnPair(source_column="product_id", target_column="id"),)

    def test_raises_when_leg_does_not_connect_ends(self, tmp_path: Path) -> None:
        store = _seed_two_way_junction(tmp_path)
        with store:
            bridges = synthesize_bridges(store=store, source_connection_id="sid")
            bridge = bridges[0]
            # Construct a fake leg that doesn't touch the bridge ends.
            wrong_leg = CanonicalJoin(
                name="wrong",
                description="",
                source_entity="unrelated_a",
                target_entity="unrelated_b",
                on=(JoinColumnPair(source_column="x", target_column="y"),),
                origin="manual",
            )
            real_leg = store.get_canonical_join(bridge.via_joins[1], source_connection_id="sid")
            assert real_leg is not None
            with pytest.raises(ValueError, match="does not connect"):
                composed_on_pairs(bridge=bridge, leg_lo=wrong_leg, leg_hi=real_leg)


# ----- BridgeJoin equality / hash ---------------------------------------------


class TestBridgeJoinDataclass:
    def test_frozen_and_hashable(self) -> None:
        b1 = BridgeJoin(
            name="a_b_via_j",
            source_entity="a",
            target_entity="b",
            via_junction="j",
            via_joins=("j_a", "j_b"),
            inference_method="fk_constraint",
            validation_state="applied",
        )
        b2 = BridgeJoin(
            name="a_b_via_j",
            source_entity="a",
            target_entity="b",
            via_junction="j",
            via_joins=("j_a", "j_b"),
            inference_method="fk_constraint",
            validation_state="applied",
        )
        assert b1 == b2
        assert hash(b1) == hash(b2)
