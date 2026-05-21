"""Tests for `order_by` parameter on `get_metric` + auto tie-breaking.

Locks the contract introduced in PR-6h.2 (Gap #6 + Gap #7):

  - `order_by` defaults to `()` → no ORDER BY emitted (backward
    compatible — every prior caller of `resolve_metric_plan` continues
    to produce the same SQL).
  - `order_by` accepting `RequestedOrderBy(column=metric.name, ...)`
    resolves to the measure's SELECT alias.
  - `order_by` accepting `RequestedOrderBy(column=<entity>.<col>, ...)`
    where `(entity, col)` is in `group_by` resolves to the
    corresponding `group_col_N` alias.
  - Any other `column` reference raises `UnknownOrderByColumnError`
    with an `allowed_columns` payload listing the legal references.
  - When `order_by` is non-empty AND `group_by` is non-empty, the
    resolver auto-appends a deterministic tie-breaker (first
    group-by column ASC, flagged `auto_appended_tie_break=True`).
    When the caller already orders by the first group_by column, no
    tie-break is appended (the order is already deterministic by
    that column).
  - When `order_by` is non-empty but `group_by` is empty (single-row
    aggregate), no tie-break is appended.
  - Emit produces `ORDER BY "<alias>" {ASC|DESC}` between GROUP BY
    and LIMIT, with each clause comma-separated, direction uppercased.

Closes the agent-UX gap surfaced by the 2026-05-21 smoke: Claude
called `get_metric(..., limit=5)` expecting "top 5", but the compiler
emitted no ORDER BY — with only 3 rows in the demo store the lucky
result was right, but on a real table the slice would have been
non-deterministic.
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
    RequestedFilter,
    RequestedOrderBy,
    ResolvedOrderBy,
    UnknownFilterColumnError,
    UnknownGroupByColumnError,
    UnknownMeasureColumnError,
    UnknownOrderByColumnError,
    emit_sql,
    resolve_metric_plan,
)

SOURCE = "src_a"


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


def _col(table: str, name: str, *, ordinal: int, data_type: str = "text") -> Column:
    return Column(
        name=name,
        table_name=table,
        schema_name="public",
        data_type=data_type,
        nullable=True,
        ordinal_position=ordinal,
        is_primary_key=False,
    )


# Columns each fixture table carries beyond `id`. Centralised so the
# `_seed_two_hop` fixture stays in sync with what the compiler's column-
# existence validation (PR-6h.3) now checks against. Adding a column
# referenced by a test means adding it to this map AND seeding it.
_FIXTURE_COLUMNS: dict[str, tuple[str, ...]] = {
    "order_items": ("quantity", "product_id"),
    "orders": ("placed_at", "user_id"),
    "users": ("email", "full_name"),
}


def _seed_two_hop(store: SQLiteStore) -> None:
    """`order_item → order → user`. Minimal fixture matching the
    PR-6h.1 + PR-6h.1.1 multi-hop tests; `total_items_sold` metric
    anchored on `order_item` lets us exercise group_by + order_by
    against a 2-hop chain.
    """
    for name in ("order_items", "orders", "users"):
        extra = _FIXTURE_COLUMNS.get(name, ())
        cols: tuple[Column, ...] = (
            _id_col(name),
            *(_col(name, col_name, ordinal=2 + i) for i, col_name in enumerate(extra)),
        )
        store.write_table(
            Table(name=name, schema_name="public", columns=cols),
            source_connection_id=SOURCE,
        )
    for entity_name, table in (
        ("order_item", "public.order_items"),
        ("order", "public.orders"),
        ("user", "public.users"),
    ):
        store.write_entity(
            Entity(
                name=entity_name,
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


class TestOrderByDefault:
    def test_no_order_by_produces_no_clause_in_sql(self, tmp_path: Path) -> None:
        """Default `order_by=()` is the v0 behavior — backward
        compatible with every existing caller. Emitter must NOT
        produce an ORDER BY line.
        """
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_two_hop(store)
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_items_sold",
                group_by=("user.email",),
            )
        assert plan.order_by_clauses == ()
        sql_text, _ = emit_sql(plan)
        assert "ORDER BY" not in sql_text


class TestOrderByByMeasureName:
    def test_order_by_measure_name_resolves_and_emits(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_two_hop(store)
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_items_sold",
                group_by=("user.email",),
                order_by=(RequestedOrderBy(column="total_items_sold", direction="desc"),),
            )
        # Caller's clause + auto tie-break (group_col_0 ASC).
        assert len(plan.order_by_clauses) == 2
        first, tiebreak = plan.order_by_clauses
        assert first == ResolvedOrderBy(
            expression="total_items_sold",
            direction="desc",
            auto_appended_tie_break=False,
        )
        assert tiebreak == ResolvedOrderBy(
            expression="group_col_0",
            direction="asc",
            auto_appended_tie_break=True,
        )

        sql_text, _ = emit_sql(plan)
        assert 'ORDER BY "total_items_sold" DESC, "group_col_0" ASC' in sql_text
        # ORDER BY must come AFTER GROUP BY and BEFORE LIMIT.
        order_pos = sql_text.find("ORDER BY")
        group_pos = sql_text.find("GROUP BY")
        limit_pos = sql_text.find("LIMIT")
        assert group_pos < order_pos < limit_pos


class TestOrderByByGroupColumn:
    def test_order_by_group_by_column_resolves_to_group_col_alias(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_two_hop(store)
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_items_sold",
                group_by=("user.email",),
                order_by=(RequestedOrderBy(column="user.email", direction="asc"),),
            )
        # Caller asked to order by `user.email` which is group_col_0;
        # the tie-break would also be group_col_0 → skipped (already
        # ordered by it).
        assert plan.order_by_clauses == (
            ResolvedOrderBy(
                expression="group_col_0", direction="asc", auto_appended_tie_break=False
            ),
        )


class TestOrderByErrorCases:
    def test_unknown_column_raises_unknown_order_by_column(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_two_hop(store)
            with pytest.raises(UnknownOrderByColumnError) as exc_info:
                resolve_metric_plan(
                    store=store,
                    source_connection_id=SOURCE,
                    metric_name="total_items_sold",
                    group_by=("user.email",),
                    order_by=(RequestedOrderBy(column="user.full_name", direction="asc"),),
                )
        assert exc_info.value.requested_column == "user.full_name"
        # Allowed: metric name + every group_by reference.
        assert set(exc_info.value.allowed_columns) == {"total_items_sold", "user.email"}

    def test_unknown_column_with_no_group_by_only_allows_measure(self, tmp_path: Path) -> None:
        """When `group_by` is empty, the only allowed ORDER BY column
        is the metric's name. Trying to ORDER BY anything else (even
        a column that exists on the anchor entity) raises.
        """
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_two_hop(store)
            with pytest.raises(UnknownOrderByColumnError) as exc_info:
                resolve_metric_plan(
                    store=store,
                    source_connection_id=SOURCE,
                    metric_name="total_items_sold",
                    order_by=(RequestedOrderBy(column="order_item.quantity"),),
                )
        assert exc_info.value.allowed_columns == ("total_items_sold",)


class TestTieBreak:
    def test_tie_break_omitted_when_no_group_by(self, tmp_path: Path) -> None:
        """Single-row aggregate — no group_by, no tie-break needed."""
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_two_hop(store)
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_items_sold",
                order_by=(RequestedOrderBy(column="total_items_sold", direction="desc"),),
            )
        assert plan.order_by_clauses == (
            ResolvedOrderBy(
                expression="total_items_sold",
                direction="desc",
                auto_appended_tie_break=False,
            ),
        )

    def test_tie_break_omitted_when_order_by_already_covers_first_group_col(
        self, tmp_path: Path
    ) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_two_hop(store)
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_items_sold",
                group_by=("user.email",),
                order_by=(
                    RequestedOrderBy(column="total_items_sold", direction="desc"),
                    RequestedOrderBy(column="user.email", direction="asc"),
                ),
            )
        # Caller covered the first group_col explicitly; no auto-append.
        assert all(not c.auto_appended_tie_break for c in plan.order_by_clauses)
        assert len(plan.order_by_clauses) == 2

    def test_duplicate_order_by_clauses_deduped(self, tmp_path: Path) -> None:
        """Passing the same column twice in `order_by` silently drops
        the second occurrence — duplicate ORDER BY entries have no
        SQL effect and aren't worth refusing the call over.
        """
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_two_hop(store)
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_items_sold",
                group_by=("user.email",),
                order_by=(
                    RequestedOrderBy(column="total_items_sold", direction="desc"),
                    RequestedOrderBy(column="total_items_sold", direction="asc"),
                ),
            )
        # Only the FIRST occurrence is kept; the second (asc) is silently dropped.
        # Tie-break appended since group_col_0 not in seen yet.
        assert plan.order_by_clauses == (
            ResolvedOrderBy(
                expression="total_items_sold",
                direction="desc",
                auto_appended_tie_break=False,
            ),
            ResolvedOrderBy(
                expression="group_col_0",
                direction="asc",
                auto_appended_tie_break=True,
            ),
        )


# ----- error class shape contracts -------------------------------------------


class TestUnknownGroupByColumn:
    """Closes the 2026-05-21 stress-test silent-failure path:
    `get_metric(group_by=['user.bogus_column'])` used to emit literal
    SQL `"user"."bogus_column"`, run it against Postgres, get
    `UndefinedColumn` back, and surface to the agent as
    `internal_error` — useless feedback for a typo. The new
    compile-time check raises `UnknownGroupByColumnError` BEFORE SQL
    emission so the MCP layer can map to a clean envelope kind with
    the allowed-column set in `recovery.suggested_args`.
    """

    def test_unknown_column_on_anchor_entity_raises(self, tmp_path: Path) -> None:
        # Anchor entity is `order_item`; column `bogus_column` doesn't
        # exist on `public.order_items`. Validate before BFS even runs.
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_two_hop(store)
            with pytest.raises(UnknownGroupByColumnError) as exc_info:
                resolve_metric_plan(
                    store=store,
                    source_connection_id=SOURCE,
                    metric_name="total_items_sold",
                    group_by=("order_item.bogus_column",),
                )
        assert exc_info.value.entity == "order_item"
        assert exc_info.value.column == "bogus_column"
        # Allowed columns is the real per-table list (sorted) — agents
        # can pick any of these as a retry.
        assert "id" in exc_info.value.allowed_columns
        assert "quantity" in exc_info.value.allowed_columns

    def test_unknown_column_on_joined_entity_raises(self, tmp_path: Path) -> None:
        # `user` is reachable via 2-hop chain from `order_item`. Once
        # BFS resolves the join, the column check fires.
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_two_hop(store)
            with pytest.raises(UnknownGroupByColumnError) as exc_info:
                resolve_metric_plan(
                    store=store,
                    source_connection_id=SOURCE,
                    metric_name="total_items_sold",
                    group_by=("user.bogus_column",),
                )
        assert exc_info.value.entity == "user"
        assert exc_info.value.column == "bogus_column"
        assert set(exc_info.value.allowed_columns) == {"id", "email", "full_name"}

    def test_known_column_on_joined_entity_still_resolves(self, tmp_path: Path) -> None:
        # Pinning the happy path so the validation guard doesn't
        # accidentally over-reject for a column that DOES exist.
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_two_hop(store)
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_items_sold",
                group_by=("user.email",),
            )
        assert len(plan.group_by_columns) == 1
        assert plan.group_by_columns[0].column == "email"


class TestUnknownFilterColumn:
    """Parallel to TestUnknownGroupByColumn — the same compile-time
    check on the `filters=` surface.
    """

    def test_unknown_filter_column_raises(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_two_hop(store)
            with pytest.raises(UnknownFilterColumnError) as exc_info:
                resolve_metric_plan(
                    store=store,
                    source_connection_id=SOURCE,
                    metric_name="total_items_sold",
                    filters=(RequestedFilter(column="user.bogus_column", op="eq", value="x"),),
                )
        assert exc_info.value.entity == "user"
        assert exc_info.value.column == "bogus_column"
        assert "email" in exc_info.value.allowed_columns


def test_unknown_group_by_column_error_pickles() -> None:
    """Frozen-dataclass error must round-trip through pickle — locks
    `__reduce__` shape against accidental field-order drift, matching
    the `UnknownOrderByColumnError` contract."""
    import pickle

    err = UnknownGroupByColumnError(
        entity="user",
        column="bogus",
        allowed_columns=("email", "full_name", "id"),
    )
    revived: UnknownGroupByColumnError = pickle.loads(pickle.dumps(err))
    assert revived.entity == err.entity
    assert revived.column == err.column
    assert revived.allowed_columns == err.allowed_columns


class TestUnknownMeasureColumn:
    """Compile-time validation of every column the measure references
    against the anchor entity's table. Covers BOTH the v1 bare-column
    path and the v2 composite-expression path — a typo in either shape
    surfaces here as a clean envelope instead of an `internal_error`
    masking Postgres's UndefinedColumn."""

    def test_unknown_bare_measure_column_raises(self, tmp_path: Path) -> None:
        from schemabrain.core.metric import Metric, MetricMeasure

        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_two_hop(store)
            # Overwrite the seeded `total_items_sold` metric with one
            # that references a column that doesn't exist on `order_item`.
            store.write_metric(
                Metric(
                    name="total_items_sold",
                    description="",
                    entity="order_item",
                    measure=MetricMeasure(agg="sum", column="bogus_column"),
                    time_dimension=None,
                    time_grains=(),
                ),
                source_connection_id=SOURCE,
            )
            with pytest.raises(UnknownMeasureColumnError) as exc_info:
                resolve_metric_plan(
                    store=store,
                    source_connection_id=SOURCE,
                    metric_name="total_items_sold",
                )
        assert exc_info.value.entity == "order_item"
        assert exc_info.value.column == "bogus_column"

    def test_unknown_composite_operand_raises(self, tmp_path: Path) -> None:
        from schemabrain.core.metric import Metric, MetricMeasure

        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_two_hop(store)
            # Composite expression with one valid operand + one typo.
            # `quantity` is on `order_item`; `bogus_field` is not.
            store.write_metric(
                Metric(
                    name="total_items_sold",
                    description="",
                    entity="order_item",
                    measure=MetricMeasure(agg="sum", expression="quantity * bogus_field"),
                    time_dimension=None,
                    time_grains=(),
                ),
                source_connection_id=SOURCE,
            )
            with pytest.raises(UnknownMeasureColumnError) as exc_info:
                resolve_metric_plan(
                    store=store,
                    source_connection_id=SOURCE,
                    metric_name="total_items_sold",
                )
        assert exc_info.value.entity == "order_item"
        assert exc_info.value.column == "bogus_field"


