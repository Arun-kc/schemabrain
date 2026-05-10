"""Tests for the MCP tool implementations.

These exercise the pure-function impls in `mcp/tools.py` directly —
no FastMCP, no transport, no async. The smoke test in
`test_mcp_server.py` covers the full FastMCP integration once.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from schemabrain.core.description import ColumnDescription
from schemabrain.core.embedding import ColumnEmbedding
from schemabrain.core.models import Column, ForeignKey, Table
from schemabrain.core.store import SQLiteStore
from schemabrain.mcp.tools import (
    TableDescription,
    TableHit,
    TableNotFoundError,
    describe_table_impl,
    find_relevant_tables_impl,
)


def _column(
    name: str,
    *,
    table_name: str,
    schema_name: str = "public",
    data_type: str = "TEXT",
    nullable: bool = True,
    ordinal_position: int = 1,
    default: str | None = None,
    is_primary_key: bool = False,
) -> Column:
    return Column(
        name=name,
        table_name=table_name,
        schema_name=schema_name,
        data_type=data_type,
        nullable=nullable,
        ordinal_position=ordinal_position,
        default=default,
        is_primary_key=is_primary_key,
    )


def _desc(text: str) -> ColumnDescription:
    return ColumnDescription(
        text=text,
        model="claude-haiku-4-5",
        prompt_version="v",
        input_tokens=1,
        cached_input_tokens=0,
        output_tokens=1,
        cost_usd=0.0001,
    )


_DIM = 4


def _unit(idx: int) -> tuple[float, ...]:
    return tuple(1.0 if i == idx else 0.0 for i in range(_DIM))


def _emb(vec: tuple[float, ...]) -> ColumnEmbedding:
    return ColumnEmbedding(vector=vec, model="test-emb", dimension=len(vec))


class _AxisEmbedder:
    """Test embedder mapping query strings to pre-scripted axis vectors."""

    model_name = "test-emb"
    dimension = _DIM

    def __init__(self, script: dict[str, tuple[float, ...]]) -> None:
        self._script = script
        self.calls: list[str] = []

    def embed(self, text: str) -> tuple[float, ...]:
        self.calls.append(text)
        if text not in self._script:
            raise KeyError(f"unscripted query: {text!r}")
        return self._script[text]


@pytest.fixture
def populated_store(tmp_path: Path) -> SQLiteStore:
    """Three tables with descriptions, embeddings, and one FK."""
    store = SQLiteStore(tmp_path / "store.db")
    sid = "src1"

    users = Table(
        name="users",
        schema_name="public",
        columns=(
            _column(
                "id",
                table_name="users",
                data_type="BIGINT",
                nullable=False,
                ordinal_position=1,
                is_primary_key=True,
            ),
            _column(
                "email",
                table_name="users",
                data_type="VARCHAR(255)",
                nullable=False,
                ordinal_position=2,
            ),
        ),
    )
    orders = Table(
        name="orders",
        schema_name="public",
        columns=(
            _column(
                "id",
                table_name="orders",
                data_type="BIGINT",
                nullable=False,
                ordinal_position=1,
                is_primary_key=True,
            ),
            _column(
                "user_id",
                table_name="orders",
                data_type="BIGINT",
                nullable=False,
                ordinal_position=2,
            ),
            _column(
                "total_cents",
                table_name="orders",
                data_type="INTEGER",
                nullable=False,
                ordinal_position=3,
                default="0",
            ),
        ),
        foreign_keys=(
            ForeignKey(
                name="orders_user_id_fkey",
                source_columns=("user_id",),
                target_schema="public",
                target_table="users",
                target_columns=("id",),
            ),
        ),
    )
    products = Table(
        name="products",
        schema_name="public",
        columns=(
            _column(
                "id",
                table_name="products",
                data_type="BIGINT",
                nullable=False,
                ordinal_position=1,
                is_primary_key=True,
            ),
            _column(
                "sku",
                table_name="products",
                data_type="TEXT",
                nullable=False,
                ordinal_position=2,
            ),
        ),
    )
    for t in (users, orders, products):
        store.write_table(t, source_connection_id=sid)

    store.write_table_descriptions(
        "public",
        "users",
        source_connection_id=sid,
        descriptions={
            "id": _desc("Numeric primary key for the user row"),
            "email": _desc("User's contact email address"),
        },
    )
    store.write_table_descriptions(
        "public",
        "orders",
        source_connection_id=sid,
        descriptions={
            "id": _desc("Order primary key"),
            "user_id": _desc("Customer who placed the order"),
            "total_cents": _desc("Order total in cents"),
        },
    )
    store.write_table_descriptions(
        "public",
        "products",
        source_connection_id=sid,
        descriptions={
            "id": _desc("Product primary key"),
            "sku": _desc("Inventory SKU code"),
        },
    )

    # Embeddings: each table has one "winning" column on a unique axis.
    store.write_table_embeddings(
        "public",
        "users",
        source_connection_id=sid,
        embeddings={
            "id": _emb(_unit(3)),
            "email": _emb(_unit(0)),  # winning
        },
    )
    store.write_table_embeddings(
        "public",
        "orders",
        source_connection_id=sid,
        embeddings={
            "id": _emb(_unit(3)),
            "user_id": _emb(_unit(1)),  # winning
            "total_cents": _emb(_unit(3)),
        },
    )
    store.write_table_embeddings(
        "public",
        "products",
        source_connection_id=sid,
        embeddings={
            "id": _emb(_unit(3)),
            "sku": _emb(_unit(2)),  # winning
        },
    )
    return store


SOURCE_ID = "src1"


# ---------------------------------------------------------------------
# find_relevant_tables_impl
# ---------------------------------------------------------------------


class TestFindRelevantTablesImpl:
    def test_returns_typed_table_hits(self, populated_store: SQLiteStore) -> None:
        embedder = _AxisEmbedder({"emails please": _unit(0)})
        result = find_relevant_tables_impl(
            store=populated_store,
            source_connection_id=SOURCE_ID,
            embedder=embedder,
            query="emails please",
            limit=10,
        )
        assert all(isinstance(h, TableHit) for h in result)

    def test_axis_0_query_surfaces_users_with_email_as_best_column(
        self, populated_store: SQLiteStore
    ) -> None:
        embedder = _AxisEmbedder({"q": _unit(0)})
        result = find_relevant_tables_impl(
            store=populated_store,
            source_connection_id=SOURCE_ID,
            embedder=embedder,
            query="q",
            limit=10,
        )
        assert result[0].qualified_name == "public.users"
        assert result[0].best_column == "email"
        assert result[0].best_column_description == "User's contact email address"
        assert result[0].score == pytest.approx(1.0)

    def test_axis_1_surfaces_orders_via_user_id(self, populated_store: SQLiteStore) -> None:
        embedder = _AxisEmbedder({"q": _unit(1)})
        result = find_relevant_tables_impl(
            store=populated_store,
            source_connection_id=SOURCE_ID,
            embedder=embedder,
            query="q",
            limit=10,
        )
        assert result[0].qualified_name == "public.orders"
        assert result[0].best_column == "user_id"

    def test_drops_zero_score_tables(self, populated_store: SQLiteStore) -> None:
        # Pure axis-0 query → only users matches (score 1.0); orders and
        # products score 0.0 and must be dropped.
        embedder = _AxisEmbedder({"q": _unit(0)})
        result = find_relevant_tables_impl(
            store=populated_store,
            source_connection_id=SOURCE_ID,
            embedder=embedder,
            query="q",
            limit=10,
        )
        assert [h.qualified_name for h in result] == ["public.users"]

    def test_respects_limit(self, populated_store: SQLiteStore) -> None:
        # All three axes weighted equally → all 3 tables tie at score 1.0.
        # Sort key is (-score, schema, table), so deterministic order is
        # orders, products, users. Limit=2 should keep the first two.
        embedder = _AxisEmbedder({"all": (1.0, 1.0, 1.0, 0.0)})
        result = find_relevant_tables_impl(
            store=populated_store,
            source_connection_id=SOURCE_ID,
            embedder=embedder,
            query="all",
            limit=2,
        )
        assert [h.qualified_name for h in result] == ["public.orders", "public.products"]

    def test_zero_limit_returns_empty(self, populated_store: SQLiteStore) -> None:
        embedder = _AxisEmbedder({})
        result = find_relevant_tables_impl(
            store=populated_store,
            source_connection_id=SOURCE_ID,
            embedder=embedder,
            query="q",
            limit=0,
        )
        assert result == []
        assert embedder.calls == []  # no embedder call when limit is degenerate

    def test_empty_query_returns_empty_without_embedder_call(
        self, populated_store: SQLiteStore
    ) -> None:
        embedder = _AxisEmbedder({})
        assert (
            find_relevant_tables_impl(
                store=populated_store,
                source_connection_id=SOURCE_ID,
                embedder=embedder,
                query="",
                limit=10,
            )
            == []
        )
        assert (
            find_relevant_tables_impl(
                store=populated_store,
                source_connection_id=SOURCE_ID,
                embedder=embedder,
                query="   \n\t  ",
                limit=10,
            )
            == []
        )
        assert embedder.calls == []

    def test_embedder_called_exactly_once_per_query(self, populated_store: SQLiteStore) -> None:
        embedder = _AxisEmbedder({"q": _unit(0)})
        find_relevant_tables_impl(
            store=populated_store,
            source_connection_id=SOURCE_ID,
            embedder=embedder,
            query="q",
            limit=10,
        )
        assert embedder.calls == ["q"]

    def test_token_estimate_is_positive_and_finite(self, populated_store: SQLiteStore) -> None:
        embedder = _AxisEmbedder({"q": _unit(0)})
        hit = find_relevant_tables_impl(
            store=populated_store,
            source_connection_id=SOURCE_ID,
            embedder=embedder,
            query="q",
            limit=10,
        )[0]
        assert hit.token_estimate > 0
        assert hit.token_estimate < 10_000

    def test_filters_by_source_connection_id(self, populated_store: SQLiteStore) -> None:
        embedder = _AxisEmbedder({"q": _unit(0)})
        assert (
            find_relevant_tables_impl(
                store=populated_store,
                source_connection_id="DOES-NOT-EXIST",
                embedder=embedder,
                query="q",
                limit=10,
            )
            == []
        )

    def test_table_without_embeddings_silently_skipped(self, tmp_path: Path) -> None:
        # If a table is indexed without embeddings (--no-embed or pre-4-B
        # store), it should NOT crash retrieval — just not show up.
        store = SQLiteStore(tmp_path / "s.db")
        sid = "src1"
        store.write_table(
            Table(
                name="users",
                schema_name="public",
                columns=(_column("email", table_name="users", ordinal_position=1),),
            ),
            source_connection_id=sid,
        )
        store.write_table(
            Table(
                name="orders",
                schema_name="public",
                columns=(_column("id", table_name="orders", ordinal_position=1),),
            ),
            source_connection_id=sid,
        )
        # only embed users
        store.write_table_embeddings(
            "public",
            "users",
            source_connection_id=sid,
            embeddings={"email": _emb(_unit(0))},
        )
        embedder = _AxisEmbedder({"q": _unit(0)})
        result = find_relevant_tables_impl(
            store=store, source_connection_id=sid, embedder=embedder, query="q", limit=10
        )
        assert [h.qualified_name for h in result] == ["public.users"]
        store.close()

    def test_best_column_description_empty_when_no_description_for_winning_column(
        self, tmp_path: Path
    ) -> None:
        # Edge case: a column has an embedding but no description (shouldn't
        # happen in normal indexer flow but the store allows it). The hit
        # must still come back with a sensible empty `best_column_description`,
        # not crash.
        store = SQLiteStore(tmp_path / "s.db")
        sid = "src1"
        store.write_table(
            Table(
                name="users",
                schema_name="public",
                columns=(_column("email", table_name="users", ordinal_position=1),),
            ),
            source_connection_id=sid,
        )
        store.write_table_embeddings(
            "public",
            "users",
            source_connection_id=sid,
            embeddings={"email": _emb(_unit(0))},
        )
        embedder = _AxisEmbedder({"q": _unit(0)})
        result = find_relevant_tables_impl(
            store=store, source_connection_id=sid, embedder=embedder, query="q", limit=10
        )
        assert result[0].qualified_name == "public.users"
        assert result[0].best_column == "email"
        assert result[0].best_column_description == ""
        store.close()


# ---------------------------------------------------------------------
# describe_table_impl
# ---------------------------------------------------------------------


class TestDescribeTableImpl:
    def test_returns_typed_description(self, populated_store: SQLiteStore) -> None:
        result = describe_table_impl(
            store=populated_store,
            source_connection_id=SOURCE_ID,
            qualified_name="public.users",
        )
        assert isinstance(result, TableDescription)
        assert result.qualified_name == "public.users"
        assert result.schema_name == "public"
        assert result.name == "users"

    def test_columns_include_name_type_nullable_pk_description(
        self, populated_store: SQLiteStore
    ) -> None:
        result = describe_table_impl(
            store=populated_store,
            source_connection_id=SOURCE_ID,
            qualified_name="public.users",
        )
        col_by_name = {c.name: c for c in result.columns}
        assert set(col_by_name.keys()) == {"id", "email"}
        assert col_by_name["id"].data_type == "BIGINT"
        assert col_by_name["id"].nullable is False
        assert col_by_name["id"].is_primary_key is True
        assert col_by_name["id"].description == "Numeric primary key for the user row"
        assert col_by_name["email"].data_type == "VARCHAR(255)"
        assert col_by_name["email"].is_primary_key is False
        assert col_by_name["email"].description == "User's contact email address"

    def test_columns_in_ordinal_position_order(self, populated_store: SQLiteStore) -> None:
        result = describe_table_impl(
            store=populated_store,
            source_connection_id=SOURCE_ID,
            qualified_name="public.orders",
        )
        # orders columns: id (1), user_id (2), total_cents (3)
        assert [c.name for c in result.columns] == ["id", "user_id", "total_cents"]

    def test_default_value_round_trips(self, populated_store: SQLiteStore) -> None:
        result = describe_table_impl(
            store=populated_store,
            source_connection_id=SOURCE_ID,
            qualified_name="public.orders",
        )
        col = next(c for c in result.columns if c.name == "total_cents")
        assert col.default == "0"

    def test_foreign_keys_exposed_with_target_qualified_name(
        self, populated_store: SQLiteStore
    ) -> None:
        result = describe_table_impl(
            store=populated_store,
            source_connection_id=SOURCE_ID,
            qualified_name="public.orders",
        )
        assert len(result.foreign_keys) == 1
        fk = result.foreign_keys[0]
        assert fk.name == "orders_user_id_fkey"
        assert fk.source_columns == ["user_id"]
        assert fk.target_qualified_name == "public.users"
        assert fk.target_columns == ["id"]

    def test_table_with_no_descriptions_returns_empty_strings(self, tmp_path: Path) -> None:
        # A `--no-enrich` store has columns but no description rows; the
        # tool must still serve a description envelope with empty desc fields.
        store = SQLiteStore(tmp_path / "s.db")
        sid = "src1"
        store.write_table(
            Table(
                name="widgets",
                schema_name="public",
                columns=(
                    _column(
                        "id",
                        table_name="widgets",
                        data_type="BIGINT",
                        nullable=False,
                        ordinal_position=1,
                        is_primary_key=True,
                    ),
                ),
            ),
            source_connection_id=sid,
        )
        result = describe_table_impl(
            store=store,
            source_connection_id=sid,
            qualified_name="public.widgets",
        )
        assert result.columns[0].description == ""
        store.close()

    def test_unknown_table_raises_table_not_found(self, populated_store: SQLiteStore) -> None:
        with pytest.raises(TableNotFoundError, match=r"public\.does_not_exist"):
            describe_table_impl(
                store=populated_store,
                source_connection_id=SOURCE_ID,
                qualified_name="public.does_not_exist",
            )

    def test_malformed_qualified_name_raises_value_error(
        self, populated_store: SQLiteStore
    ) -> None:
        # No dot, multiple dots, empty parts — all malformed.
        for bad in ("nodot", "a.b.c", ".missing_schema", "missing_name."):
            with pytest.raises(ValueError, match="qualified_name"):
                describe_table_impl(
                    store=populated_store,
                    source_connection_id=SOURCE_ID,
                    qualified_name=bad,
                )

    def test_token_estimate_is_positive(self, populated_store: SQLiteStore) -> None:
        result = describe_table_impl(
            store=populated_store,
            source_connection_id=SOURCE_ID,
            qualified_name="public.users",
        )
        assert result.token_estimate > 0

    def test_filters_by_source_connection_id(self, populated_store: SQLiteStore) -> None:
        with pytest.raises(TableNotFoundError):
            describe_table_impl(
                store=populated_store,
                source_connection_id="DOES-NOT-EXIST",
                qualified_name="public.users",
            )

    def test_table_with_no_foreign_keys_returns_empty_list(
        self, populated_store: SQLiteStore
    ) -> None:
        result = describe_table_impl(
            store=populated_store,
            source_connection_id=SOURCE_ID,
            qualified_name="public.products",
        )
        assert result.foreign_keys == []


class TestCosineHelper:
    """The MCP tools have their own copy of cosine (small duplication
    vs polluting the eval Retriever Protocol). Pin its edge cases here.
    """

    def test_dimension_mismatch_raises_value_error(self) -> None:
        from schemabrain.mcp.tools import _cosine

        with pytest.raises(ValueError, match="dimension mismatch"):
            _cosine((1.0, 0.0), (1.0, 0.0, 0.0))

    def test_zero_norm_returns_zero(self) -> None:
        from schemabrain.mcp.tools import _cosine

        assert _cosine((0.0, 0.0, 0.0), (1.0, 0.0, 0.0)) == 0.0
        assert _cosine((1.0, 0.0, 0.0), (0.0, 0.0, 0.0)) == 0.0
        assert _cosine((0.0, 0.0, 0.0), (0.0, 0.0, 0.0)) == 0.0

    def test_parallel_vectors_score_one(self) -> None:
        from schemabrain.mcp.tools import _cosine

        assert _cosine((1.0, 0.0, 0.0), (1.0, 0.0, 0.0)) == pytest.approx(1.0)

    def test_orthogonal_vectors_score_zero(self) -> None:
        from schemabrain.mcp.tools import _cosine

        assert _cosine((1.0, 0.0, 0.0), (0.0, 1.0, 0.0)) == pytest.approx(0.0)
