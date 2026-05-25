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
from schemabrain.mcp.describe_column import describe_column_impl
from schemabrain.mcp.describe_table import describe_table_impl
from schemabrain.mcp.find_relevant_tables import find_relevant_tables_impl
from schemabrain.mcp.shapes import (
    ColumnDetail,
    ColumnNotFoundError,
    IncomingForeignKeyInfo,
    JoinEdge,
    JoinPath,
    SuggestJoinsResult,
    TableDescription,
    TableHit,
    TableNotFoundError,
)
from schemabrain.mcp.suggest_joins import suggest_joins_impl


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

    def test_keeps_highest_scoring_column_when_multiple_positive(self, tmp_path: Path) -> None:
        # A table with TWO positive-score columns. The aggregation must
        # keep the highest-scoring column as `best_column` and not
        # overwrite it with the second one. Exercises the
        # `(schema, table) not in best_per_table` short-circuit when
        # the table has already won — second/third columns for that
        # table are skipped, not allowed to clobber.
        store = SQLiteStore(tmp_path / "s.db")
        sid = "src1"
        store.write_table(
            Table(
                name="users",
                schema_name="public",
                columns=(
                    _column("email", table_name="users", ordinal_position=1),
                    _column("name", table_name="users", ordinal_position=2),
                ),
            ),
            source_connection_id=sid,
        )
        # email aligned with axis 0 → cosine 1.0.
        # name at 45-deg (axis 0+1) → cosine 1/sqrt(2) ≈ 0.707.
        store.write_table_embeddings(
            "public",
            "users",
            source_connection_id=sid,
            embeddings={
                "email": _emb(_unit(0)),
                "name": _emb((1.0, 1.0, 0.0, 0.0)),
            },
        )
        store.write_table_descriptions(
            "public",
            "users",
            source_connection_id=sid,
            descriptions={
                "email": ColumnDescription(
                    text="email of user",
                    model="m",
                    prompt_version="v",
                    input_tokens=1,
                    cached_input_tokens=0,
                    output_tokens=1,
                    cost_usd=0.0001,
                ),
                "name": ColumnDescription(
                    text="name of user",
                    model="m",
                    prompt_version="v",
                    input_tokens=1,
                    cached_input_tokens=0,
                    output_tokens=1,
                    cost_usd=0.0001,
                ),
            },
        )
        embedder = _AxisEmbedder({"q": _unit(0)})
        result = find_relevant_tables_impl(
            store=store, source_connection_id=sid, embedder=embedder, query="q", limit=10
        )
        assert len(result) == 1
        assert result[0].best_column == "email"  # higher score wins
        assert result[0].score == pytest.approx(1.0)
        store.close()

    def test_per_table_score_is_max_not_sum_or_mean(self, tmp_path: Path) -> None:
        # The docstring promises per-table score = MAX cosine across
        # columns (sparse-relevance heuristic). Pin that explicitly:
        # one table with two positive-scoring columns at distinct
        # magnitudes (0.8 and 0.6). The result MUST surface the MAX
        # (0.8), not the SUM (1.4), the MEAN (0.7), or anything else.
        # The other multiple-positive-columns test can't distinguish
        # MAX from accidentally-correct alternatives because the
        # winning column scores exactly 1.0.
        store = SQLiteStore(tmp_path / "s.db")
        sid = "src1"
        store.write_table(
            Table(
                name="users",
                schema_name="public",
                columns=(
                    _column("colA", table_name="users", ordinal_position=1),
                    _column("colB", table_name="users", ordinal_position=2),
                ),
            ),
            source_connection_id=sid,
        )
        # Stored vectors that produce specific dot-products against
        # a unit-axis-0 query. With normalized vectors aligned at
        # angles `acos(0.8)` and `acos(0.6)` to axis 0:
        #   v_A = (0.8, sqrt(1 - 0.64), 0, 0) → cosine == 0.8
        #   v_B = (0.6, sqrt(1 - 0.36), 0, 0) → cosine == 0.6
        import math as _math

        v_a = (0.8, _math.sqrt(1 - 0.64), 0.0, 0.0)
        v_b = (0.6, _math.sqrt(1 - 0.36), 0.0, 0.0)
        store.write_table_embeddings(
            "public",
            "users",
            source_connection_id=sid,
            embeddings={
                "colA": _emb(v_a),
                "colB": _emb(v_b),
            },
        )
        store.write_table_descriptions(
            "public",
            "users",
            source_connection_id=sid,
            descriptions={"colA": _desc("a"), "colB": _desc("b")},
        )
        embedder = _AxisEmbedder({"q": _unit(0)})
        result = find_relevant_tables_impl(
            store=store, source_connection_id=sid, embedder=embedder, query="q", limit=10
        )
        assert len(result) == 1
        assert result[0].best_column == "colA"
        # MAX = 0.8 (NOT SUM 1.4, NOT MEAN 0.7).
        assert result[0].score == pytest.approx(0.8, abs=1e-5)
        store.close()

    def test_zero_norm_query_returns_empty_without_storing_error(
        self, populated_store: SQLiteStore
    ) -> None:
        # Defensive parity with EmbeddingRetriever: a degenerate
        # zero-norm vector from the embedder must NOT propagate the
        # Store's ValueError to the MCP caller. Translate to [] at
        # this layer.
        embedder = _AxisEmbedder({"q": (0.0, 0.0, 0.0, 0.0)})
        result = find_relevant_tables_impl(
            store=populated_store,
            source_connection_id=SOURCE_ID,
            embedder=embedder,
            query="q",
            limit=10,
        )
        assert result == []

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