def test_unknown_filter_column_error_pickles() -> None:
    import pickle

    err = UnknownFilterColumnError(
        entity="order",
        column="bogus",
        allowed_columns=("id", "placed_at", "user_id"),
    )
    revived: UnknownFilterColumnError = pickle.loads(pickle.dumps(err))
    assert revived.entity == err.entity
    assert revived.column == err.column
    assert revived.allowed_columns == err.allowed_columns


def test_unknown_measure_column_error_pickles() -> None:
    import pickle

    err = UnknownMeasureColumnError(
        entity="order_item",
        column="bogus",
        allowed_columns=("id", "order_id", "quantity", "unit_price"),
    )
    revived: UnknownMeasureColumnError = pickle.loads(pickle.dumps(err))
    assert revived.entity == err.entity
    assert revived.column == err.column
    assert revived.allowed_columns == err.allowed_columns


def test_ambiguous_path_error_pickles() -> None:
    """The audit layer fingerprints refusal events; AmbiguousPathError's
    `candidate_paths` field is a nested tuple, so `__reduce__` must
    preserve the structure round-trip."""
    import pickle

    from schemabrain.semantic.compiler import AmbiguousPathError

    err = AmbiguousPathError(
        anchor_entity="order_item",
        target_entity="user",
        candidate_paths=(
            ("order_items_order_id", "orders_user_id_billing"),
            ("order_items_order_id", "orders_user_id_shipping"),
        ),
    )
    revived: AmbiguousPathError = pickle.loads(pickle.dumps(err))
    assert revived.anchor_entity == err.anchor_entity
    assert revived.target_entity == err.target_entity
    assert revived.candidate_paths == err.candidate_paths


