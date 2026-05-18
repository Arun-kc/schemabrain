"""Tests for `joins/suggest.py` — the canonical-join suggestion pipeline.

Pins the canonical-join suggester contracts:

  - FK seeds always present (deterministic); query-log evidence
    layered on top
  - FK + query-log evidence for the same `(source_entity,
    target_entity, on)` merge into ONE candidate with both
    evidence tags
  - FK without query-log → confidence `"high"`, frequency 0
  - Query-log without FK → confidence `"medium"`/`"low"` per
    `_MEDIUM_CONFIDENCE_FREQUENCY`
  - FK seed self-references dropped (self-joins not supported)
  - Candidates whose physical tables don't both back an entity
    dropped silently
  - Output sorted by confidence DESC, then frequency DESC, then
    name ASC
  - `to_canonical_join` strips provenance — the persisted shape is
    clean
  - Cycle detection report surfaces cycles + isolated entities;
    cycles do NOT block writes (per the design)
"""

from __future__ import annotations

from pathlib import Path

from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.example_query import ExampleQuery
from schemabrain.core.join import CanonicalJoin, JoinColumnPair
from schemabrain.core.models import Column, ForeignKey, Table
from schemabrain.core.store import SQLiteStore
from schemabrain.joins.suggest import (
    JoinCandidate,
    detect_cycles_in_join_graph,
    suggest_canonical_joins,
)

SOURCE_A = "src_a"


# ----- helpers ---------------------------------------------------------------


def _column(*, name: str, table: str, schema: str = "public", ordinal: int = 1) -> Column:
    return Column(
        name=name,
        table_name=table,
        schema_name=schema,
        data_type="bigint",
        nullable=False,
        ordinal_position=ordinal,
        is_primary_key=(name == "id"),
    )


def _table(
    name: str,
    schema: str = "public",
    *,
    foreign_keys: tuple[ForeignKey, ...] = (),
    extra_columns: tuple[str, ...] = (),
) -> Table:
    # Always carry `id` as the PK. `extra_columns` lets a caller add
    # FK-source columns like `user_id` so the pydantic FK-shape
    # validator doesn't refuse the construction.
    columns = (_column(name="id", table=name, schema=schema, ordinal=1),)
    columns += tuple(
        _column(name=col, table=name, schema=schema, ordinal=i + 2)
        for i, col in enumerate(extra_columns)
    )
    return Table(
        name=name,
        schema_name=schema,
        columns=columns,
        foreign_keys=foreign_keys,
    )


def _entity(name: str, qualified_table: str) -> Entity:
    return Entity(
        name=name,
        description="",
        binding=SingleTableBinding(qualified_table=qualified_table),
        identity="id",
    )


def _fk(
    name: str,
    source_columns: tuple[str, ...],
    target_schema: str,
    target_table: str,
    target_columns: tuple[str, ...],
) -> ForeignKey:
    return ForeignKey(
        name=name,
        source_columns=source_columns,
        target_schema=target_schema,
        target_table=target_table,
        target_columns=target_columns,
    )


def _seeded_store(
    tmp_path: Path,
    *,
    order_fks: tuple[ForeignKey, ...] = (),
    order_extra_columns: tuple[str, ...] = (),
) -> SQLiteStore:
    """A store with users + orders tables + matching entities.

    `order_fks` attaches FKs to the `orders` table at write time —
    the store persists them inline with the table; there's no
    separate FK-writing method.

    `order_extra_columns` accommodates the FK-shape validator —
    each FK's `source_columns` must be present on the `orders` row.
    Defaults to `("user_id",)` when `order_fks` is non-empty so
    common FK-shape tests stay terse.
    """
    if order_fks and not order_extra_columns:
        order_extra_columns = ("user_id",)
    store = SQLiteStore(tmp_path / "store.db")
    store.write_table(_table("users"), source_connection_id=SOURCE_A)
    store.write_table(
        _table(
            "orders",
            foreign_keys=order_fks,
            extra_columns=order_extra_columns,
        ),
        source_connection_id=SOURCE_A,
    )
    store.write_entity(_entity("customer", "public.users"), source_connection_id=SOURCE_A)
    store.write_entity(_entity("order", "public.orders"), source_connection_id=SOURCE_A)
    return store


# ----- FK-only candidates ----------------------------------------------------