# ---------------------------------------------------------------------
# describe_column_impl
# ---------------------------------------------------------------------


class TestDescribeColumnImpl:
    def test_returns_typed_detail(self, populated_store: SQLiteStore) -> None:
        result = describe_column_impl(
            store=populated_store,
            source_connection_id=SOURCE_ID,
            qualified_name="public.users.email",
        )
        assert isinstance(result, ColumnDetail)
        assert result.qualified_name == "public.users.email"
        assert result.schema_name == "public"
        assert result.table_name == "users"
        assert result.name == "email"

    def test_carries_full_column_metadata(self, populated_store: SQLiteStore) -> None:
        # Mirrors what `describe_table[col]` would have returned, plus
        # description text from the LLM enrichment.
        result = describe_column_impl(
            store=populated_store,
            source_connection_id=SOURCE_ID,
            qualified_name="public.users.email",
        )
        assert result.data_type == "VARCHAR(255)"
        assert result.nullable is False
        assert result.is_primary_key is False
        assert result.description == "User's contact email address"

    def test_default_value_round_trips(self, populated_store: SQLiteStore) -> None:
        result = describe_column_impl(
            store=populated_store,
            source_connection_id=SOURCE_ID,
            qualified_name="public.orders.total_cents",
        )
        assert result.default == "0"

    def test_outgoing_foreign_key_listed(self, populated_store: SQLiteStore) -> None:
        # orders.user_id has an outgoing FK to public.users.id.
        result = describe_column_impl(
            store=populated_store,
            source_connection_id=SOURCE_ID,
            qualified_name="public.orders.user_id",
        )
        assert len(result.outgoing_foreign_keys) == 1
        fk = result.outgoing_foreign_keys[0]
        assert fk.name == "orders_user_id_fkey"
        assert fk.target_qualified_name == "public.users"
        assert fk.target_columns == ["id"]
        # The column itself MUST be in source_columns of its outgoing FK.
        assert "user_id" in fk.source_columns

    def test_outgoing_excludes_fks_that_dont_involve_this_column(
        self, populated_store: SQLiteStore
    ) -> None:
        # orders.id is the PK and doesn't own any outgoing FK — even
        # though orders has an FK on user_id, it must NOT show up here.
        result = describe_column_impl(
            store=populated_store,
            source_connection_id=SOURCE_ID,
            qualified_name="public.orders.id",
        )
        assert result.outgoing_foreign_keys == []

    def test_incoming_foreign_key_listed_on_pk_target(self, populated_store: SQLiteStore) -> None:
        # users.id is referenced by orders.user_id — the back-reference
        # must surface here.
        result = describe_column_impl(
            store=populated_store,
            source_connection_id=SOURCE_ID,
            qualified_name="public.users.id",
        )
        assert len(result.incoming_foreign_keys) == 1
        ifk = result.incoming_foreign_keys[0]
        assert isinstance(ifk, IncomingForeignKeyInfo)
        assert ifk.name == "orders_user_id_fkey"
        assert ifk.source_qualified_name == "public.orders"
        assert ifk.source_columns == ["user_id"]
        assert ifk.target_columns == ["id"]

    def test_incoming_empty_when_no_back_references(self, populated_store: SQLiteStore) -> None:
        # products.id has no FK pointing at it in this fixture.
        result = describe_column_impl(
            store=populated_store,
            source_connection_id=SOURCE_ID,
            qualified_name="public.products.id",
        )
        assert result.incoming_foreign_keys == []

    def test_token_estimate_is_positive(self, populated_store: SQLiteStore) -> None:
        result = describe_column_impl(
            store=populated_store,
            source_connection_id=SOURCE_ID,
            qualified_name="public.users.email",
        )
        assert result.token_estimate > 0

    def test_unknown_table_raises_table_not_found(self, populated_store: SQLiteStore) -> None:
        with pytest.raises(TableNotFoundError, match=r"public\.does_not_exist"):
            describe_column_impl(
                store=populated_store,
                source_connection_id=SOURCE_ID,
                qualified_name="public.does_not_exist.anything",
            )

    def test_unknown_column_in_known_table_raises_column_not_found(
        self, populated_store: SQLiteStore
    ) -> None:
        # The table exists but the column doesn't. Distinct error type
        # so callers can route the two cases differently if they want.
        with pytest.raises(ColumnNotFoundError, match=r"public\.users\.does_not_exist"):
            describe_column_impl(
                store=populated_store,
                source_connection_id=SOURCE_ID,
                qualified_name="public.users.does_not_exist",
            )

    @pytest.mark.parametrize(
        "bad",
        [
            "nodot",
            "one.dot",
            "a.b.c.d",
            ".empty.schema",
            "empty.table.",
            "empty..column",
        ],
    )
    def test_malformed_qualified_name_raises_value_error(
        self, bad: str, populated_store: SQLiteStore
    ) -> None:
        # Must be exactly schema.table.column. Wrong dot count or any
        # empty part is rejected.
        with pytest.raises(ValueError, match="qualified_name"):
            describe_column_impl(
                store=populated_store,
                source_connection_id=SOURCE_ID,
                qualified_name=bad,
            )

    def test_filters_by_source_connection_id(self, populated_store: SQLiteStore) -> None:
        with pytest.raises(TableNotFoundError):
            describe_column_impl(
                store=populated_store,
                source_connection_id="DOES-NOT-EXIST",
                qualified_name="public.users.email",
            )

    def test_column_with_no_description_returns_empty_string(self, tmp_path: Path) -> None:
        # --no-enrich path: column row exists but no description was
        # generated. Tool must not crash.
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
        result = describe_column_impl(
            store=store,
            source_connection_id=sid,
            qualified_name="public.widgets.id",
        )
        assert result.description == ""
        store.close()