def test_unknown_via_join_error_pickles() -> None:
    """Same audit-fingerprint contract as the other compiler errors.
    `requested_via` and `available_join_names` are both tuples that
    must survive the pickle round-trip."""
    import pickle

    from schemabrain.semantic.compiler import UnknownViaJoinError

    err = UnknownViaJoinError(
        anchor_entity="order",
        target_entity="user",
        requested_via=("ghost_join",),
        available_join_names=("orders_user_id_billing", "orders_user_id_shipping"),
    )
    revived: UnknownViaJoinError = pickle.loads(pickle.dumps(err))
    assert revived.anchor_entity == err.anchor_entity
    assert revived.target_entity == err.target_entity
    assert revived.requested_via == err.requested_via
    assert revived.available_join_names == err.available_join_names


def test_unknown_order_by_column_error_pickles() -> None:
    """Frozen-dataclass error classes must round-trip through pickle
    so a future audit layer can fingerprint refusals. Lock the
    `__reduce__` shape against accidental field-order drift.
    """
    import pickle

    err = UnknownOrderByColumnError(
        requested_column="user.full_name",
        allowed_columns=("total_items_sold", "user.email"),
    )
    revived: UnknownOrderByColumnError = pickle.loads(pickle.dumps(err))
    assert revived.requested_column == err.requested_column
    assert revived.allowed_columns == err.allowed_columns
    assert str(revived) == str(err)