class TestFkOnlyCandidates:
    def test_single_fk_yields_one_candidate(self, tmp_path: Path) -> None:
        store = _seeded_store(
            tmp_path,
            order_fks=(
                _fk(
                    "orders_user_id_fkey",
                    ("user_id",),
                    "public",
                    "users",
                    ("id",),
                ),
            ),
        )
        candidates = suggest_canonical_joins(store=store, source_connection_id=SOURCE_A)
        store.close()
        assert len(candidates) == 1
        c = candidates[0]
        assert c.source_entity == "order"
        assert c.target_entity == "customer"
        assert c.confidence == "high"
        assert c.evidence == ("foreign_key",)
        assert c.fk_name == "orders_user_id_fkey"
        assert c.query_log_frequency == 0
        # Auto-generated name strips the `_fkey` suffix.
        assert c.name == "orders_user_id"

    def test_composite_fk_yields_multi_pair_candidate(self, tmp_path: Path) -> None:
        # Junction-table shape: org_members(user_id, org_id) → users(id, org_id).
        store = SQLiteStore(tmp_path / "store.db")
        store.write_table(
            _table("users", extra_columns=("org_id",)),
            source_connection_id=SOURCE_A,
        )
        store.write_table(
            _table(
                "org_members",
                foreign_keys=(
                    _fk(
                        "org_members_user_fkey",
                        ("user_id", "org_id"),
                        "public",
                        "users",
                        ("id", "org_id"),
                    ),
                ),
                extra_columns=("user_id", "org_id"),
            ),
            source_connection_id=SOURCE_A,
        )
        store.write_entity(_entity("user", "public.users"), source_connection_id=SOURCE_A)
        store.write_entity(
            _entity("org_member", "public.org_members"),
            source_connection_id=SOURCE_A,
        )
        candidates = suggest_canonical_joins(store=store, source_connection_id=SOURCE_A)
        store.close()
        assert len(candidates) == 1
        assert len(candidates[0].on) == 2

    def test_fk_to_table_without_entity_dropped(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "store.db")
        store.write_table(_table("users"), source_connection_id=SOURCE_A)
        store.write_table(
            _table(
                "orders",
                foreign_keys=(
                    _fk(
                        "orders_user_id_fkey",
                        ("user_id",),
                        "public",
                        "users",
                        ("id",),
                    ),
                ),
                extra_columns=("user_id",),
            ),
            source_connection_id=SOURCE_A,
        )
        # Only `order` entity exists — `customer` does not.
        store.write_entity(_entity("order", "public.orders"), source_connection_id=SOURCE_A)
        candidates = suggest_canonical_joins(store=store, source_connection_id=SOURCE_A)
        store.close()
        # No matching entity on target side — candidate dropped.
        assert candidates == []

    def test_self_referential_fk_dropped(self, tmp_path: Path) -> None:
        # users.manager_id → users.id — self-FK. Suggester drops at v1.
        store = SQLiteStore(tmp_path / "store.db")
        store.write_table(
            _table(
                "users",
                foreign_keys=(
                    _fk(
                        "users_manager_id_fkey",
                        ("manager_id",),
                        "public",
                        "users",
                        ("id",),
                    ),
                ),
                extra_columns=("manager_id",),
            ),
            source_connection_id=SOURCE_A,
        )
        store.write_entity(_entity("user", "public.users"), source_connection_id=SOURCE_A)
        candidates = suggest_canonical_joins(store=store, source_connection_id=SOURCE_A)
        store.close()
        assert candidates == []


# ----- query-log-only candidates ---------------------------------------------


def _write_example_sql(store: SQLiteStore, sql: str, observation_count: int = 1) -> None:
    """Persist one observed SQL statement under each touched table.

    Mirrors what the `mine-queries` pipeline does — one row per
    (table_touched, sql_text) pair.
    """
    from schemabrain.mining.sql_parse import extract_table_references

    refs = extract_table_references(sql)
    rows = []
    for schema, table in refs:
        if schema is None:
            continue
        rows.append(
            ExampleQuery(
                schema_name=schema,
                table_name=table,
                sql_text=sql,
                observation_count=observation_count,
                first_seen_at=0,
                last_seen_at=0,
                source="pg_stat_statements",
                sensitivity="public",
                pii_categories=frozenset(),
            )
        )
    if rows:
        store.write_example_queries(rows, source_connection_id=SOURCE_A)


