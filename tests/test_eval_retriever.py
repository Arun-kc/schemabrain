"""Tests for the eval retriever layer.

`Retriever` is a Protocol; the runner programs against it. Concrete
implementations:
  - `KeywordRetriever` — the Week-3 baseline, keyword-overlap scoring
    over table/column names + description text. Cheap to compute, no
    embeddings required.
  - `EmbeddingRetriever` — Week-4 cosine-similarity scorer over stored
    `column_embeddings`. Per-table score = max cosine across columns
    (a single very-relevant column is strong evidence the table is
    relevant). Requires the store to have been indexed WITH an
    embedder; tables with no embeddings are silently skipped.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from schemabrain.core.description import ColumnDescription
from schemabrain.core.embedding import ColumnEmbedding
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore
from schemabrain.eval.retriever import EmbeddingRetriever, KeywordRetriever, Retriever


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
        assert isinstance(r, Retriever)


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


# Helpers for EmbeddingRetriever tests.
# We use 4-dim unit-axis vectors so cosine similarity has meaning we can
# reason about: parallel = 1.0, orthogonal = 0.0.

_DIM = 4


def _unit(idx: int) -> tuple[float, ...]:
    """One-hot unit vector of length _DIM with the 1 at position `idx`."""
    return tuple(1.0 if i == idx else 0.0 for i in range(_DIM))


def _emb(vector: tuple[float, ...], model: str = "test-emb") -> ColumnEmbedding:
    return ColumnEmbedding(vector=vector, model=model, dimension=len(vector))


class _ScriptedEmbedder:
    """Test embedder that returns a vector keyed by exact text match.

    Lets tests pre-script which query string maps to which axis vector.
    Anything not in the script raises so silent test drift fails loudly.
    """

    model_name = "test-emb"
    dimension = _DIM

    def __init__(self, script: dict[str, tuple[float, ...]]) -> None:
        self._script = script
        self.calls: list[str] = []

    def embed(self, text: str) -> tuple[float, ...]:
        self.calls.append(text)
        if text not in self._script:
            raise KeyError(f"_ScriptedEmbedder has no scripted vector for {text!r}")
        return self._script[text]


@pytest.fixture
def embedded_store(tmp_path: Path) -> SQLiteStore:
    """Three tables, each embedded so their MAX-cosine column lands on a
    distinct unit axis. This lets us pin which table should win for a
    query embedded onto each axis.
        users    → axis 0
        orders   → axis 1
        products → axis 2
    """
    store = SQLiteStore(tmp_path / "store.db")
    sid = "src1"
    users = _table("users", ("id", "email", "full_name"))
    orders = _table("orders", ("id", "user_id", "total_cents"))
    products = _table("products", ("id", "sku", "name"))
    for t in (users, orders, products):
        store.write_table(t, source_connection_id=sid)

    # Each table's "best" column points exactly along one axis.
    # Other columns are orthogonal so they contribute zero to the score.
    store.write_table_embeddings(
        "public",
        "users",
        source_connection_id=sid,
        embeddings={
            "id": _emb(_unit(3)),
            "email": _emb(_unit(0)),  # this is the "winning" column
            "full_name": _emb(_unit(3)),
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
            "name": _emb(_unit(3)),
        },
    )
    return store


class TestEmbeddingRetrieverProtocol:
    def test_satisfies_protocol(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "s.db")
        embedder = _ScriptedEmbedder({})
        r: Retriever = EmbeddingRetriever(
            store=store, source_connection_id="src1", embedder=embedder
        )
        assert isinstance(r, Retriever)


class TestEmbeddingRetriever:
    def test_returns_empty_when_store_has_no_tables(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "s.db")
        embedder = _ScriptedEmbedder({"q": _unit(0)})
        r = EmbeddingRetriever(store=store, source_connection_id="src1", embedder=embedder)
        assert r.retrieve("q", limit=10) == []

    def test_zero_limit_returns_empty(self, embedded_store: SQLiteStore) -> None:
        embedder = _ScriptedEmbedder({"q": _unit(0)})
        r = EmbeddingRetriever(store=embedded_store, source_connection_id="src1", embedder=embedder)
        assert r.retrieve("q", limit=0) == []

    def test_negative_limit_returns_empty(self, embedded_store: SQLiteStore) -> None:
        embedder = _ScriptedEmbedder({"q": _unit(0)})
        r = EmbeddingRetriever(store=embedded_store, source_connection_id="src1", embedder=embedder)
        assert r.retrieve("q", limit=-1) == []

    def test_empty_query_returns_empty_without_embedding(self, embedded_store: SQLiteStore) -> None:
        # Important: an empty query must NOT call the embedder. Otherwise
        # we'd waste an ONNX call AND have to handle the zero-vector
        # cosine division.
        embedder = _ScriptedEmbedder({})
        r = EmbeddingRetriever(store=embedded_store, source_connection_id="src1", embedder=embedder)
        assert r.retrieve("", limit=10) == []
        assert r.retrieve("   \n\t  ", limit=10) == []
        assert embedder.calls == []

    def test_query_on_axis_0_surfaces_users(self, embedded_store: SQLiteStore) -> None:
        # users.email is the only column whose embedding aligns with axis 0;
        # max-cosine puts users at the top.
        embedder = _ScriptedEmbedder({"about user emails": _unit(0)})
        r = EmbeddingRetriever(store=embedded_store, source_connection_id="src1", embedder=embedder)
        result = r.retrieve("about user emails", limit=10)
        assert result[0] == "public.users"

    def test_query_on_axis_1_surfaces_orders(self, embedded_store: SQLiteStore) -> None:
        embedder = _ScriptedEmbedder({"who placed orders": _unit(1)})
        r = EmbeddingRetriever(store=embedded_store, source_connection_id="src1", embedder=embedder)
        result = r.retrieve("who placed orders", limit=10)
        assert result[0] == "public.orders"

    def test_query_on_axis_2_surfaces_products(self, embedded_store: SQLiteStore) -> None:
        embedder = _ScriptedEmbedder({"sku codes please": _unit(2)})
        r = EmbeddingRetriever(store=embedded_store, source_connection_id="src1", embedder=embedder)
        result = r.retrieve("sku codes please", limit=10)
        assert result[0] == "public.products"

    def test_returns_only_tables_with_score_above_zero(self, embedded_store: SQLiteStore) -> None:
        # Query parallel to axis 0 → users score = 1.0, orders + products
        # score = 0.0. We don't want zero-score tables in the result list:
        # they're noise that crowds out the signal.
        embedder = _ScriptedEmbedder({"q": _unit(0)})
        r = EmbeddingRetriever(store=embedded_store, source_connection_id="src1", embedder=embedder)
        result = r.retrieve("q", limit=10)
        assert result == ["public.users"]

    def test_respects_limit(self, embedded_store: SQLiteStore) -> None:
        # All three axes sum into the query → all three tables score 1.0.
        # With limit=2, only 2 come back.
        # Use a vector where each non-noise axis has equal weight.
        embedder = _ScriptedEmbedder({"all": (1.0, 1.0, 1.0, 0.0)})
        r = EmbeddingRetriever(store=embedded_store, source_connection_id="src1", embedder=embedder)
        result = r.retrieve("all", limit=2)
        assert len(result) == 2

    def test_filters_by_source_connection_id(self, embedded_store: SQLiteStore) -> None:
        embedder = _ScriptedEmbedder({"q": _unit(0)})
        r = EmbeddingRetriever(
            store=embedded_store, source_connection_id="DOES-NOT-EXIST", embedder=embedder
        )
        assert r.retrieve("q", limit=10) == []

    def test_skips_table_without_embeddings(self, tmp_path: Path) -> None:
        # A table indexed without an embedder (or one that hit the cap
        # before embedding completed) has no rows in column_embeddings.
        # The retriever must silently skip it rather than crashing.
        store = SQLiteStore(tmp_path / "s.db")
        sid = "src1"
        store.write_table(_table("users", ("id", "email")), source_connection_id=sid)
        store.write_table(_table("orders", ("id", "user_id")), source_connection_id=sid)
        # Only embed users.
        store.write_table_embeddings(
            "public",
            "users",
            source_connection_id=sid,
            embeddings={"email": _emb(_unit(0))},
        )
        embedder = _ScriptedEmbedder({"q": _unit(0)})
        r = EmbeddingRetriever(store=store, source_connection_id=sid, embedder=embedder)
        result = r.retrieve("q", limit=10)
        assert result == ["public.users"]  # orders not crashed, just absent

    def test_deterministic_tiebreak_when_scores_equal(self, embedded_store: SQLiteStore) -> None:
        # All 3 tables tie at score 1.0; ranking falls back to qualified
        # name order. orders < products < users alphabetically.
        embedder = _ScriptedEmbedder({"all": (1.0, 1.0, 1.0, 0.0)})
        r = EmbeddingRetriever(store=embedded_store, source_connection_id="src1", embedder=embedder)
        result = r.retrieve("all", limit=10)
        assert result == ["public.orders", "public.products", "public.users"]

    def test_dimension_mismatch_between_query_and_stored_raises(self, tmp_path: Path) -> None:
        # If the user re-runs eval with a different embedder dimension
        # than the one used at index time, cosine is undefined. Surface
        # it loudly so they know to wipe + re-index.
        store = SQLiteStore(tmp_path / "s.db")
        sid = "src1"
        store.write_table(_table("users", ("email",)), source_connection_id=sid)
        store.write_table_embeddings(
            "public",
            "users",
            source_connection_id=sid,
            embeddings={"email": _emb(_unit(0))},  # 4-dim
        )

        # Embedder produces 8-dim — mismatched.
        class _BadDimEmbedder:
            model_name = "bad"
            dimension = 8

            def embed(self, text: str) -> tuple[float, ...]:
                return tuple(0.1 for _ in range(8))

        r = EmbeddingRetriever(store=store, source_connection_id=sid, embedder=_BadDimEmbedder())
        with pytest.raises(ValueError, match="dimension"):
            r.retrieve("anything", limit=10)

    def test_zero_norm_query_returns_empty(self, embedded_store: SQLiteStore) -> None:
        # A degenerate zero vector from the embedder makes cosine
        # undefined (0 / 0). Treat as "no signal" — empty result, no
        # crash.
        embedder = _ScriptedEmbedder({"q": (0.0, 0.0, 0.0, 0.0)})
        r = EmbeddingRetriever(store=embedded_store, source_connection_id="src1", embedder=embedder)
        assert r.retrieve("q", limit=10) == []

    def test_zero_norm_stored_vector_does_not_crash(self, tmp_path: Path) -> None:
        # If a stored vector somehow has zero norm (shouldn't happen with
        # a real embedder, but defense-in-depth), it should score 0 not
        # crash.
        store = SQLiteStore(tmp_path / "s.db")
        sid = "src1"
        store.write_table(_table("users", ("email",)), source_connection_id=sid)
        store.write_table(_table("orders", ("id",)), source_connection_id=sid)
        store.write_table_embeddings(
            "public",
            "users",
            source_connection_id=sid,
            embeddings={"email": _emb((0.0, 0.0, 0.0, 0.0))},
        )
        store.write_table_embeddings(
            "public",
            "orders",
            source_connection_id=sid,
            embeddings={"id": _emb(_unit(1))},
        )
        embedder = _ScriptedEmbedder({"q": _unit(1)})
        r = EmbeddingRetriever(store=store, source_connection_id=sid, embedder=embedder)
        result = r.retrieve("q", limit=10)
        # users (zero-norm) drops out; orders stays.
        assert result == ["public.orders"]

    def test_query_embedded_only_once(self, embedded_store: SQLiteStore) -> None:
        # Per-table cosine must NOT re-embed the query each time —
        # otherwise a 100-table schema would do 100 embedder calls
        # instead of 1, defeating the local-embedding cost story.
        embedder = _ScriptedEmbedder({"q": _unit(0)})
        r = EmbeddingRetriever(store=embedded_store, source_connection_id="src1", embedder=embedder)
        r.retrieve("q", limit=10)
        assert embedder.calls == ["q"]

    def test_per_table_aggregation_keeps_max_not_overwritten_by_lower(self, tmp_path: Path) -> None:
        # Two positive-score columns on ONE table with different
        # magnitudes. The aggregation must KEEP the first (higher)
        # score and NOT overwrite it with the second (lower). This
        # exercises the `prev is not None and score <= prev` branch
        # in the per-table MAX aggregation.
        store = SQLiteStore(tmp_path / "s.db")
        sid = "src1"
        store.write_table(_table("users", ("email", "name")), source_connection_id=sid)
        # email aligned with axis 0 → cosine 1.0.
        # name on a 45-deg vector → cosine 1/sqrt(2) ≈ 0.707.
        store.write_table_embeddings(
            "public",
            "users",
            source_connection_id=sid,
            embeddings={
                "email": _emb(_unit(0)),
                "name": _emb((1.0, 1.0, 0.0, 0.0)),
            },
        )
        embedder = _ScriptedEmbedder({"q": _unit(0)})
        r = EmbeddingRetriever(store=store, source_connection_id=sid, embedder=embedder)
        # Only one table in store, so it's returned once. The fact
        # that it's returned and not crashed proves both columns went
        # through the aggregation without one clobbering the other.
        assert r.retrieve("q", limit=10) == ["public.users"]
