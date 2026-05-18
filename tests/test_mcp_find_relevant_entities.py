"""Tests for the `find_relevant_entities` MCP tool.

Three layers:

  - `find_relevant_entities_impl` (pure function, store + embedder in,
    `list[EntityHit]` out) — empty, ranking, source-filter, edge cases.
  - The wired tool on the FastMCP server — envelope round-trip,
    empty/success status mapping, follow-up hints to `describe_entity`.
  - The `result_summary` extractor — what the audit row + tail render
    pull off the response.

`find_relevant_entities` mirrors `find_relevant_tables` but anchors on
semantic entities. It reuses the column-embedding index — an entity's
score is the MAX cosine similarity across its bound table's columns,
restricted to tables that have a confirmed entity binding.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from schemabrain.core.description import ColumnDescription
from schemabrain.core.embedding import ColumnEmbedding
from schemabrain.core.entity import Entity, Origin, SingleTableBinding
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore
from schemabrain.mcp import build_server
from schemabrain.mcp.envelope import ToolResponse
from schemabrain.mcp.find_relevant_entities import find_relevant_entities_impl
from schemabrain.mcp.shapes import EntityHit
from schemabrain.observability.extractors import get_result_extractor

# ----- helpers ---------------------------------------------------------------


def _column(
    name: str,
    *,
    table_name: str,
    schema_name: str = "public",
    data_type: str = "TEXT",
    nullable: bool = True,
    ordinal_position: int = 1,
    is_primary_key: bool = False,
) -> Column:
    return Column(
        name=name,
        table_name=table_name,
        schema_name=schema_name,
        data_type=data_type,
        nullable=nullable,
        ordinal_position=ordinal_position,
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


def _entity(name: str, qualified_table: str, *, origin: Origin = "manual") -> Entity:
    return Entity(
        name=name,
        description=f"A {name} entity",
        binding=SingleTableBinding(qualified_table=qualified_table),
        identity="id",
        origin=origin,
    )


class _AxisEmbedder:
    """Test embedder mapping query strings to pre-scripted axis vectors.

    Mirrors the helper in `test_mcp_tools.py` so the test surface stays
    consistent across the two `find_relevant_*` tools.
    """

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
    """Three tables (users, orders, products), all with embeddings.

    Entities are NOT seeded here — tests add them per-case so each test
    can exercise the entity-mapping logic against a known surface.

    Embedding axes:
      - users.email → axis 0
      - orders.user_id → axis 1
      - products.sku → axis 2
      - every "id" column → axis 3 (neutral, never the winner)
    """
    store = SQLiteStore(tmp_path / "store.db")
    sid = "src1"

    users = Table(
        name="users",
        schema_name="public",
        columns=(
            _column("id", table_name="users", data_type="BIGINT", is_primary_key=True),
            _column("email", table_name="users", data_type="TEXT", ordinal_position=2),
        ),
    )
    orders = Table(
        name="orders",
        schema_name="public",
        columns=(
            _column("id", table_name="orders", data_type="BIGINT", is_primary_key=True),
            _column("user_id", table_name="orders", data_type="BIGINT", ordinal_position=2),
        ),
    )
    products = Table(
        name="products",
        schema_name="public",
        columns=(
            _column("id", table_name="products", data_type="BIGINT", is_primary_key=True),
            _column("sku", table_name="products", data_type="TEXT", ordinal_position=2),
        ),
    )
    for t in (users, orders, products):
        store.write_table(t, source_connection_id=sid)

    store.write_table_descriptions(
        "public",
        "users",
        source_connection_id=sid,
        descriptions={
            "id": _desc("user id"),
            "email": _desc("user contact email"),
        },
    )
    store.write_table_descriptions(
        "public",
        "orders",
        source_connection_id=sid,
        descriptions={
            "id": _desc("order id"),
            "user_id": _desc("customer who placed the order"),
        },
    )
    store.write_table_descriptions(
        "public",
        "products",
        source_connection_id=sid,
        descriptions={
            "id": _desc("product id"),
            "sku": _desc("inventory sku code"),
        },
    )

    store.write_table_embeddings(
        "public",
        "users",
        source_connection_id=sid,
        embeddings={"id": _emb(_unit(3)), "email": _emb(_unit(0))},
    )
    store.write_table_embeddings(
        "public",
        "orders",
        source_connection_id=sid,
        embeddings={"id": _emb(_unit(3)), "user_id": _emb(_unit(1))},
    )
    store.write_table_embeddings(
        "public",
        "products",
        source_connection_id=sid,
        embeddings={"id": _emb(_unit(3)), "sku": _emb(_unit(2))},
    )
    return store


SOURCE_ID = "src1"


# ----- impl-level tests ------------------------------------------------------


class TestFindRelevantEntitiesImpl:
    def test_returns_typed_entity_hits(self, populated_store: SQLiteStore) -> None:
        populated_store.write_entity(
            _entity("customer", "public.users"), source_connection_id=SOURCE_ID
        )
        embedder = _AxisEmbedder({"emails": _unit(0)})
        result = find_relevant_entities_impl(
            store=populated_store,
            source_connection_id=SOURCE_ID,
            embedder=embedder,
            query="emails",
            limit=10,
        )
        assert all(isinstance(h, EntityHit) for h in result)

    def test_surfaces_entity_with_winning_column(self, populated_store: SQLiteStore) -> None:
        populated_store.write_entity(
            _entity("customer", "public.users"), source_connection_id=SOURCE_ID
        )
        embedder = _AxisEmbedder({"q": _unit(0)})
        result = find_relevant_entities_impl(
            store=populated_store,
            source_connection_id=SOURCE_ID,
            embedder=embedder,
            query="q",
            limit=10,
        )
        assert result[0].name == "customer"
        assert result[0].qualified_table == "public.users"
        assert result[0].best_column == "email"
        assert result[0].best_column_description == "user contact email"
        assert result[0].score == pytest.approx(1.0)

    def test_ranks_multiple_entities_by_score(self, populated_store: SQLiteStore) -> None:
        populated_store.write_entity(
            _entity("customer", "public.users"), source_connection_id=SOURCE_ID
        )
        populated_store.write_entity(
            _entity("order", "public.orders"), source_connection_id=SOURCE_ID
        )
        # Query biased toward axis 0 (users.email): score 1.0 for users,
        # 0.0 for orders. Only customer survives the zero-score filter.
        embedder = _AxisEmbedder({"q": _unit(0)})
        result = find_relevant_entities_impl(
            store=populated_store,
            source_connection_id=SOURCE_ID,
            embedder=embedder,
            query="q",
            limit=10,
        )
        assert [h.name for h in result] == ["customer"]

    def test_deterministic_tiebreak_alphabetical_by_entity_name(
        self, populated_store: SQLiteStore
    ) -> None:
        # All three entities defined; all-axes query ties them all at 1.0.
        # Deterministic tiebreak: alphabetical by entity name.
        populated_store.write_entity(
            _entity("customer", "public.users"), source_connection_id=SOURCE_ID
        )
        populated_store.write_entity(
            _entity("order", "public.orders"), source_connection_id=SOURCE_ID
        )
        populated_store.write_entity(
            _entity("product", "public.products"), source_connection_id=SOURCE_ID
        )
        embedder = _AxisEmbedder({"all": (1.0, 1.0, 1.0, 0.0)})
        result = find_relevant_entities_impl(
            store=populated_store,
            source_connection_id=SOURCE_ID,
            embedder=embedder,
            query="all",
            limit=10,
        )
        assert [h.name for h in result] == ["customer", "order", "product"]

    def test_columns_without_entity_binding_are_skipped(self, populated_store: SQLiteStore) -> None:
        # Only one entity defined (customer → users). Orders has the
        # winning column on axis 1, but no entity binds it — must not
        # appear in the result set.
        populated_store.write_entity(
            _entity("customer", "public.users"), source_connection_id=SOURCE_ID
        )
        embedder = _AxisEmbedder({"q": _unit(1)})
        result = find_relevant_entities_impl(
            store=populated_store,
            source_connection_id=SOURCE_ID,
            embedder=embedder,
            query="q",
            limit=10,
        )
        # orders.user_id scored 1.0 on axis 1, but orders has no entity.
        # users scored 0.0 on axis 1 and is also dropped.
        assert result == []

    def test_limit_of_one_returns_only_top_ranked(self, populated_store: SQLiteStore) -> None:
        """Boundary case for the `candidates[:limit]` slice — confirms
        only the alphabetically-first entity surfaces under a 3-way
        score tie when limit=1, not an off-by-one accident.
        """
        populated_store.write_entity(
            _entity("customer", "public.users"), source_connection_id=SOURCE_ID
        )
        populated_store.write_entity(
            _entity("order", "public.orders"), source_connection_id=SOURCE_ID
        )
        populated_store.write_entity(
            _entity("product", "public.products"), source_connection_id=SOURCE_ID
        )
        embedder = _AxisEmbedder({"all": (1.0, 1.0, 1.0, 0.0)})
        result = find_relevant_entities_impl(
            store=populated_store,
            source_connection_id=SOURCE_ID,
            embedder=embedder,
            query="all",
            limit=1,
        )
        assert [h.name for h in result] == ["customer"]

    def test_respects_limit(self, populated_store: SQLiteStore) -> None:
        populated_store.write_entity(
            _entity("customer", "public.users"), source_connection_id=SOURCE_ID
        )
        populated_store.write_entity(
            _entity("order", "public.orders"), source_connection_id=SOURCE_ID
        )
        populated_store.write_entity(
            _entity("product", "public.products"), source_connection_id=SOURCE_ID
        )
        embedder = _AxisEmbedder({"all": (1.0, 1.0, 1.0, 0.0)})
        result = find_relevant_entities_impl(
            store=populated_store,
            source_connection_id=SOURCE_ID,
            embedder=embedder,
            query="all",
            limit=2,
        )
        assert [h.name for h in result] == ["customer", "order"]

    def test_zero_limit_returns_empty_without_embedder_call(
        self, populated_store: SQLiteStore
    ) -> None:
        populated_store.write_entity(
            _entity("customer", "public.users"), source_connection_id=SOURCE_ID
        )
        embedder = _AxisEmbedder({})
        result = find_relevant_entities_impl(
            store=populated_store,
            source_connection_id=SOURCE_ID,
            embedder=embedder,
            query="q",
            limit=0,
        )
        assert result == []
        assert embedder.calls == []

    def test_negative_limit_returns_empty(self, populated_store: SQLiteStore) -> None:
        populated_store.write_entity(
            _entity("customer", "public.users"), source_connection_id=SOURCE_ID
        )
        embedder = _AxisEmbedder({})
        result = find_relevant_entities_impl(
            store=populated_store,
            source_connection_id=SOURCE_ID,
            embedder=embedder,
            query="q",
            limit=-5,
        )
        assert result == []
        assert embedder.calls == []

    def test_empty_query_returns_empty_without_embedder_call(
        self, populated_store: SQLiteStore
    ) -> None:
        populated_store.write_entity(
            _entity("customer", "public.users"), source_connection_id=SOURCE_ID
        )
        embedder = _AxisEmbedder({})
        for q in ("", "   ", "\n\t  "):
            result = find_relevant_entities_impl(
                store=populated_store,
                source_connection_id=SOURCE_ID,
                embedder=embedder,
                query=q,
                limit=10,
            )
            assert result == []
        assert embedder.calls == []

    def test_no_entities_curated_returns_empty(self, populated_store: SQLiteStore) -> None:
        # Indexed store, no entities defined yet. Charter behavior:
        # short-circuit before calling the embedder so a non-curated
        # store doesn't burn embedder cycles per call.
        embedder = _AxisEmbedder({})
        result = find_relevant_entities_impl(
            store=populated_store,
            source_connection_id=SOURCE_ID,
            embedder=embedder,
            query="anything",
            limit=10,
        )
        assert result == []
        assert embedder.calls == []

    def test_zero_norm_query_returns_empty(self, populated_store: SQLiteStore) -> None:
        populated_store.write_entity(
            _entity("customer", "public.users"), source_connection_id=SOURCE_ID
        )
        # Zero vector → no direction → no signal. Translated to [] at
        # this layer so the Store's loud ValueError doesn't propagate
        # to the MCP caller (same translation as find_relevant_tables).
        embedder = _AxisEmbedder({"q": (0.0, 0.0, 0.0, 0.0)})
        result = find_relevant_entities_impl(
            store=populated_store,
            source_connection_id=SOURCE_ID,
            embedder=embedder,
            query="q",
            limit=10,
        )
        assert result == []

    def test_embedder_called_exactly_once_per_query(self, populated_store: SQLiteStore) -> None:
        populated_store.write_entity(
            _entity("customer", "public.users"), source_connection_id=SOURCE_ID
        )
        embedder = _AxisEmbedder({"q": _unit(0)})
        find_relevant_entities_impl(
            store=populated_store,
            source_connection_id=SOURCE_ID,
            embedder=embedder,
            query="q",
            limit=10,
        )
        assert embedder.calls == ["q"]

    def test_token_estimate_is_positive_and_finite(self, populated_store: SQLiteStore) -> None:
        populated_store.write_entity(
            _entity("customer", "public.users"), source_connection_id=SOURCE_ID
        )
        embedder = _AxisEmbedder({"q": _unit(0)})
        result = find_relevant_entities_impl(
            store=populated_store,
            source_connection_id=SOURCE_ID,
            embedder=embedder,
            query="q",
            limit=10,
        )
        assert result[0].token_estimate > 0

    def test_multiple_matching_columns_on_same_entity_keep_first_winner(
        self, tmp_path: Path
    ) -> None:
        """When two columns on the same entity-bound table both match
        the query, the FIRST one (ordered by descending score, then
        alphabetical) wins and subsequent matches on the same entity
        are skipped. Per the Store's ordering contract this gives a
        deterministic `best_column` regardless of insertion order.
        """
        store = SQLiteStore(tmp_path / "store.db")
        users = Table(
            name="users",
            schema_name="public",
            columns=(
                _column("id", table_name="users", data_type="BIGINT", is_primary_key=True),
                _column("alpha_col", table_name="users", ordinal_position=2),
                _column("zeta_col", table_name="users", ordinal_position=3),
            ),
        )
        store.write_table(users, source_connection_id="sid")
        store.write_table_descriptions(
            "public",
            "users",
            source_connection_id="sid",
            descriptions={
                "alpha_col": _desc("alpha description"),
                "zeta_col": _desc("zeta description"),
            },
        )
        # Both columns embed on axis 0 → both score 1.0 against an axis-0
        # query. Tiebreak is alphabetical → `alpha_col` wins; `zeta_col`
        # then hits the same-entity skip branch.
        store.write_table_embeddings(
            "public",
            "users",
            source_connection_id="sid",
            embeddings={
                "alpha_col": _emb(_unit(0)),
                "zeta_col": _emb(_unit(0)),
            },
        )
        store.write_entity(_entity("customer", "public.users"), source_connection_id="sid")
        embedder = _AxisEmbedder({"q": _unit(0)})
        result = find_relevant_entities_impl(
            store=store,
            source_connection_id="sid",
            embedder=embedder,
            query="q",
            limit=10,
        )
        # Single entity surfaces (deduped), with alpha_col as winner.
        assert len(result) == 1
        assert result[0].name == "customer"
        assert result[0].best_column == "alpha_col"

    def test_winning_column_without_description_yields_empty_string(self, tmp_path: Path) -> None:
        """A column may be embedded but lack an LLM description (e.g. a
        no-enrich index). The hit must still surface — `best_column` set,
        `best_column_description` empty.
        """
        store = SQLiteStore(tmp_path / "store.db")
        users = Table(
            name="users",
            schema_name="public",
            columns=(
                _column("id", table_name="users", data_type="BIGINT", is_primary_key=True),
                _column("email", table_name="users", ordinal_position=2),
            ),
        )
        store.write_table(users, source_connection_id="sid")
        # Embedding present, description deliberately absent.
        store.write_table_embeddings(
            "public",
            "users",
            source_connection_id="sid",
            embeddings={"email": _emb(_unit(0))},
        )
        store.write_entity(_entity("customer", "public.users"), source_connection_id="sid")
        embedder = _AxisEmbedder({"q": _unit(0)})
        result = find_relevant_entities_impl(
            store=store,
            source_connection_id="sid",
            embedder=embedder,
            query="q",
            limit=10,
        )
        assert result[0].best_column == "email"
        assert result[0].best_column_description == ""

    def test_filters_by_source_connection_id(self, tmp_path: Path) -> None:
        """An entity registered under one source must not appear when
        searching another source.
        """
        store = SQLiteStore(tmp_path / "store.db")
        users = Table(
            name="users",
            schema_name="public",
            columns=(
                _column("id", table_name="users", data_type="BIGINT", is_primary_key=True),
                _column("email", table_name="users", ordinal_position=2),
            ),
        )
        for sid in ("src_a", "src_b"):
            store.write_table(users, source_connection_id=sid)
            store.write_table_descriptions(
                "public",
                "users",
                source_connection_id=sid,
                descriptions={"email": _desc("email")},
            )
            store.write_table_embeddings(
                "public",
                "users",
                source_connection_id=sid,
                embeddings={"email": _emb(_unit(0))},
            )

        store.write_entity(_entity("customer", "public.users"), source_connection_id="src_a")

        embedder = _AxisEmbedder({"q": _unit(0)})

        result_a = find_relevant_entities_impl(
            store=store, source_connection_id="src_a", embedder=embedder, query="q", limit=10
        )
        result_b = find_relevant_entities_impl(
            store=store, source_connection_id="src_b", embedder=embedder, query="q", limit=10
        )
        assert [h.name for h in result_a] == ["customer"]
        # src_b has the same table indexed but no entity; nothing
        # surfaces because the entity binding gates the result.
        assert result_b == []


# ----- envelope round-trip tests ---------------------------------------------


class _StubEmbedder:
    """Stub embedder for server-wiring tests. Always returns axis 0."""

    model_name = "test"
    dimension = _DIM

    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed(self, text: str) -> tuple[float, ...]:
        self.calls.append(text)
        return _unit(0)


def _build_test_server(tmp_path: Path) -> tuple[object, SQLiteStore]:
    """Build a server backed by a fresh store with the ecommerce
    fixture seeded (tables, descriptions, embeddings). Tests inject
    entities directly via `store.write_entity` per-case.
    """
    store = SQLiteStore(tmp_path / "s.db")
    sid = "sid"
    users = Table(
        name="users",
        schema_name="public",
        columns=(
            _column("id", table_name="users", data_type="BIGINT", is_primary_key=True),
            _column("email", table_name="users", ordinal_position=2),
        ),
    )
    store.write_table(users, source_connection_id=sid)
    store.write_table_descriptions(
        "public",
        "users",
        source_connection_id=sid,
        descriptions={"id": _desc("user id"), "email": _desc("user contact email")},
    )
    store.write_table_embeddings(
        "public",
        "users",
        source_connection_id=sid,
        embeddings={"id": _emb(_unit(3)), "email": _emb(_unit(0))},
    )
    server = build_server(
        store=store,
        source_connection_id=sid,
        embedder=_StubEmbedder(),
    )
    return server, store


class TestEnvelopeEmpty:
    def test_no_entities_curated_routes_only_to_find_relevant_tables(self, tmp_path: Path) -> None:
        """When the store has zero curated entities, the empty envelope
        OMITS `list_entities` from hints — calling it would also return
        empty, so routing the agent there is a wasted cycle. Only
        `find_relevant_tables` (physical discovery) is suggested.
        """
        server, store = _build_test_server(tmp_path)
        try:
            _content, structured = asyncio.run(
                server.call_tool("find_relevant_entities", {"query": "email"})
            )
            envelope = ToolResponse.model_validate(structured)
        finally:
            store.close()
        assert envelope.status == "empty"
        assert envelope.error is None
        assert envelope.data == []
        hints = list(envelope.follow_up_hints or ())
        assert hints == ["find_relevant_tables"]

    def test_entities_curated_but_no_match_routes_to_both(self, tmp_path: Path) -> None:
        """When entities ARE curated but none semantically match the
        query, the empty envelope hints at BOTH `list_entities` (so the
        agent can confirm what's available) AND `find_relevant_tables`
        (physical fallback). Different state than 'no entities curated';
        the hints distinguish.
        """
        server, store = _build_test_server(tmp_path)
        try:
            # Curate an entity. Then query on an axis (axis 0) that DOES
            # match the bound table's `email` column — wait, that would
            # return a hit. Instead, the stub embedder always returns
            # axis 0, so we need an entity bound to a table that has no
            # axis-0 columns. Re-seed an `orders` table that has only
            # axis-3 (id) — neutral — embeddings.
            orders = Table(
                name="orders",
                schema_name="public",
                columns=(
                    _column("id", table_name="orders", data_type="BIGINT", is_primary_key=True),
                ),
            )
            store.write_table(orders, source_connection_id="sid")
            store.write_table_embeddings(
                "public",
                "orders",
                source_connection_id="sid",
                embeddings={"id": _emb(_unit(3))},
            )
            # Bind to orders — the stub embedder's axis-0 query won't match.
            store.write_entity(_entity("order", "public.orders"), source_connection_id="sid")
            _content, structured = asyncio.run(
                server.call_tool("find_relevant_entities", {"query": "anything"})
            )
            envelope = ToolResponse.model_validate(structured)
        finally:
            store.close()
        assert envelope.status == "empty"
        assert envelope.data == []
        hints = list(envelope.follow_up_hints or ())
        assert hints == ["list_entities", "find_relevant_tables"]


class TestEnvelopeSuccess:
    def test_returns_success_envelope_with_entity_hits(self, tmp_path: Path) -> None:
        server, store = _build_test_server(tmp_path)
        try:
            store.write_entity(_entity("customer", "public.users"), source_connection_id="sid")
            _content, structured = asyncio.run(
                server.call_tool("find_relevant_entities", {"query": "email"})
            )
            envelope = ToolResponse.model_validate(structured)
        finally:
            store.close()
        assert envelope.status == "success"
        assert envelope.error is None
        assert envelope.data is not None
        assert len(envelope.data) == 1
        # Charter Principle 5: composition hint to drill into one entity.
        assert "describe_entity" in (envelope.follow_up_hints or ())

    def test_envelope_carries_entity_hit_shape(self, tmp_path: Path) -> None:
        server, store = _build_test_server(tmp_path)
        try:
            store.write_entity(_entity("customer", "public.users"), source_connection_id="sid")
            _content, structured = asyncio.run(
                server.call_tool("find_relevant_entities", {"query": "email"})
            )
            envelope = ToolResponse.model_validate(structured)
        finally:
            store.close()
        assert envelope.data is not None
        hit = envelope.data[0]
        assert hit["name"] == "customer"
        assert hit["qualified_table"] == "public.users"
        assert hit["best_column"] == "email"
        assert hit["best_column_description"] == "user contact email"


# ----- result_summary extractor tests ----------------------------------------


class TestResultSummaryExtractor:
    def test_extractor_registered(self) -> None:
        # A registered extractor is what `tail` and the OTel/audit
        # pipeline use to render a one-line summary of the response.
        extractor = get_result_extractor("find_relevant_entities")
        # The default extractor returns {} for any input — the
        # registered one must return a populated dict for a hit list.
        hit = EntityHit(
            name="customer",
            score=1.0,
            qualified_table="public.users",
            best_column="email",
            best_column_description="user contact email",
            token_estimate=10,
        )
        out = extractor([hit])
        assert out == {"matches": 1}

    def test_extractor_handles_none(self) -> None:
        extractor = get_result_extractor("find_relevant_entities")
        assert extractor(None) == {}

    def test_extractor_handles_empty_list(self) -> None:
        extractor = get_result_extractor("find_relevant_entities")
        assert extractor([]) == {"matches": 0}

    def test_extractor_swallows_unexpected_exceptions(self) -> None:
        """The extractor MUST NEVER raise (drives audit + tail rendering).
        If `len(data)` raises, fall back to the empty summary so the
        audit row stays writable.
        """

        class _LenRaiser:
            def __len__(self) -> int:
                raise RuntimeError("synthetic")

        extractor = get_result_extractor("find_relevant_entities")
        assert extractor(_LenRaiser()) == {}