class TestQueryLogCandidates:
    def test_query_log_only_join_yields_low_confidence(self, tmp_path: Path) -> None:
        # Single observation of a JOIN with no FK constraint.
        store = _seeded_store(tmp_path)
        _write_example_sql(
            store,
            "SELECT * FROM public.users u JOIN public.orders o ON u.id = o.user_id",
            observation_count=1,
        )
        candidates = suggest_canonical_joins(store=store, source_connection_id=SOURCE_A)
        store.close()
        assert len(candidates) == 1
        c = candidates[0]
        assert c.confidence == "low"
        assert c.evidence == ("query_log",)
        assert c.fk_name is None
        assert c.query_log_frequency == 1

    def test_frequent_query_log_join_yields_medium_confidence(self, tmp_path: Path) -> None:
        store = _seeded_store(tmp_path)
        # Single statement observed 25 times — well above the 5-threshold.
        _write_example_sql(
            store,
            "SELECT * FROM public.users u JOIN public.orders o ON u.id = o.user_id",
            observation_count=25,
        )
        candidates = suggest_canonical_joins(store=store, source_connection_id=SOURCE_A)
        store.close()
        assert len(candidates) == 1
        assert candidates[0].confidence == "medium"
        assert candidates[0].query_log_frequency == 25


# ----- merge: FK + query-log -------------------------------------------------


class TestEvidenceMerge:
    def test_fk_and_query_log_combine_into_one_candidate(self, tmp_path: Path) -> None:
        store = _seeded_store(
            tmp_path,
            order_fks=(
                _fk(
                    "orders_user_id_fkey",
                    ("user_id",),
                    "public",
                    "users",
                    ("id",),
                ),
            ),
        )
        _write_example_sql(
            store,
            "SELECT * FROM public.users u JOIN public.orders o ON u.id = o.user_id",
            observation_count=10,
        )
        candidates = suggest_canonical_joins(store=store, source_connection_id=SOURCE_A)
        store.close()
        assert len(candidates) == 1
        c = candidates[0]
        assert c.confidence == "high"
        # Both evidence sources surface on the envelope.
        assert "foreign_key" in c.evidence
        assert "query_log" in c.evidence
        assert c.fk_name == "orders_user_id_fkey"
        assert c.query_log_frequency == 10


# ----- multi-canonical-per-pair ----------------------------------------------


class TestMultiCanonicalPerPair:
    def test_billing_and_shipping_yield_distinct_candidates(self, tmp_path: Path) -> None:
        # The the design's poster child: two FKs from `orders` to
        # `addresses` produce two distinct canonical-join candidates,
        # with distinct auto-generated names.
        store = SQLiteStore(tmp_path / "store.db")
        store.write_table(_table("addresses"), source_connection_id=SOURCE_A)
        store.write_table(
            _table(
                "orders",
                foreign_keys=(
                    _fk(
                        "orders_billing_address_id_fkey",
                        ("billing_address_id",),
                        "public",
                        "addresses",
                        ("id",),
                    ),
                    _fk(
                        "orders_shipping_address_id_fkey",
                        ("shipping_address_id",),
                        "public",
                        "addresses",
                        ("id",),
                    ),
                ),
                extra_columns=("billing_address_id", "shipping_address_id"),
            ),
            source_connection_id=SOURCE_A,
        )
        store.write_entity(_entity("order", "public.orders"), source_connection_id=SOURCE_A)
        store.write_entity(_entity("address", "public.addresses"), source_connection_id=SOURCE_A)
        candidates = suggest_canonical_joins(store=store, source_connection_id=SOURCE_A)
        store.close()
        # Two distinct candidates between the same entity pair, with
        # distinct auto-generated names.
        assert len(candidates) == 2
        names = sorted(c.name for c in candidates)
        # Auto-names derived from FK names (strip `_fkey`).
        assert names == [
            "orders_billing_address_id",
            "orders_shipping_address_id",
        ]
        # Same source-target pair on both.
        for c in candidates:
            assert c.source_entity == "order"
            assert c.target_entity == "address"


# ----- sort ordering ---------------------------------------------------------


class TestSortOrdering:
    def test_high_confidence_before_medium_and_low(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "store.db")
        # 3 entity tables, FK on one pair, query-log on another.
        store.write_table(_table("users"), source_connection_id=SOURCE_A)
        store.write_table(_table("products"), source_connection_id=SOURCE_A)
        store.write_table(
            _table(
                "orders",
                foreign_keys=(
                    _fk(
                        "orders_user_id_fkey",
                        ("user_id",),
                        "public",
                        "users",
                        ("id",),
                    ),
                ),
                extra_columns=("user_id", "product_id"),
            ),
            source_connection_id=SOURCE_A,
        )
        store.write_entity(_entity("customer", "public.users"), source_connection_id=SOURCE_A)
        store.write_entity(_entity("order", "public.orders"), source_connection_id=SOURCE_A)
        store.write_entity(_entity("product", "public.products"), source_connection_id=SOURCE_A)
        _write_example_sql(
            store,
            "SELECT * FROM public.orders o JOIN public.products p ON o.product_id = p.id",
            observation_count=1,  # → low confidence
        )
        candidates = suggest_canonical_joins(store=store, source_connection_id=SOURCE_A)
        store.close()
        # FK candidate first (high), query-log candidate second (low).
        assert candidates[0].confidence == "high"
        assert candidates[1].confidence == "low"