# ---------------------------------------------------------------------
# suggest_joins_impl
# ---------------------------------------------------------------------


def _bare_table(name: str, cols: tuple[str, ...], fks: tuple[ForeignKey, ...] = ()) -> Table:
    """Build a tiny Table with one PK column + extra FK columns. Just
    enough structure for FK-graph tests; types and stats don't matter.
    """
    columns = tuple(
        _column(
            c,
            table_name=name,
            data_type="BIGINT",
            nullable=False,
            ordinal_position=i + 1,
            is_primary_key=(c == "id"),
        )
        for i, c in enumerate(cols)
    )
    return Table(name=name, schema_name="public", columns=columns, foreign_keys=fks)


@pytest.fixture
def fk_graph_store(tmp_path: Path) -> SQLiteStore:
    """A small FK graph for suggest_joins tests:

        users  ←(user_id)— orders —(product_id)→ products
                                       ↑
                            order_items —(order_id)→ orders
                            order_items —(product_id)→ products

        events (isolated, no FKs)

    Direct edges:
      orders → users
      orders → products
      order_items → orders
      order_items → products

    Multi-hop:
      users ↔ products via orders (2 hops)
      users ↔ order_items via orders (2 hops)
    """
    store = SQLiteStore(tmp_path / "fkgraph.db")
    sid = "src1"

    users = _bare_table("users", ("id", "email"))
    products = _bare_table("products", ("id", "sku"))
    orders = _bare_table(
        "orders",
        ("id", "user_id", "product_id"),
        fks=(
            ForeignKey(
                name="orders_user_id_fkey",
                source_columns=("user_id",),
                target_schema="public",
                target_table="users",
                target_columns=("id",),
            ),
            ForeignKey(
                name="orders_product_id_fkey",
                source_columns=("product_id",),
                target_schema="public",
                target_table="products",
                target_columns=("id",),
            ),
        ),
    )
    order_items = _bare_table(
        "order_items",
        ("id", "order_id", "product_id"),
        fks=(
            ForeignKey(
                name="order_items_order_id_fkey",
                source_columns=("order_id",),
                target_schema="public",
                target_table="orders",
                target_columns=("id",),
            ),
            ForeignKey(
                name="order_items_product_id_fkey",
                source_columns=("product_id",),
                target_schema="public",
                target_table="products",
                target_columns=("id",),
            ),
        ),
    )
    events = _bare_table("events", ("id",))

    for t in (users, products, orders, order_items, events):
        store.write_table(t, source_connection_id=sid)

    return store


