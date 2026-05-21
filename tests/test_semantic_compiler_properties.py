"""Property-based tests for the metric compiler.

Q16 from `project_v1_quality_plan.md` — mandatory because the
compiler is a v2-substrate seam where invariants must hold across the
whole input space, not just hand-picked examples.

The properties asserted:

  - **Single statement.** `emit_sql` NEVER produces a `;` regardless
    of metric shape / group_by / filters / time_grain. This is the
    I1 invariant from `project_v2_security_invariants.md`.

  - **Parameterisation.** Every filter value enters via `:p_*`
    placeholders bound in the returned params dict; no caller value
    appears inline in the SQL text. I3 invariant.

  - **LIMIT always present.** Every emit produces a `LIMIT :p_limit`
    clause; the params dict always carries `p_limit`. Defense against
    pathological group_by combinations exploding row count.

  - **Resolver determinism.** Two calls with identical arguments
    produce equal MetricPlan instances — `joins` sorted by
    canonical name, `group_by_columns` in insertion order, etc.

  - **No identifier leakage into params.** Entity/column names appear
    in SQL text (they're store-validated identifiers) but never in
    params keys/values.
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.join import CanonicalJoin, JoinColumnPair
from schemabrain.core.metric import Metric, MetricMeasure
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore
from schemabrain.semantic.compiler import (
    RequestedFilter,
    emit_sql,
    resolve_metric_plan,
)

SOURCE = "src_a"


# ----- fixture seed ----------------------------------------------------------


# Columns each fixture table carries beyond `id`. PR-6h.3's compile-
# time column-existence check now validates against the table's column
# list; the property tests reference these via Hypothesis-generated
# `entity.column` strings, so every referenced column must be present
# on the seeded table.
_FIXTURE_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "orders": (
        ("user_id", "bigint"),
        ("product_id", "bigint"),
        ("status", "text"),
        ("region", "text"),
        ("created_at", "timestamptz"),
        ("refunded_at", "timestamptz"),
        ("total_amount", "integer"),
    ),
    "customers": (
        ("country", "text"),
        ("tier", "text"),
    ),
    "products": (("category", "text"),),
}


def _seed(store: SQLiteStore) -> None:
    """A rich-enough store to exercise the compiler across input space.

    `order` (anchor) + `customer` (1-hop, many_to_one) + `product`
    (1-hop, one_to_many — fan-out case) + `total_revenue` (temporal,
    all 5 grains supported).
    """
    for table_name in ("orders", "customers", "products"):
        extras = _FIXTURE_COLUMNS.get(table_name, ())
        columns: tuple[Column, ...] = (
            Column(
                name="id",
                table_name=table_name,
                schema_name="public",
                data_type="bigint",
                nullable=False,
                ordinal_position=1,
                is_primary_key=True,
            ),
            *(
                Column(
                    name=col_name,
                    table_name=table_name,
                    schema_name="public",
                    data_type=col_type,
                    nullable=True,
                    ordinal_position=2 + i,
                    is_primary_key=False,
                )
                for i, (col_name, col_type) in enumerate(extras)
            ),
        )
        store.write_table(
            Table(name=table_name, schema_name="public", columns=columns),
            source_connection_id=SOURCE,
        )
    store.write_entity(
        Entity(
            name="order",
            description="",
            binding=SingleTableBinding(qualified_table="public.orders"),
            identity="id",
        ),
        source_connection_id=SOURCE,
    )
    store.write_entity(
        Entity(
            name="customer",
            description="",
            binding=SingleTableBinding(qualified_table="public.customers"),
            identity="id",
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
    store.write_canonical_join(
        CanonicalJoin(
            name="customer_orders",
            description="",
            source_entity="order",
            target_entity="customer",
            on=(JoinColumnPair(source_column="user_id", target_column="id"),),
            cardinality="many_to_one",
        ),
        source_connection_id=SOURCE,
    )
    store.write_canonical_join(
        CanonicalJoin(
            name="order_product",
            description="",
            source_entity="order",
            target_entity="product",
            on=(JoinColumnPair(source_column="product_id", target_column="id"),),
            cardinality="one_to_many",
        ),
        source_connection_id=SOURCE,
    )
    store.write_metric(
        Metric(
            name="total_revenue",
            description="",
            entity="order",
            measure=MetricMeasure(agg="sum", column="total_amount"),
            time_dimension="order.created_at",
            time_grains=("day", "week", "month", "quarter", "year"),
        ),
        source_connection_id=SOURCE,
    )


@pytest.fixture(scope="module")
def shared_store(tmp_path_factory: pytest.TempPathFactory) -> SQLiteStore:
    store = SQLiteStore(tmp_path_factory.mktemp("compiler_properties") / "store.db")
    _seed(store)
    return store


# ----- strategies ------------------------------------------------------------

# Closed set of group_by references the seed supports. Limiting to a
# known-resolvable set keeps the property tests focused on emit
# invariants — refusal-path properties are exercised by the unit tests.
_GROUP_BY_OPTIONS = (
    "order.status",
    "order.region",
    "customer.tier",
    "customer.country",
    "product.category",
)

_FILTER_COLUMN_OPTIONS = (
    "order.status",
    "order.total_amount",
    "order.refunded_at",
    "customer.tier",
)

_SCALAR_OPS = ("eq", "ne", "lt", "lte", "gt", "gte")
_LIST_OPS = ("in", "not_in")
_UNARY_OPS = ("is_null", "not_null")

_TIME_GRAINS = ("day", "week", "month", "quarter", "year")


@st.composite
def scalar_filter(draw: st.DrawFn) -> RequestedFilter:
    column = draw(st.sampled_from(_FILTER_COLUMN_OPTIONS))
    op = draw(st.sampled_from(_SCALAR_OPS))
    value = draw(
        st.one_of(
            st.integers(min_value=-1_000_000, max_value=1_000_000),
            st.text(
                alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"),
                min_size=0,
                max_size=64,
            ),
            st.booleans(),
        )
    )
    return RequestedFilter(column=column, op=op, value=value)  # type: ignore[arg-type]


@st.composite
def list_filter(draw: st.DrawFn) -> RequestedFilter:
    column = draw(st.sampled_from(_FILTER_COLUMN_OPTIONS))
    op = draw(st.sampled_from(_LIST_OPS))
    value = draw(
        st.lists(
            st.one_of(
                st.integers(min_value=-1000, max_value=1000),
                st.text(
                    alphabet=st.characters(
                        blacklist_categories=("Cs",), blacklist_characters="\x00"
                    ),
                    min_size=0,
                    max_size=16,
                ),
            ),
            min_size=1,
            max_size=5,
        )
    )
    return RequestedFilter(column=column, op=op, value=value)  # type: ignore[arg-type]


@st.composite
def unary_filter(draw: st.DrawFn) -> RequestedFilter:
    column = draw(st.sampled_from(_FILTER_COLUMN_OPTIONS))
    op = draw(st.sampled_from(_UNARY_OPS))
    return RequestedFilter(column=column, op=op)  # type: ignore[arg-type]


def any_filter() -> st.SearchStrategy[RequestedFilter]:
    return st.one_of(scalar_filter(), list_filter(), unary_filter())


# ----- properties ------------------------------------------------------------


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    group_by=st.lists(st.sampled_from(_GROUP_BY_OPTIONS), max_size=4).map(tuple),
    filters=st.lists(any_filter(), max_size=4).map(tuple),
    time_grain=st.one_of(st.none(), st.sampled_from(_TIME_GRAINS)),
    limit=st.integers(min_value=1, max_value=10_000),
)
def test_single_statement_invariant(
    shared_store: SQLiteStore,
    group_by: tuple[str, ...],
    filters: tuple[RequestedFilter, ...],
    time_grain: str | None,
    limit: int,
) -> None:
    """I1: emitted SQL never contains a `;`."""
    plan = resolve_metric_plan(
        store=shared_store,
        source_connection_id=SOURCE,
        metric_name="total_revenue",
        group_by=group_by,
        filters=filters,
        time_grain=time_grain,  # type: ignore[arg-type]
        limit=limit,
    )
    sql, _params = emit_sql(plan)
    assert ";" not in sql


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    group_by=st.lists(st.sampled_from(_GROUP_BY_OPTIONS), max_size=4).map(tuple),
    filters=st.lists(any_filter(), max_size=4).map(tuple),
    time_grain=st.one_of(st.none(), st.sampled_from(_TIME_GRAINS)),
)
def test_limit_always_present(
    shared_store: SQLiteStore,
    group_by: tuple[str, ...],
    filters: tuple[RequestedFilter, ...],
    time_grain: str | None,
) -> None:
    """Defense invariant: emitted SQL always carries `LIMIT :p_limit`
    and the params dict always carries `p_limit`."""
    plan = resolve_metric_plan(
        store=shared_store,
        source_connection_id=SOURCE,
        metric_name="total_revenue",
        group_by=group_by,
        filters=filters,
        time_grain=time_grain,  # type: ignore[arg-type]
    )
    sql, params = emit_sql(plan)
    assert "LIMIT :p_limit" in sql
    assert "p_limit" in params


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(filters=st.lists(scalar_filter(), min_size=1, max_size=4).map(tuple))
def test_parameterisation_invariant_structural(
    shared_store: SQLiteStore,
    filters: tuple[RequestedFilter, ...],
) -> None:
    """I3: every filter value binds via `:p_*` placeholder, never as
    an inline literal in the SQL text.

    Structural assertion in two parts:

      1. Every filter index N has a `p_filter_N` (scalar) entry in
         `params`, equal to the requested value.
      2. The SQL text contains no string-literal `'value'` shapes
         OTHER THAN the closed-grammar `date_trunc('<grain>', ...)`
         construct, which is safe-by-construction (grain is a
         Literal of 5 values verified by the resolver, not user
         input). We strip the safe pattern and assert the residue
         has zero quote characters — catching any unsafe inline
         interpolation directly.
    """
    plan = resolve_metric_plan(
        store=shared_store,
        source_connection_id=SOURCE,
        metric_name="total_revenue",
        filters=filters,
    )
    sql, params = emit_sql(plan)
    for index, requested in enumerate(filters):
        param_name = f"p_filter_{index}"
        assert param_name in params, (
            f"filter[{index}] value should bind via :{param_name} but param is missing"
        )
        assert params[param_name] == requested.value
    # Strip the closed-grammar safe pattern (`date_trunc('<grain>',`)
    # and assert no other single-quote characters remain. Catches the
    # SQL-injection-shape case without false-positiving on the
    # closed-grammar grain literal.
    import re

    residue = re.sub(
        r"date_trunc\('(?:day|week|month|quarter|year)',",
        "date_trunc(SAFE,",
        sql,
    )
    assert "'" not in residue, (
        "parameterised SQL contains an unexpected single quote — string literal leaked inline"
    )


@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    group_by=st.lists(st.sampled_from(_GROUP_BY_OPTIONS), max_size=4).map(tuple),
    filters=st.lists(any_filter(), max_size=3).map(tuple),
    time_grain=st.one_of(st.none(), st.sampled_from(_TIME_GRAINS)),
)
def test_resolver_determinism(
    shared_store: SQLiteStore,
    group_by: tuple[str, ...],
    filters: tuple[RequestedFilter, ...],
    time_grain: str | None,
) -> None:
    """Two calls with identical args produce equal MetricPlan
    instances. The future audit layer depends on this — fingerprints
    over `MetricPlan` must be stable across re-resolution."""
    plan1 = resolve_metric_plan(
        store=shared_store,
        source_connection_id=SOURCE,
        metric_name="total_revenue",
        group_by=group_by,
        filters=filters,
        time_grain=time_grain,  # type: ignore[arg-type]
    )
    plan2 = resolve_metric_plan(
        store=shared_store,
        source_connection_id=SOURCE,
        metric_name="total_revenue",
        group_by=group_by,
        filters=filters,
        time_grain=time_grain,  # type: ignore[arg-type]
    )
    assert plan1 == plan2


@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    group_by=st.lists(st.sampled_from(_GROUP_BY_OPTIONS), max_size=4).map(tuple),
)
def test_joins_follow_first_mention_order(
    shared_store: SQLiteStore,
    group_by: tuple[str, ...],
) -> None:
    """`MetricPlan.joins` is emitted in topological chain order — for
    this single-hop fixture (each entity reachable from anchor in one
    hop), that maps to "first-mention order" in `group_by`. The
    emitter relies on topological ordering so each JOIN can reference
    the previous hop's alias on the left side; the previous v1
    contract (alphabetical sort) no longer holds since PR-6h.1's
    multi-hop work."""
    plan = resolve_metric_plan(
        store=shared_store,
        source_connection_id=SOURCE,
        metric_name="total_revenue",
        group_by=group_by,
    )
    # Reconstruct the expected first-mention order of non-anchor entities.
    expected_entity_order: list[str] = []
    for column_ref in group_by:
        entity = column_ref.split(".", 1)[0]
        if entity == "order":
            continue  # anchor, no join needed
        if entity not in expected_entity_order:
            expected_entity_order.append(entity)
    actual_entity_order = [j.target_entity for j in plan.joins]
    assert actual_entity_order == expected_entity_order


@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(filters=st.lists(any_filter(), max_size=4).map(tuple))
def test_params_keys_are_placeholder_names(
    shared_store: SQLiteStore,
    filters: tuple[RequestedFilter, ...],
) -> None:
    """All param keys follow the `p_*` convention. A param key that
    doesn't match would mean a literal leaked into the placeholder
    namespace — caught here before reaching the DB."""
    plan = resolve_metric_plan(
        store=shared_store,
        source_connection_id=SOURCE,
        metric_name="total_revenue",
        filters=filters,
    )
    _sql, params = emit_sql(plan)
    for key in params:
        assert key.startswith("p_"), f"param key {key!r} doesn't follow p_* convention"


def test_param_count_matches_placeholder_count(shared_store: SQLiteStore) -> None:
    """Every `:p_*` placeholder in the SQL has a binding in params."""
    # This one is deterministic — not Hypothesis, but property-shaped.
    plan = resolve_metric_plan(
        store=shared_store,
        source_connection_id=SOURCE,
        metric_name="total_revenue",
        filters=(
            RequestedFilter(column="order.status", op="eq", value="completed"),
            RequestedFilter(
                column="order.total_amount",
                op="in",
                value=[100, 200, 300],
            ),
            RequestedFilter(column="order.refunded_at", op="is_null"),
        ),
        time_grain="day",
    )
    sql, params = emit_sql(plan)
    # Extract placeholders from SQL.
    import re

    placeholders = set(re.findall(r":(p_[A-Za-z0-9_]+)", sql))
    assert placeholders == set(params.keys()), (
        f"placeholder/param mismatch — placeholders: {placeholders}, params: {set(params.keys())}"
    )
