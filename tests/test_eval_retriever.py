"""Tests for the eval retriever layer.

`Retriever` is a Protocol; the runner programs against it. Today's
concrete implementation is `KeywordRetriever`, a deliberately simple
keyword-overlap scorer that exists so we can produce baseline scores
before embedding-based retrieval lands in Week 4-5.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from schemabrain.core.description import ColumnDescription
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore
from schemabrain.eval.retriever import KeywordRetriever, Retriever


def _table(name: str, columns: tuple[str, ...]) -> Table:
    return Table(
        name=name,
        schema_name="public",
        columns=tuple(
            Column(
                name=c,
                table_name=name,
                schema_name="public",
                data_type="TEXT",
                nullable=False,
                ordinal_position=i + 1,
            )
            for i, c in enumerate(columns)
        ),
    )


def _desc(text: str) -> ColumnDescription:
    return ColumnDescription(
        text=text,
        model="fake-model",
        prompt_version="test",
        input_tokens=1,
        cached_input_tokens=0,
        output_tokens=1,
        cost_usd=0.0001,
    )


@pytest.fixture
def populated_store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "store.db")
    sid = "src1"
    users = _table("users", ("id", "email", "full_name"))
    orders = _table("orders", ("id", "user_id", "total_cents"))
    products = _table("products", ("id", "sku", "name"))
    store.write_table(users, source_connection_id=sid)
    store.write_table(orders, source_connection_id=sid)
    store.write_table(products, source_connection_id=sid)
    store.write_table_descriptions(
        "public",
        "users",
        source_connection_id=sid,
        descriptions={
            "id": _desc("Unique user identifier"),
            "email": _desc("Account email address used for login"),
            "full_name": _desc("Display name shown in the UI"),
        },
    )
    store.write_table_descriptions(
        "public",
        "orders",
        source_connection_id=sid,
        descriptions={
            "id": _desc("Order primary key"),
            "user_id": _desc("References the customer who placed the order"),
            "total_cents": _desc("Order grand total in cents"),
        },
    )
    store.write_table_descriptions(
        "public",
        "products",
        source_connection_id=sid,
        descriptions={
            "id": _desc("Product primary key"),
            "sku": _desc("Stock keeping unit code for inventory"),
            "name": _desc("Product display name"),
        },
    )
    return store


class TestRetrieverProtocol:
    def test_keyword_retriever_satisfies_protocol(self, tmp_path: Path) -> None:
        # KeywordRetriever must be assignable to a Retriever-typed name —
        # if Protocol structural typing breaks, this catches it.
        store = SQLiteStore(tmp_path / "s.db")
        r: Retriever = KeywordRetriever(store=store, source_connection_id="src1")
        assert hasattr(r, "retrieve")


class TestKeywordRetriever:
    def test_returns_empty_when_store_has_no_tables(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "s.db")
        r = KeywordRetriever(store=store, source_connection_id="src1")
        assert r.retrieve("anything", limit=10) == []

    def test_returns_empty_when_no_keywords_overlap(self, populated_store: SQLiteStore) -> None:
        r = KeywordRetriever(store=populated_store, source_connection_id="src1")
        assert r.retrieve("xyzzy quux nonsense", limit=10) == []

    def test_returns_empty_when_query_is_only_stopwords(self, populated_store: SQLiteStore) -> None:
        # All tokens get filtered as stopwords/short → no signal.
        r = KeywordRetriever(store=populated_store, source_connection_id="src1")
        assert r.retrieve("the and for", limit=10) == []

    def test_finds_table_by_description_keyword(self, populated_store: SQLiteStore) -> None:
        r = KeywordRetriever(store=populated_store, source_connection_id="src1")
        # "email" hits public.users via the email column description.
        result = r.retrieve("Where do we store user emails?", limit=10)
        assert "public.users" in result

    def test_finds_table_by_table_name_alone(self, populated_store: SQLiteStore) -> None:
        r = KeywordRetriever(store=populated_store, source_connection_id="src1")
        result = r.retrieve("show me orders", limit=10)
        assert "public.orders" in result

    def test_finds_table_by_column_name(self, populated_store: SQLiteStore) -> None:
        r = KeywordRetriever(store=populated_store, source_connection_id="src1")
        # `sku` is a column name on products; not in any description text.
        result = r.retrieve("look up sku", limit=10)
        assert "public.products" in result

    def test_ranks_higher_overlap_first(self, populated_store: SQLiteStore) -> None:
        r = KeywordRetriever(store=populated_store, source_connection_id="src1")
        # "user email login account" hits users (email/account/login all in
        # users descriptions) much harder than orders/products.
        result = r.retrieve("user email login account", limit=10)
        assert result[0] == "public.users"

    def test_respects_limit(self, populated_store: SQLiteStore) -> None:
        r = KeywordRetriever(store=populated_store, source_connection_id="src1")
        # `id` hits all 3 tables (it's a column name on each). Limit to 2.
        result = r.retrieve("primary identifier id", limit=2)
        assert len(result) == 2

    def test_zero_limit_returns_empty(self, populated_store: SQLiteStore) -> None:
        r = KeywordRetriever(store=populated_store, source_connection_id="src1")
        assert r.retrieve("user email", limit=0) == []

    def test_negative_limit_returns_empty(self, populated_store: SQLiteStore) -> None:
        r = KeywordRetriever(store=populated_store, source_connection_id="src1")
        assert r.retrieve("user email", limit=-1) == []

    def test_filters_by_source_connection_id(self, populated_store: SQLiteStore) -> None:
        # Tables under a different source_connection_id must be invisible.
        r = KeywordRetriever(store=populated_store, source_connection_id="src-DOES-NOT-EXIST")
        assert r.retrieve("user email", limit=10) == []

    def test_underscore_words_are_split(self, populated_store: SQLiteStore) -> None:
        # `user_id` should match a query about "user" — i.e., underscores
        # don't form an opaque token that hides keywords inside compound
        # column names.
        r = KeywordRetriever(store=populated_store, source_connection_id="src1")
        result = r.retrieve("customer placed", limit=10)
        # "customer" and "placed" both appear in orders.user_id description.
        assert "public.orders" in result

    def test_case_insensitive(self, populated_store: SQLiteStore) -> None:
        r = KeywordRetriever(store=populated_store, source_connection_id="src1")
        upper = r.retrieve("EMAIL ADDRESS", limit=10)
        lower = r.retrieve("email address", limit=10)
        assert upper == lower

    def test_partially_enriched_table_still_matches_unenriched_column_names(
        self, tmp_path: Path
    ) -> None:
        # Regression: when a table has descriptions for SOME columns but
        # not all (e.g., the cap fired mid-table on a previous run, or
        # only N columns happened to enrich), the un-enriched column
        # names must still be in the corpus. Otherwise a query that hits
        # ONLY an un-enriched column name silently misses.
        store = SQLiteStore(tmp_path / "s.db")
        sid = "src1"
        store.write_table(
            _table("widgets", ("id", "color", "obscure_legacy_field")),
            source_connection_id=sid,
        )
        # Enrich only 2 of 3 columns.
        store.write_table_descriptions(
            "public",
            "widgets",
            source_connection_id=sid,
            descriptions={
                "id": _desc("widget primary key"),
                "color": _desc("widget paint color"),
            },
        )
        r = KeywordRetriever(store=store, source_connection_id=sid)
        # Query hits only the un-enriched column's name.
        result = r.retrieve("obscure legacy field", limit=10)
        assert result == ["public.widgets"]

    def test_ignores_table_with_no_descriptions(self, tmp_path: Path) -> None:
        # If a table is indexed but its descriptions weren't generated
        # (e.g. --no-enrich path), the retriever can still match on
        # table name + column names alone — it must not crash.
        store = SQLiteStore(tmp_path / "s.db")
        sid = "src1"
        store.write_table(_table("widgets", ("id", "color")), source_connection_id=sid)
        r = KeywordRetriever(store=store, source_connection_id=sid)
        result = r.retrieve("widgets color", limit=10)
        assert result == ["public.widgets"]

    def test_alternate_schema_is_supported(self, tmp_path: Path) -> None:
        # Tables in non-public schemas still produce qualified names like
        # `analytics.events`.
        store = SQLiteStore(tmp_path / "s.db")
        sid = "src1"
        t = Table(
            name="events",
            schema_name="analytics",
            columns=(
                Column(
                    name="id",
                    table_name="events",
                    schema_name="analytics",
                    data_type="TEXT",
                    nullable=False,
                    ordinal_position=1,
                ),
            ),
        )
        store.write_table(t, source_connection_id=sid)
        r = KeywordRetriever(store=store, source_connection_id=sid)
        assert r.retrieve("events", limit=10) == ["analytics.events"]