class TestSuggestJoinsImpl:
    def test_returns_typed_result(self, fk_graph_store: SQLiteStore) -> None:
        result = suggest_joins_impl(
            store=fk_graph_store,
            source_connection_id=SOURCE_ID,
            tables=["public.orders", "public.users"],
        )
        assert isinstance(result, SuggestJoinsResult)
        assert all(isinstance(p, JoinPath) for p in result.paths)
        for p in result.paths:
            assert all(isinstance(e, JoinEdge) for e in p.edges)

    def test_direct_fk_one_hop(self, fk_graph_store: SQLiteStore) -> None:
        # orders has FK to users — should be a 1-hop direct edge.
        result = suggest_joins_impl(
            store=fk_graph_store,
            source_connection_id=SOURCE_ID,
            tables=["public.orders", "public.users"],
        )
        assert len(result.paths) == 1
        path = result.paths[0]
        assert path.start_qualified_name == "public.orders"
        assert path.end_qualified_name == "public.users"
        assert path.hops == 1
        assert len(path.edges) == 1
        edge = path.edges[0]
        assert edge.fk_name == "orders_user_id_fkey"
        assert edge.left_qualified_name == "public.orders"
        assert edge.left_columns == ["user_id"]
        assert edge.right_qualified_name == "public.users"
        assert edge.right_columns == ["id"]
        assert edge.confidence == 1.0
        assert edge.via == "foreign_key"
        assert path.confidence == 1.0
        assert result.unreachable_pairs == []

    def test_reverse_traversal_swaps_columns(self, fk_graph_store: SQLiteStore) -> None:
        # users → orders requires traversing the FK BACKWARD. Edge must
        # present columns from the path's perspective: left_columns are
        # on `users` (the target side of the FK), right_columns on
        # `orders` (the source side).
        result = suggest_joins_impl(
            store=fk_graph_store,
            source_connection_id=SOURCE_ID,
            tables=["public.users", "public.orders"],
        )
        assert len(result.paths) == 1
        path = result.paths[0]
        assert path.hops == 1
        edge = path.edges[0]
        assert edge.left_qualified_name == "public.users"
        assert edge.left_columns == ["id"]
        assert edge.right_qualified_name == "public.orders"
        assert edge.right_columns == ["user_id"]
        # FK identity is preserved — the join condition is identical.
        assert edge.fk_name == "orders_user_id_fkey"

    def test_multi_hop_path(self, fk_graph_store: SQLiteStore) -> None:
        # users ↔ products has no direct FK; shortest path is 2 hops via
        # orders (users→orders→products) OR via order_items+orders (3 hops).
        # BFS prefers the 2-hop. Two equally-short variants exist:
        #   users→orders→products via (orders.user_id→users, orders.product_id→products)
        # Plus via order_items+orders is 3 hops, longer.
        result = suggest_joins_impl(
            store=fk_graph_store,
            source_connection_id=SOURCE_ID,
            tables=["public.users", "public.products"],
        )
        assert len(result.paths) == 1
        path = result.paths[0]
        assert path.hops == 2
        assert path.start_qualified_name == "public.users"
        assert path.end_qualified_name == "public.products"
        # First edge connects users → some intermediate; second edge
        # connects intermediate → products.
        assert path.edges[0].left_qualified_name == "public.users"
        assert path.edges[-1].right_qualified_name == "public.products"
        # The intermediate table is the same on both edges.
        assert path.edges[0].right_qualified_name == path.edges[1].left_qualified_name

    def test_unreachable_pair_recorded(self, fk_graph_store: SQLiteStore) -> None:
        # `events` has no FKs to/from anything else.
        result = suggest_joins_impl(
            store=fk_graph_store,
            source_connection_id=SOURCE_ID,
            tables=["public.users", "public.events"],
        )
        assert result.paths == []
        assert result.unreachable_pairs == [["public.events", "public.users"]]

    def test_three_table_input_returns_pairwise_paths(self, fk_graph_store: SQLiteStore) -> None:
        # 3 tables → 3 unordered pairs. Each pair gets one shortest path
        # (or shows up in unreachable_pairs).
        result = suggest_joins_impl(
            store=fk_graph_store,
            source_connection_id=SOURCE_ID,
            tables=["public.users", "public.orders", "public.products"],
        )
        # Pairs: (users, orders) 1-hop, (orders, products) 1-hop,
        # (users, products) 2-hop. All reachable.
        assert len(result.paths) == 3
        assert result.unreachable_pairs == []
        # Sorted by hops asc → 1-hops first, then 2-hops.
        hops = [p.hops for p in result.paths]
        assert hops == sorted(hops)

    def test_paths_sorted_by_hops_then_qualified_name(self, fk_graph_store: SQLiteStore) -> None:
        # Three pairs: (orders, users) 1-hop, (orders, products) 1-hop,
        # (users, products) 2-hop. Within hops=1 tier, tiebreak is
        # alphabetical by (start, end) qualified name.
        result = suggest_joins_impl(
            store=fk_graph_store,
            source_connection_id=SOURCE_ID,
            tables=["public.users", "public.orders", "public.products"],
        )
        signatures = [(p.hops, p.start_qualified_name, p.end_qualified_name) for p in result.paths]
        assert signatures == sorted(signatures)

    def test_input_tables_normalized_to_unique_set(self, fk_graph_store: SQLiteStore) -> None:
        # Duplicate input tables shouldn't double-count pairs. Two
        # `public.users` entries collapse to one — but the pair count
        # still requires ≥2 distinct tables to produce any path.
        result = suggest_joins_impl(
            store=fk_graph_store,
            source_connection_id=SOURCE_ID,
            tables=["public.users", "public.orders", "public.users"],
        )
        # Effective pairs: just (users, orders).
        assert len(result.paths) == 1
        assert result.paths[0].hops == 1

    def test_fewer_than_two_distinct_tables_raises(self, fk_graph_store: SQLiteStore) -> None:
        with pytest.raises(ValueError, match="at least 2 distinct tables"):
            suggest_joins_impl(
                store=fk_graph_store,
                source_connection_id=SOURCE_ID,
                tables=["public.users"],
            )

    def test_empty_tables_raises(self, fk_graph_store: SQLiteStore) -> None:
        with pytest.raises(ValueError, match="at least 2 distinct tables"):
            suggest_joins_impl(
                store=fk_graph_store,
                source_connection_id=SOURCE_ID,
                tables=[],
            )

    def test_duplicate_only_tables_raise(self, fk_graph_store: SQLiteStore) -> None:
        # Two identical names collapse to one unique → still degenerate.
        with pytest.raises(ValueError, match="at least 2 distinct tables"):
            suggest_joins_impl(
                store=fk_graph_store,
                source_connection_id=SOURCE_ID,
                tables=["public.users", "public.users"],
            )

    def test_malformed_qualified_name_raises_from_parser(self, fk_graph_store: SQLiteStore) -> None:
        # Reuse the same parser as describe_table_impl — anything not
        # `schema.name` form is rejected uniformly.
        with pytest.raises(ValueError, match=r"schema\.name"):
            suggest_joins_impl(
                store=fk_graph_store,
                source_connection_id=SOURCE_ID,
                tables=["public.users", "no_schema_here"],
            )

    def test_missing_table_raises_table_not_found(self, fk_graph_store: SQLiteStore) -> None:
        with pytest.raises(TableNotFoundError, match=r"public\.ghost"):
            suggest_joins_impl(
                store=fk_graph_store,
                source_connection_id=SOURCE_ID,
                tables=["public.users", "public.ghost"],
            )

    def test_filters_by_source_connection_id(self, fk_graph_store: SQLiteStore) -> None:
        # All input tables exist under `src1`; querying a different
        # source treats them as missing.
        with pytest.raises(TableNotFoundError):
            suggest_joins_impl(
                store=fk_graph_store,
                source_connection_id="OTHER",
                tables=["public.users", "public.orders"],
            )

    def test_token_estimates_are_positive(self, fk_graph_store: SQLiteStore) -> None:
        result = suggest_joins_impl(
            store=fk_graph_store,
            source_connection_id=SOURCE_ID,
            tables=["public.users", "public.orders"],
        )
        assert result.token_estimate > 0
        assert result.paths[0].token_estimate > 0

    def test_max_hops_truncates_long_paths(self, fk_graph_store: SQLiteStore) -> None:
        # users ↔ products is 2 hops via orders. With max_hops=1, only
        # direct FKs are explored — pair becomes unreachable.
        result = suggest_joins_impl(
            store=fk_graph_store,
            source_connection_id=SOURCE_ID,
            tables=["public.users", "public.products"],
            max_hops=1,
        )
        assert result.paths == []
        assert result.unreachable_pairs == [["public.products", "public.users"]]

    def test_default_max_hops_reaches_five_hop_chain(self, tmp_path: Path) -> None:
        """A 5-hop FK chain `a→b→c→d→e→f` (the depth of the bundled
        e-commerce fixture's `users → categories` pair through the M:N
        junction) MUST be reachable at the default `max_hops`. Without
        this guard, the documented demo path returns `unreachable` on
        the fixture, which is a confusing first-touch experience for
        users evaluating SchemaBrain.
        """
        store = SQLiteStore(tmp_path / "deep.db")
        sid = "src1"

        # f has no outgoing FKs; each preceding table points one step closer.
        def _fk(name: str, src_col: str, target_table: str) -> ForeignKey:
            return ForeignKey(
                name=name,
                source_columns=(src_col,),
                target_schema="public",
                target_table=target_table,
                target_columns=("id",),
            )

        f = _bare_table("f", ("id",))
        e = _bare_table("e", ("id", "f_id"), fks=(_fk("e_f_fk", "f_id", "f"),))
        d = _bare_table("d", ("id", "e_id"), fks=(_fk("d_e_fk", "e_id", "e"),))
        c = _bare_table("c", ("id", "d_id"), fks=(_fk("c_d_fk", "d_id", "d"),))
        b = _bare_table("b", ("id", "c_id"), fks=(_fk("b_c_fk", "c_id", "c"),))
        a = _bare_table("a", ("id", "b_id"), fks=(_fk("a_b_fk", "b_id", "b"),))

        for t in (f, e, d, c, b, a):
            store.write_table(t, source_connection_id=sid)

        result = suggest_joins_impl(
            store=store,
            source_connection_id=sid,
            tables=["public.a", "public.f"],
        )
        assert result.unreachable_pairs == [], (
            "default max_hops must cover a 5-hop chain — current "
            "default truncates before the bundled e-commerce fixture's "
            "users→categories depth, breaking the documented demo path"
        )
        assert len(result.paths) == 1
        assert result.paths[0].hops == 5

    def test_max_hops_zero_or_negative_raises(self, fk_graph_store: SQLiteStore) -> None:
        for bad in (0, -1):
            with pytest.raises(ValueError, match="max_hops"):
                suggest_joins_impl(
                    store=fk_graph_store,
                    source_connection_id=SOURCE_ID,
                    tables=["public.users", "public.orders"],
                    max_hops=bad,
                )

    def test_composite_fk_columns_round_trip(self, tmp_path: Path) -> None:
        # A composite FK from events(member_org_id, member_user_id) →
        # org_members(org_id, user_id) must surface BOTH source and
        # target column lists in the JoinEdge — not just the first.
        store = SQLiteStore(tmp_path / "comp.db")
        sid = "src1"
        org_members = Table(
            name="org_members",
            schema_name="public",
            columns=(
                _column(
                    "org_id",
                    table_name="org_members",
                    data_type="BIGINT",
                    nullable=False,
                    ordinal_position=1,
                    is_primary_key=True,
                ),
                _column(
                    "user_id",
                    table_name="org_members",
                    data_type="BIGINT",
                    nullable=False,
                    ordinal_position=2,
                    is_primary_key=True,
                ),
            ),
        )
        events = Table(
            name="events",
            schema_name="public",
            columns=(
                _column(
                    "id",
                    table_name="events",
                    data_type="BIGINT",
                    nullable=False,
                    ordinal_position=1,
                    is_primary_key=True,
                ),
                _column(
                    "member_org_id",
                    table_name="events",
                    data_type="BIGINT",
                    nullable=False,
                    ordinal_position=2,
                ),
                _column(
                    "member_user_id",
                    table_name="events",
                    data_type="BIGINT",
                    nullable=False,
                    ordinal_position=3,
                ),
            ),
            foreign_keys=(
                ForeignKey(
                    name="events_member_fkey",
                    source_columns=("member_org_id", "member_user_id"),
                    target_schema="public",
                    target_table="org_members",
                    target_columns=("org_id", "user_id"),
                ),
            ),
        )
        store.write_table(org_members, source_connection_id=sid)
        store.write_table(events, source_connection_id=sid)

        result = suggest_joins_impl(
            store=store,
            source_connection_id=sid,
            tables=["public.events", "public.org_members"],
        )
        assert len(result.paths) == 1
        edge = result.paths[0].edges[0]
        assert edge.left_qualified_name == "public.events"
        assert edge.left_columns == ["member_org_id", "member_user_id"]
        assert edge.right_qualified_name == "public.org_members"
        assert edge.right_columns == ["org_id", "user_id"]
        store.close()

    def test_isolated_subgraph_pairs_all_unreachable(self, fk_graph_store: SQLiteStore) -> None:
        # `events` has no FKs anywhere — pairs against it are all
        # unreachable but pairs WITHIN the connected component still work.
        result = suggest_joins_impl(
            store=fk_graph_store,
            source_connection_id=SOURCE_ID,
            tables=["public.users", "public.orders", "public.events"],
        )
        # Pairs in input order: (users, orders) is 1-hop;
        # (users, events) and (orders, events) are unreachable. Path
        # orientation honors input order — `users` came first, so
        # the path starts at users. Unreachable pairs canonicalize
        # alphabetically.
        assert len(result.paths) == 1
        assert result.paths[0].start_qualified_name == "public.users"
        assert result.paths[0].end_qualified_name == "public.orders"
        assert result.unreachable_pairs == [
            ["public.events", "public.orders"],
            ["public.events", "public.users"],
        ]

    def test_self_referential_fk_does_not_break_graph(self, tmp_path: Path) -> None:
        # `employees.manager_id → employees.id` is a self-referential FK.
        # The adjacency builder adds two entries for `employees` pointing
        # back to itself; BFS must still terminate cleanly when the
        # graph contains a self-loop, and a path from a SECOND table to
        # `employees` must still resolve correctly.
        store = SQLiteStore(tmp_path / "self.db")
        sid = "src1"
        employees = Table(
            name="employees",
            schema_name="public",
            columns=(
                _column(
                    "id",
                    table_name="employees",
                    data_type="BIGINT",
                    nullable=False,
                    ordinal_position=1,
                    is_primary_key=True,
                ),
                _column(
                    "manager_id",
                    table_name="employees",
                    data_type="BIGINT",
                    nullable=True,
                    ordinal_position=2,
                ),
            ),
            foreign_keys=(
                ForeignKey(
                    name="employees_manager_id_fkey",
                    source_columns=("manager_id",),
                    target_schema="public",
                    target_table="employees",
                    target_columns=("id",),
                ),
            ),
        )
        tasks = Table(
            name="tasks",
            schema_name="public",
            columns=(
                _column(
                    "id",
                    table_name="tasks",
                    data_type="BIGINT",
                    nullable=False,
                    ordinal_position=1,
                    is_primary_key=True,
                ),
                _column(
                    "owner_id",
                    table_name="tasks",
                    data_type="BIGINT",
                    nullable=False,
                    ordinal_position=2,
                ),
            ),
            foreign_keys=(
                ForeignKey(
                    name="tasks_owner_id_fkey",
                    source_columns=("owner_id",),
                    target_schema="public",
                    target_table="employees",
                    target_columns=("id",),
                ),
            ),
        )
        store.write_table(employees, source_connection_id=sid)
        store.write_table(tasks, source_connection_id=sid)

        result = suggest_joins_impl(
            store=store,
            source_connection_id=sid,
            tables=["public.tasks", "public.employees"],
        )
        # Direct FK tasks → employees, 1 hop. Self-loop on employees
        # must not interfere.
        assert len(result.paths) == 1
        path = result.paths[0]
        assert path.hops == 1
        assert path.edges[0].fk_name == "tasks_owner_id_fkey"
        assert result.unreachable_pairs == []
        store.close()