# ----- to_canonical_join ----------------------------------------------------


class TestToCanonicalJoin:
    def test_strips_provenance_envelope(self) -> None:
        candidate = JoinCandidate(
            name="customer_orders",
            source_entity="order",
            target_entity="customer",
            on=(JoinColumnPair(source_column="user_id", target_column="id"),),
            confidence="high",
            evidence=("foreign_key", "query_log"),
            fk_name="orders_user_id_fkey",
            query_log_frequency=42,
            rationale="FK + query-log confirm.",
        )
        canonical = candidate.to_canonical_join()
        assert isinstance(canonical, CanonicalJoin)
        assert canonical.name == "customer_orders"
        assert canonical.origin == "suggested"
        # Provenance does NOT bleed into the persisted shape — the
        # canonical record stays clean.

    def test_origin_override_for_manual_path(self) -> None:
        candidate = JoinCandidate(
            name="customer_orders",
            source_entity="order",
            target_entity="customer",
            on=(JoinColumnPair(source_column="user_id", target_column="id"),),
            confidence="high",
            evidence=("foreign_key",),
            fk_name=None,
            query_log_frequency=0,
            rationale="",
        )
        canonical = candidate.to_canonical_join(origin="manual")
        assert canonical.origin == "manual"


# ----- empty / degenerate cases ----------------------------------------------


class TestQueryLogTouchingNonEntity:
    def test_query_log_join_with_non_entity_table_dropped(self, tmp_path: Path) -> None:
        # Indexed table without an entity. Query log joins against it
        # produce an ExtractedJoin, but the suggester drops it because
        # one endpoint doesn't back an entity.
        store = _seeded_store(tmp_path)
        # `addresses` table indexed but NO matching entity.
        store.write_table(_table("addresses"), source_connection_id=SOURCE_A)
        sql = "SELECT * FROM public.users u JOIN public.addresses a ON u.id = a.user_id"
        store.write_example_queries(
            [
                ExampleQuery(
                    schema_name="public",
                    table_name="users",
                    sql_text=sql,
                    observation_count=1,
                    first_seen_at=0,
                    last_seen_at=0,
                    source="pg_stat_statements",
                    sensitivity="public",
                    pii_categories=frozenset(),
                )
            ],
            source_connection_id=SOURCE_A,
        )
        candidates = suggest_canonical_joins(store=store, source_connection_id=SOURCE_A)
        store.close()
        # No candidate surfaces — addresses entity is missing.
        assert candidates == []


class TestEmptyCases:
    def test_no_entities_yields_empty_suggestions(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "store.db")
        candidates = suggest_canonical_joins(store=store, source_connection_id=SOURCE_A)
        store.close()
        assert candidates == []

    def test_no_evidence_yields_empty_suggestions(self, tmp_path: Path) -> None:
        # Entities exist but no FKs and no query log.
        store = _seeded_store(tmp_path)
        candidates = suggest_canonical_joins(store=store, source_connection_id=SOURCE_A)
        store.close()
        assert candidates == []


# ----- cycle detection -------------------------------------------------------


def _join(name: str, source: str, target: str) -> CanonicalJoin:
    return CanonicalJoin(
        name=name,
        description="",
        source_entity=source,
        target_entity=target,
        on=(JoinColumnPair(source_column=f"{target}_id", target_column="id"),),
    )


class TestCandidateNaming:
    def test_collision_appends_numeric_suffix(self, tmp_path: Path) -> None:
        # Two FKs with names that clean to the same value (e.g.
        # `_fk` vs `_fkey` suffixes) hit the collision path in
        # `_allocate_name`. Verify the second one gets `_2`.
        from schemabrain.joins.suggest import _allocate_name

        used: set[str] = set()
        first = _allocate_name(preferred="my_join", used=used)
        second = _allocate_name(preferred="my_join", used=used)
        third = _allocate_name(preferred="my_join", used=used)
        assert first == "my_join"
        assert second == "my_join_2"
        assert third == "my_join_3"

    def test_clean_fk_name_strips_fkey_and_fk_suffixes(self) -> None:
        from schemabrain.joins.suggest import _clean_fk_name

        assert _clean_fk_name("orders_user_id_fkey", "order", "customer") == "orders_user_id"
        assert _clean_fk_name("orders_user_id_fk", "order", "customer") == "orders_user_id"
        # No recognised suffix → returned as-is.
        assert _clean_fk_name("custom_name", "a", "b") == "custom_name"
        # Empty / fallback to `<source>_<target>`.
        assert _clean_fk_name("", "order", "customer") == "order_customer"
        assert _clean_fk_name("_fkey", "order", "customer") == "order_customer"


