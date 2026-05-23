"""Tests for `schemabrain.inspect.engine` — store-only schema browser.

Goals:

  1. `build_summary` returns deterministic counts (tables / columns /
     entities / metrics / joins) and alphabetically-ordered name
     lists across the store. Filtering by `source_connection_id`
     scopes counts to that source.
  2. `build_entity_detail` returns full detail for a known entity:
     entity dataclass + every column on the bound table (with PII
     tags) + related entities (via canonical joins, both directions)
     + anchored metrics. Returns `None` for a missing name.
  3. Empty store is a valid input: `build_summary` returns zero
     counts; `build_entity_detail` returns `None`.
  4. The `is_identity` flag on `EntityColumnDetail` is True iff the
     column matches `Entity.identity`.
  5. Related-entities traversal is bidirectional: a join with
     `source=customer, target=order` surfaces `order` when drilling
     into `customer` AND `customer` when drilling into `order`.

The engine is store-only — no live DataSource needed. Tests use a
populated `SQLiteStore` directly.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.join import CanonicalJoin, JoinColumnPair
from schemabrain.core.metric import Metric, MetricMeasure
from schemabrain.core.models import Column, ForeignKey, Table
from schemabrain.core.store import SQLiteStore
from schemabrain.inspect.engine import (
    RelatedEntity,
    StoreSummary,
    build_entity_detail,
    build_summary,
)

SOURCE_ID = "src_test_inspect"


# ----- helpers --------------------------------------------------------------


def _make_column(
    name: str,
    *,
    table_name: str,
    schema_name: str = "public",
    data_type: str = "text",
    nullable: bool = True,
    pk: bool = False,
    pos: int = 1,
) -> Column:
    return Column(
        name=name,
        table_name=table_name,
        schema_name=schema_name,
        data_type=data_type,
        nullable=nullable,
        ordinal_position=pos,
        is_primary_key=pk,
    )


def _seed_customers_table(store: SQLiteStore) -> None:
    store.write_table(
        Table(
            schema_name="public",
            name="customers",
            columns=(
                _make_column(
                    "id", table_name="customers", data_type="bigint", nullable=False, pk=True, pos=1
                ),
                _make_column("email", table_name="customers", nullable=False, pos=2),
                _make_column("full_name", table_name="customers", nullable=False, pos=3),
                _make_column(
                    "created_at",
                    table_name="customers",
                    data_type="timestamptz",
                    nullable=False,
                    pos=4,
                ),
            ),
        ),
        source_connection_id=SOURCE_ID,
    )


def _seed_orders_table(store: SQLiteStore) -> None:
    store.write_table(
        Table(
            schema_name="public",
            name="orders",
            columns=(
                _make_column(
                    "id", table_name="orders", data_type="bigint", nullable=False, pk=True, pos=1
                ),
                _make_column(
                    "customer_id", table_name="orders", data_type="bigint", nullable=False, pos=2
                ),
                _make_column(
                    "total_cents", table_name="orders", data_type="int", nullable=False, pos=3
                ),
                _make_column(
                    "placed_at", table_name="orders", data_type="timestamptz", nullable=False, pos=4
                ),
            ),
        ),
        source_connection_id=SOURCE_ID,
    )


def _customer_entity() -> Entity:
    return Entity(
        name="customer",
        description="A registered user who can place orders.",
        binding=SingleTableBinding(qualified_table="public.customers"),
        identity="id",
    )


def _order_entity() -> Entity:
    return Entity(
        name="order",
        description="A purchase placed by one customer.",
        binding=SingleTableBinding(qualified_table="public.orders"),
        identity="id",
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SQLiteStore]:
    store = SQLiteStore(path=tmp_path / "store.db")
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def populated_store(store: SQLiteStore) -> SQLiteStore:
    """A store with 2 tables, 2 entities, 1 metric, 1 join."""
    _seed_customers_table(store)
    _seed_orders_table(store)
    store.write_entity(_customer_entity(), source_connection_id=SOURCE_ID)
    store.write_entity(_order_entity(), source_connection_id=SOURCE_ID)
    store.write_metric(
        Metric(
            name="total_revenue",
            description="Sum of order subtotals.",
            entity="order",
            measure=MetricMeasure(agg="sum", column="total_cents"),
            time_dimension="order.placed_at",
            time_grains=("day", "month"),
        ),
        source_connection_id=SOURCE_ID,
    )
    store.write_canonical_join(
        CanonicalJoin(
            name="customer_orders",
            description="Each order is placed by one customer.",
            source_entity="customer",
            target_entity="order",
            on=(JoinColumnPair(source_column="id", target_column="customer_id"),),
            cardinality="one_to_many",
        ),
        source_connection_id=SOURCE_ID,
    )
    return store


# ----- StoreSummary ---------------------------------------------------------


class TestStoreSummary:
    def test_empty_store_summary(self, store: SQLiteStore) -> None:
        summary = build_summary(store=store, source_connection_id=None)
        assert summary.table_count == 0
        # Cross-source sentinel (None) — empty store, but we still
        # take the no-source-filter path.
        assert summary.column_count is None
        assert summary.entity_count == 0
        assert summary.metric_count == 0
        assert summary.join_count == 0
        assert summary.entity_names == ()
        assert summary.metric_names == ()
        assert summary.join_names == ()

    def test_populated_store_summary(self, populated_store: SQLiteStore) -> None:
        summary = build_summary(store=populated_store, source_connection_id=SOURCE_ID)
        assert summary.table_count == 2
        assert summary.column_count == 8  # 4 cols per table
        assert summary.entity_count == 2
        assert summary.metric_count == 1
        assert summary.join_count == 1
        # Alphabetical ordering — deterministic across runs.
        assert summary.entity_names == ("customer", "order")
        assert summary.metric_names == ("total_revenue",)
        assert summary.join_names == ("customer_orders",)

    def test_summary_is_frozen(self) -> None:
        summary = StoreSummary(
            table_count=0,
            column_count=0,
            entity_count=0,
            metric_count=0,
            join_count=0,
            entity_names=(),
            metric_names=(),
            join_names=(),
        )
        with pytest.raises((AttributeError, TypeError)):
            summary.table_count = 1  # type: ignore[misc]


# ----- multi-source dedup (smoke 2026-05-19 Bug 4) -------------------------


class TestStoreSummaryMultiSource:
    """Smoke 2026-05-19: an operator's pre-existing store carried 6
    entities under an older source-id scheme; a fresh `init` wrote 6
    NEW entities under the current source-id. `inspect` rendered all
    12 names with no signal the same logical entities appeared twice.

    These tests pin the new behavior:
      1. Cross-source build (filter is None) dedupes entity / metric /
         join names by name so the tree renders each once.
      2. Cross-source counts mirror the deduped name counts (NOT the
         raw row counts).
      3. `source_connection_ids` lists every source that has data so
         the renderer can compose its multi-source banner.
      4. Scoped build (filter is a real source-id) preserves the
         original v0 semantics — raw row counts equal distinct-name
         counts under the `(source_connection_id, name)` PK.
    """

    @pytest.fixture
    def _two_source_store(self, store: SQLiteStore) -> SQLiteStore:
        # Seed two sources with overlapping entity / metric / join
        # names. Mirrors the smoke-bug shape: pre-existing rows from
        # an older `_canonical_url` scheme + fresh rows under the
        # current scheme.
        for sid in ("src_old_scheme_abc123", "src_new_scheme_def456"):
            # Tables differ per source (two distinct schemas), so
            # table-name dedup is exercised when the qualified names
            # collide AND when they don't.
            store.write_table(
                Table(
                    schema_name="public",
                    name="users",
                    columns=(
                        _make_column(
                            "id",
                            table_name="users",
                            data_type="bigint",
                            nullable=False,
                            pk=True,
                            pos=1,
                        ),
                    ),
                ),
                source_connection_id=sid,
            )
            store.write_entity(
                Entity(
                    name="user",
                    description=f"User from {sid}",
                    binding=SingleTableBinding(qualified_table="public.users"),
                    identity="id",
                    origin="manual",
                ),
                source_connection_id=sid,
            )
            store.write_entity(
                Entity(
                    name="order",
                    description=f"Order from {sid}",
                    binding=SingleTableBinding(qualified_table="public.users"),
                    identity="id",
                    origin="manual",
                ),
                source_connection_id=sid,
            )
            store.write_metric(
                Metric(
                    name="total_revenue",
                    description="Sum of revenue",
                    entity="order",
                    measure=MetricMeasure(agg="sum", column="id"),
                    time_dimension=None,
                    time_grains=(),
                ),
                source_connection_id=sid,
            )
        return store

    def test_cross_source_dedupes_entity_names(self, _two_source_store: SQLiteStore) -> None:
        summary = build_summary(store=_two_source_store, source_connection_id=None)
        # Two entities per source, each with the same names —
        # cross-source view collapses to the distinct name set.
        assert summary.entity_names == ("order", "user")
        assert summary.entity_count == 2

    def test_cross_source_dedupes_metric_names(self, _two_source_store: SQLiteStore) -> None:
        summary = build_summary(store=_two_source_store, source_connection_id=None)
        assert summary.metric_names == ("total_revenue",)
        assert summary.metric_count == 1

    def test_cross_source_dedupes_table_count(self, _two_source_store: SQLiteStore) -> None:
        summary = build_summary(store=_two_source_store, source_connection_id=None)
        # Both sources have public.users → one qualified name.
        assert summary.table_count == 1

    def test_cross_source_lists_every_source_id(self, _two_source_store: SQLiteStore) -> None:
        summary = build_summary(store=_two_source_store, source_connection_id=None)
        # Sorted ascending — matches the SQL ORDER BY in
        # `list_distinct_source_connection_ids`.
        assert summary.source_connection_ids == (
            "src_new_scheme_def456",
            "src_old_scheme_abc123",
        )

    def test_scoped_build_lists_single_source_id(self, _two_source_store: SQLiteStore) -> None:
        # Scoping to one source surfaces only that source-id in the
        # tuple — the renderer's multi-source banner is gated on
        # `len > 1`, so this branch suppresses the banner naturally.
        summary = build_summary(
            store=_two_source_store, source_connection_id="src_new_scheme_def456"
        )
        assert summary.source_connection_ids == ("src_new_scheme_def456",)
        # Raw row counts under PK uniqueness.
        assert summary.entity_count == 2
        assert summary.entity_names == ("order", "user")

    def test_empty_store_has_empty_source_id_tuple(self, store: SQLiteStore) -> None:
        # No rows in any of the 4 union'd tables → empty source-id
        # list. Renderer's `len > 1` branch is skipped.
        summary = build_summary(store=store, source_connection_id=None)
        assert summary.source_connection_ids == ()


# ----- EntityDetail ---------------------------------------------------------


class TestEntityDetail:
    def test_unknown_entity_returns_none(self, populated_store: SQLiteStore) -> None:
        detail = build_entity_detail(
            store=populated_store,
            entity_name="nonexistent",
            source_connection_id=SOURCE_ID,
        )
        assert detail is None

    def test_entity_detail_carries_full_columns(self, populated_store: SQLiteStore) -> None:
        detail = build_entity_detail(
            store=populated_store,
            entity_name="customer",
            source_connection_id=SOURCE_ID,
        )
        assert detail is not None
        assert detail.entity.name == "customer"
        assert detail.entity.identity == "id"
        # Columns surface in ordinal-position order, matching the
        # physical table layout the user would see in psql.
        assert tuple(c.name for c in detail.columns) == (
            "id",
            "email",
            "full_name",
            "created_at",
        )
        # `is_identity` marks the entity's anchor column.
        identity_col = next(c for c in detail.columns if c.is_identity)
        assert identity_col.name == "id"
        # No PII tags written for this fixture → every column is "public".
        for col in detail.columns:
            assert col.pii_sensitivity == "public"
            assert col.pii_categories == ()

    def test_entity_detail_surfaces_anchored_metric(self, populated_store: SQLiteStore) -> None:
        detail = build_entity_detail(
            store=populated_store,
            entity_name="order",
            source_connection_id=SOURCE_ID,
        )
        assert detail is not None
        assert len(detail.anchored_metrics) == 1
        metric = detail.anchored_metrics[0]
        assert metric.name == "total_revenue"
        assert metric.agg == "sum"
        assert metric.column == "total_cents"
        assert metric.time_dimension == "order.placed_at"
        assert metric.time_grains == ("day", "month")

    def test_entity_detail_anchored_metrics_empty_for_customer(
        self, populated_store: SQLiteStore
    ) -> None:
        detail = build_entity_detail(
            store=populated_store,
            entity_name="customer",
            source_connection_id=SOURCE_ID,
        )
        assert detail is not None
        # `total_revenue` is anchored on `order`, not `customer`.
        assert detail.anchored_metrics == ()


class TestRelatedEntities:
    def test_outgoing_join_surfaces_target_when_drilling_source(
        self, populated_store: SQLiteStore
    ) -> None:
        detail = build_entity_detail(
            store=populated_store,
            entity_name="customer",
            source_connection_id=SOURCE_ID,
        )
        assert detail is not None
        assert len(detail.related_entities) == 1
        rel = detail.related_entities[0]
        assert rel.name == "order"
        assert rel.direction == "outgoing"
        assert rel.join_name == "customer_orders"
        assert rel.on == (("id", "customer_id"),)
        assert rel.cardinality == "one_to_many"

    def test_incoming_join_surfaces_source_when_drilling_target(
        self, populated_store: SQLiteStore
    ) -> None:
        detail = build_entity_detail(
            store=populated_store,
            entity_name="order",
            source_connection_id=SOURCE_ID,
        )
        assert detail is not None
        assert len(detail.related_entities) == 1
        rel = detail.related_entities[0]
        assert rel.name == "customer"
        assert rel.direction == "incoming"
        assert rel.join_name == "customer_orders"
        # `on` reads as source.first = target.second from the
        # *drilled* entity's perspective: drilling into `order`,
        # `customer_id` is the local column and `id` is the remote.
        assert rel.on == (("customer_id", "id"),)


class TestEdgeCases:
    def test_summary_with_no_source_filter_skips_column_count(
        self, populated_store: SQLiteStore
    ) -> None:
        # Without a source-id filter, column counts are deliberately
        # skipped (returns 0) rather than confusing cross-source
        # totals. The remaining counts still aggregate cleanly.
        summary = build_summary(store=populated_store, source_connection_id=None)
        assert summary.table_count == 2  # tables list IS cross-source
        # Cross-source sentinel — engine returns None rather than a
        # confusing 0 ("0 columns across 7 tables" would be wrong).
        assert summary.column_count is None
        assert summary.entity_count == 2

    def test_drill_when_bound_table_not_indexed_returns_empty_columns(
        self, store: SQLiteStore
    ) -> None:
        # An entity row exists but the bound table is missing from
        # the `tables` index. `build_entity_detail` still returns a
        # detail object (the entity IS in the store) but with empty
        # columns; the renderer's "bound table not indexed" hint
        # then surfaces to the operator.
        _seed_customers_table(store)
        store.write_entity(_customer_entity(), source_connection_id=SOURCE_ID)
        # Drop the table row via the live store connection with FKs
        # off — write_entity has already passed the FK check, and we
        # want to simulate the post-write drift scenario where the
        # tables row vanishes (manual cleanup, mid-migration, etc.)
        conn = store._require_conn()
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.execute("DELETE FROM tables WHERE name = 'customers' AND schema_name = 'public'")
            conn.commit()
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
        detail = build_entity_detail(
            store=store,
            entity_name="customer",
            source_connection_id=SOURCE_ID,
        )
        assert detail is not None
        assert detail.columns == ()

    def test_unrelated_join_does_not_appear_in_related_entities(
        self, populated_store: SQLiteStore
    ) -> None:
        # Add a third entity + a join that does NOT touch `customer`.
        # The customer drill should NOT surface that unrelated join.
        populated_store.write_table(
            Table(
                schema_name="public",
                name="products",
                columns=(
                    _make_column(
                        "id",
                        table_name="products",
                        data_type="bigint",
                        nullable=False,
                        pk=True,
                        pos=1,
                    ),
                    _make_column("category_id", table_name="products", data_type="bigint", pos=2),
                ),
            ),
            source_connection_id=SOURCE_ID,
        )
        populated_store.write_table(
            Table(
                schema_name="public",
                name="categories",
                columns=(
                    _make_column(
                        "id",
                        table_name="categories",
                        data_type="bigint",
                        nullable=False,
                        pk=True,
                        pos=1,
                    ),
                ),
            ),
            source_connection_id=SOURCE_ID,
        )
        populated_store.write_entity(
            Entity(
                name="product",
                description="",
                binding=SingleTableBinding(qualified_table="public.products"),
                identity="id",
            ),
            source_connection_id=SOURCE_ID,
        )
        populated_store.write_entity(
            Entity(
                name="category",
                description="",
                binding=SingleTableBinding(qualified_table="public.categories"),
                identity="id",
            ),
            source_connection_id=SOURCE_ID,
        )
        populated_store.write_canonical_join(
            CanonicalJoin(
                name="product_categories",
                description="",
                source_entity="product",
                target_entity="category",
                on=(JoinColumnPair(source_column="category_id", target_column="id"),),
            ),
            source_connection_id=SOURCE_ID,
        )
        # Drilling customer should still surface only the
        # customer_orders join — NOT product_categories.
        detail = build_entity_detail(
            store=populated_store,
            entity_name="customer",
            source_connection_id=SOURCE_ID,
        )
        assert detail is not None
        assert len(detail.related_entities) == 1
        assert detail.related_entities[0].join_name == "customer_orders"


class TestDataclassValidation:
    def test_related_entity_rejects_invalid_direction(self) -> None:
        # Direct construction of `RelatedEntity` with a bogus
        # direction must fail at __post_init__ — protects renderers
        # from silently rendering "sideways" or empty-string values.
        with pytest.raises(ValueError, match="direction must be one of"):
            RelatedEntity(
                name="order",
                direction="sideways",  # type: ignore[arg-type]
                join_name="customer_orders",
                on=(("id", "user_id"),),
                cardinality="one_to_many",
            )


class TestRelatedEntityBridges:
    """Drilling into an entity that participates in an M:N junction
    must surface the bridge edges alongside its direct canonical joins.
    The bridge edge reports `via_junction` so the renderer can mark it
    as a mediated relationship rather than a direct edge.
    """

    def _seed_with_junction(self, store: SQLiteStore) -> None:
        # Products + categories + junction product_categories.
        store.write_table(
            Table(
                schema_name="public",
                name="products",
                columns=(
                    _make_column(
                        "id",
                        table_name="products",
                        data_type="bigint",
                        nullable=False,
                        pk=True,
                        pos=1,
                    ),
                ),
            ),
            source_connection_id=SOURCE_ID,
        )
        store.write_table(
            Table(
                schema_name="public",
                name="categories",
                columns=(
                    _make_column(
                        "id",
                        table_name="categories",
                        data_type="bigint",
                        nullable=False,
                        pk=True,
                        pos=1,
                    ),
                ),
            ),
            source_connection_id=SOURCE_ID,
        )
        store.write_table(
            Table(
                schema_name="public",
                name="product_categories",
                columns=(
                    _make_column(
                        "product_id",
                        table_name="product_categories",
                        data_type="bigint",
                        nullable=False,
                        pk=True,
                        pos=1,
                    ),
                    _make_column(
                        "category_id",
                        table_name="product_categories",
                        data_type="bigint",
                        nullable=False,
                        pk=True,
                        pos=2,
                    ),
                ),
                foreign_keys=(
                    ForeignKey(
                        name="pc_product_fkey",
                        source_columns=("product_id",),
                        target_schema="public",
                        target_table="products",
                        target_columns=("id",),
                    ),
                    ForeignKey(
                        name="pc_category_fkey",
                        source_columns=("category_id",),
                        target_schema="public",
                        target_table="categories",
                        target_columns=("id",),
                    ),
                ),
            ),
            source_connection_id=SOURCE_ID,
        )
        store.write_entity(
            Entity(
                name="product",
                description="",
                binding=SingleTableBinding(qualified_table="public.products"),
                identity="id",
            ),
            source_connection_id=SOURCE_ID,
        )
        store.write_entity(
            Entity(
                name="category",
                description="",
                binding=SingleTableBinding(qualified_table="public.categories"),
                identity="id",
            ),
            source_connection_id=SOURCE_ID,
        )
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
            source_connection_id=SOURCE_ID,
        )
        store.write_canonical_join(
            CanonicalJoin(
                name="pc_product",
                description="",
                source_entity="product_categories",
                target_entity="product",
                on=(JoinColumnPair(source_column="product_id", target_column="id"),),
                origin="suggested",
                cardinality="many_to_one",
                inference_method="fk_constraint",
                validation_state="applied",
            ),
            source_connection_id=SOURCE_ID,
        )
        store.write_canonical_join(
            CanonicalJoin(
                name="pc_category",
                description="",
                source_entity="product_categories",
                target_entity="category",
                on=(JoinColumnPair(source_column="category_id", target_column="id"),),
                origin="suggested",
                cardinality="many_to_one",
                inference_method="fk_constraint",
                validation_state="applied",
            ),
            source_connection_id=SOURCE_ID,
        )

    def test_drilling_product_surfaces_bridge_to_category(self, store: SQLiteStore) -> None:
        self._seed_with_junction(store)
        detail = build_entity_detail(
            store=store,
            entity_name="product",
            source_connection_id=SOURCE_ID,
        )
        assert detail is not None
        bridge_edges = [r for r in detail.related_entities if r.via_junction is not None]
        assert len(bridge_edges) == 1
        bridge = bridge_edges[0]
        assert bridge.name == "category"
        assert bridge.via_junction == "product_categories"
        assert bridge.cardinality == "many_to_many"
        # First leg from product's perspective: product.id =
        # product_categories.product_id. Engine orients the pair so
        # the local column comes first.
        assert bridge.on == (("id", "product_id"),)

    def test_drilling_category_surfaces_bridge_to_product(self, store: SQLiteStore) -> None:
        self._seed_with_junction(store)
        detail = build_entity_detail(
            store=store,
            entity_name="category",
            source_connection_id=SOURCE_ID,
        )
        assert detail is not None
        bridge_edges = [r for r in detail.related_entities if r.via_junction is not None]
        assert len(bridge_edges) == 1
        assert bridge_edges[0].name == "product"
        assert bridge_edges[0].via_junction == "product_categories"

    def test_drilling_junction_itself_only_shows_direct_legs(self, store: SQLiteStore) -> None:
        """The junction entity itself has only direct canonical-join
        edges — no self-bridge, no fabricated relationship to itself.
        """
        self._seed_with_junction(store)
        detail = build_entity_detail(
            store=store,
            entity_name="product_categories",
            source_connection_id=SOURCE_ID,
        )
        assert detail is not None
        # All two related-entity rows are direct legs (no bridges).
        assert all(r.via_junction is None for r in detail.related_entities)
        # The two legs: product_categories -> product, product_categories -> category.
        targets = {r.name for r in detail.related_entities}
        assert targets == {"product", "category"}


class TestEntityDetailWithPiiTags:
    def test_pii_tags_propagate_to_column_detail(self, populated_store: SQLiteStore) -> None:
        # Persist PII tags for two customer columns, then verify they
        # surface in the detail render's `pii_sensitivity` +
        # `pii_categories` fields. `PIICategory` is a `Literal[str]`,
        # so the in-memory representation is the bare string.
        populated_store.write_column_pii_tags(
            qualified_table="public.customers",
            tags={
                "email": ("pii", frozenset({"contact"})),
                "full_name": ("pii", frozenset({"contact"})),
            },
            source_connection_id=SOURCE_ID,
        )
        detail = build_entity_detail(
            store=populated_store,
            entity_name="customer",
            source_connection_id=SOURCE_ID,
        )
        assert detail is not None
        by_name = {c.name: c for c in detail.columns}
        assert by_name["email"].pii_sensitivity == "pii"
        assert by_name["email"].pii_categories == ("contact",)
        assert by_name["id"].pii_sensitivity == "public"
        assert by_name["id"].pii_categories == ()