class TestJoinPathInvariants:
    """The `JoinPath` model is part of the public MCP contract. The
    `hops == len(edges)` validator is the only piece of construction-
    time enforcement; pin it here so it can't be silently weakened.
    """

    def test_hops_must_match_edges_length(self) -> None:
        edge = JoinEdge(
            fk_name="x",
            left_qualified_name="public.a",
            left_columns=["id"],
            right_qualified_name="public.b",
            right_columns=["a_id"],
            confidence=1.0,
            via="foreign_key",
        )
        with pytest.raises(ValueError, match="hops"):
            JoinPath(
                start_qualified_name="public.a",
                end_qualified_name="public.b",
                hops=2,  # wrong — only 1 edge
                edges=[edge],
                confidence=1.0,
                token_estimate=1,
            )

    def test_well_formed_path_constructs_cleanly(self) -> None:
        edge = JoinEdge(
            fk_name="x",
            left_qualified_name="public.a",
            left_columns=["id"],
            right_qualified_name="public.b",
            right_columns=["a_id"],
            confidence=1.0,
            via="foreign_key",
        )
        path = JoinPath(
            start_qualified_name="public.a",
            end_qualified_name="public.b",
            hops=1,
            edges=[edge],
            confidence=1.0,
            token_estimate=1,
        )
        assert path.hops == 1