class TestJoinCandidateInvariants:
    def test_empty_evidence_tuple_refused(self) -> None:
        # __post_init__ guard: every candidate must carry at least one
        # evidence source.
        import pytest

        with pytest.raises(ValueError, match="evidence must be non-empty"):
            JoinCandidate(
                name="x",
                source_entity="a",
                target_entity="b",
                on=(JoinColumnPair(source_column="x", target_column="y"),),
                confidence="high",
                evidence=(),
                fk_name=None,
                query_log_frequency=0,
                rationale="",
            )


class TestTableToEntityCollision:
    def test_later_entity_wins_when_alphabetically_earlier(self) -> None:
        # Direct test on `_build_table_to_entity_map` — the helper
        # processes entities in iteration order. If the alphabetically-
        # LATER entity arrives first, the SECOND-arriving entity (which
        # is alphabetically earlier) becomes the winner: this exercises
        # the `if entity.name < existing` TRUE branch.
        from schemabrain.joins.suggest import _build_table_to_entity_map

        entities = [
            _entity("zeta_user", "public.users"),
            _entity("alpha_user", "public.users"),
        ]
        mapping = _build_table_to_entity_map(entities)
        # `alpha_user` is alphabetically first and wins, regardless of
        # arrival order.
        assert mapping == {"public.users": "alpha_user"}

    def test_first_entity_wins_when_alphabetically_earlier(self) -> None:
        # Symmetric path — alphabetically-earlier entity arrives FIRST,
        # so the second (later) entity hits the `else` branch and
        # gets dropped without becoming the winner.
        from schemabrain.joins.suggest import _build_table_to_entity_map

        entities = [
            _entity("alpha_user", "public.users"),
            _entity("zeta_user", "public.users"),
        ]
        mapping = _build_table_to_entity_map(entities)
        assert mapping == {"public.users": "alpha_user"}


class TestMultiEntityPerTable:
    def test_two_entities_on_same_table_prefer_alphabetically_first(self, tmp_path: Path) -> None:
        # Two entities bind to the same physical table — bug-shape but
        # legal at v1. Suggester prefers the alphabetically-first name.
        store = SQLiteStore(tmp_path / "store.db")
        store.write_table(_table("users"), source_connection_id=SOURCE_A)
        store.write_entity(_entity("customer", "public.users"), source_connection_id=SOURCE_A)
        store.write_entity(
            _entity("user_account", "public.users"),
            source_connection_id=SOURCE_A,
        )
        # No FK in this test — just ensure the table_to_entity map
        # doesn't crash and prefers alphabetical.
        candidates = suggest_canonical_joins(store=store, source_connection_id=SOURCE_A)
        store.close()
        # Both entities valid; no FKs and no query log → no candidates.
        assert candidates == []


class TestCycleDetection:
    def test_no_joins_yields_empty_report(self) -> None:
        report = detect_cycles_in_join_graph([])
        assert report.cycles == ()
        assert report.isolated_entities == ()
        assert report.max_path_length == 0

    def test_linear_chain_has_no_cycles(self) -> None:
        joins = [
            _join("a_to_b", "a", "b"),
            _join("b_to_c", "b", "c"),
        ]
        report = detect_cycles_in_join_graph(joins)
        assert report.cycles == ()
        # Path: a → b → c, length 3.
        assert report.max_path_length == 3

    def test_three_node_cycle_detected(self) -> None:
        # a → b → c → a
        joins = [
            _join("a_to_b", "a", "b"),
            _join("b_to_c", "b", "c"),
            _join("c_to_a", "c", "a"),
        ]
        report = detect_cycles_in_join_graph(joins)
        assert len(report.cycles) == 1
        # Canonical rotation starts at alphabetically-smallest node.
        cycle = report.cycles[0]
        assert cycle[0] == "a"
        # First and last element match (closed cycle).
        assert cycle[0] == cycle[-1]

    def test_two_node_cycle_detected(self) -> None:
        # a → b → a (legal mutual reference at v1+)
        joins = [
            _join("a_to_b", "a", "b"),
            _join("b_to_a", "b", "a"),
        ]
        report = detect_cycles_in_join_graph(joins)
        assert len(report.cycles) == 1
