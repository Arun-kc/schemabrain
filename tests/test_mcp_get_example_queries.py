"""Tests for the `get_example_queries` MCP tool.

Two layers tested here:

  - `get_example_queries_impl` (pure function, store + qualified_name in,
    `ExampleQueriesResult` out) — happy path, empty path, error paths.
  - The wired tool on the FastMCP server — envelope round-trip, status
    enum honouring, recovery hint construction.

The data side relies on direct-SQL injection (same pattern as the
store tests) because the mining writer doesn't exist yet in v0.5.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from schemabrain.core.store import SQLiteStore
from schemabrain.mcp import build_server
from schemabrain.mcp.envelope import ToolResponse
from schemabrain.mcp.get_example_queries import get_example_queries_impl
from schemabrain.mcp.shapes import ExampleQueriesResult, TableNotFoundError
from tests._example_query_helpers import (
    insert_example_query as _insert_example,
)
from tests._example_query_helpers import (
    seed_table_for_examples as _seed_table,
)

# ----- impl-level tests ----------------------------------------------------


class TestImplHappyPath:
    def test_returns_examples_ordered_by_popularity(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            _insert_example(
                store,
                source_id="src",
                schema="public",
                table="orders",
                sql_text="rare",
                observation_count=1,
            )
            _insert_example(
                store,
                source_id="src",
                schema="public",
                table="orders",
                sql_text="popular",
                observation_count=99,
            )
            result = get_example_queries_impl(
                store=store,
                source_connection_id="src",
                qualified_name="public.orders",
            )
            assert isinstance(result, ExampleQueriesResult)
            assert result.qualified_name == "public.orders"
            assert [q.sql_text for q in result.queries] == ["popular", "rare"]

    def test_token_estimate_is_positive_and_grows_with_payload(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            _insert_example(
                store,
                source_id="src",
                schema="public",
                table="orders",
                sql_text="x",
            )
            small = get_example_queries_impl(
                store=store,
                source_connection_id="src",
                qualified_name="public.orders",
            )
            _insert_example(
                store,
                source_id="src",
                schema="public",
                table="orders",
                sql_text="a much longer example SQL string that takes up bytes",
            )
            big = get_example_queries_impl(
                store=store,
                source_connection_id="src",
                qualified_name="public.orders",
            )
            assert small.token_estimate >= 1
            assert big.token_estimate > small.token_estimate

    def test_pii_fields_surface_sorted_list(self, tmp_path: Path) -> None:
        # JSON friendliness — frozenset becomes a sorted list at the
        # MCP boundary so the agent sees a stable order.
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="users")
            _insert_example(
                store,
                source_id="src",
                schema="public",
                table="users",
                sql_text="SELECT email, phone FROM users",
                sensitivity="pii",
                pii_categories="contact,online_identifier",
            )
            result = get_example_queries_impl(
                store=store,
                source_connection_id="src",
                qualified_name="public.users",
            )
            assert len(result.queries) == 1
            q = result.queries[0]
            assert q.sensitivity == "pii"
            # Sorted alphabetically — stable across runs.
            assert q.pii_categories == ["contact", "online_identifier"]

    def test_source_field_propagates(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            # Mix sources via direct SQL.
            conn = store._require_conn()
            conn.execute(
                "INSERT INTO example_queries "
                "(source_connection_id, schema_name, table_name, sql_text, "
                "observation_count, first_seen_at, last_seen_at, source, "
                "sensitivity, pii_categories) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("src", "public", "orders", "curated_sql", 5, 0, 0, "curated", "public", ""),
            )
            conn.commit()
            result = get_example_queries_impl(
                store=store,
                source_connection_id="src",
                qualified_name="public.orders",
            )
            assert result.queries[0].source == "curated"


class TestImplEmptyPath:
    def test_table_exists_no_examples_returns_empty_queries_list(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            # No _insert_example calls — table is indexed but has no
            # observed SQL yet (the v0.5 day-one state).
            result = get_example_queries_impl(
                store=store,
                source_connection_id="src",
                qualified_name="public.orders",
            )
            assert isinstance(result, ExampleQueriesResult)
            assert result.qualified_name == "public.orders"
            assert result.queries == []


class TestImplErrorPaths:
    def test_unknown_table_raises_table_not_found(self, tmp_path: Path) -> None:
        with (
            SQLiteStore(tmp_path / "s.db") as store,
            pytest.raises(TableNotFoundError, match=r"public\.nope"),
        ):
            # Empty store — no `public.nope` ever written.
            get_example_queries_impl(
                store=store,
                source_connection_id="src",
                qualified_name="public.nope",
            )

    def test_malformed_qualified_name_raises_value_error(self, tmp_path: Path) -> None:
        with (
            SQLiteStore(tmp_path / "s.db") as store,
            pytest.raises(ValueError, match=r"schema\.name"),
        ):
            get_example_queries_impl(
                store=store,
                source_connection_id="src",
                qualified_name="orders",  # no dot, malformed
            )

    def test_three_part_qualified_name_raises_value_error(self, tmp_path: Path) -> None:
        with (
            SQLiteStore(tmp_path / "s.db") as store,
            pytest.raises(ValueError, match=r"schema\.name"),
        ):
            get_example_queries_impl(
                store=store,
                source_connection_id="src",
                qualified_name="public.orders.id",  # too many dots
            )


class TestExampleQueryItemPiiInvariant:
    """The MCP envelope shape enforces the same cross-layer invariant
    as the store-side dataclass, so any envelope an agent receives
    preserves the Phase B ADR §3 contract.
    """

    def test_pii_sensitivity_without_categories_raises(self) -> None:
        from schemabrain.mcp.shapes import ExampleQueryItem

        with pytest.raises(ValueError, match="at least one pii_category"):
            ExampleQueryItem(
                sql_text="x",
                observation_count=1,
                source="pg_stat_statements",
                sensitivity="pii",
                pii_categories=[],
            )

    def test_non_pii_sensitivity_with_categories_raises(self) -> None:
        from schemabrain.mcp.shapes import ExampleQueryItem

        with pytest.raises(ValueError, match="cannot carry pii_categories"):
            ExampleQueryItem(
                sql_text="x",
                observation_count=1,
                source="pg_stat_statements",
                sensitivity="confidential",
                pii_categories=["contact"],
            )


class TestImplSourceIsolation:
    def test_examples_under_other_source_are_invisible(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src_a", schema="public", table="orders")
            _seed_table(store, source_id="src_b", schema="public", table="orders")
            _insert_example(
                store,
                source_id="src_b",
                schema="public",
                table="orders",
                sql_text="from_b",
            )
            result = get_example_queries_impl(
                store=store,
                source_connection_id="src_a",
                qualified_name="public.orders",
            )
            assert result.queries == []


# ----- envelope round-trip tests ------------------------------------------


def _build_test_server(tmp_path: Path) -> tuple[object, SQLiteStore]:
    """Build a FastMCP server backed by a temp store with one indexed
    table (`public.orders`). Returns `(server, store)` so tests can
    inject example rows after build.
    """

    class _StubEmbedder:
        model_name = "test"
        dimension = 4

        def embed(self, text: str) -> tuple[float, ...]:
            del text
            return (1.0, 0.0, 0.0, 0.0)

    store = SQLiteStore(tmp_path / "s.db")
    _seed_table(store, source_id="sid", schema="public", table="orders")
    server = build_server(
        store=store,
        source_connection_id="sid",
        embedder=_StubEmbedder(),
    )
    return server, store


class TestEnvelopeSuccess:
    def test_returns_success_envelope_with_examples(self, tmp_path: Path) -> None:
        import asyncio

        server, store = _build_test_server(tmp_path)
        try:
            _insert_example(
                store,
                source_id="sid",
                schema="public",
                table="orders",
                sql_text="SELECT * FROM orders",
                observation_count=10,
            )
            _content, structured = asyncio.run(
                server.call_tool(
                    "get_example_queries",
                    {"qualified_name": "public.orders"},
                )
            )
            envelope = ToolResponse.model_validate(structured)
            assert envelope.status == "success"
            assert envelope.error is None
            assert envelope.data is not None
            assert envelope.data["qualified_name"] == "public.orders"
            assert envelope.data["queries"][0]["sql_text"] == "SELECT * FROM orders"
            # Charter Principle 5: composition hint to drill into the
            # table's structural shape.
            assert "describe_table" in (envelope.follow_up_hints or ())
        finally:
            store.close()


class TestEnvelopeEmpty:
    def test_indexed_table_no_examples_yields_empty_status(self, tmp_path: Path) -> None:
        import asyncio

        server, store = _build_test_server(tmp_path)
        try:
            _content, structured = asyncio.run(
                server.call_tool(
                    "get_example_queries",
                    {"qualified_name": "public.orders"},
                )
            )
            envelope = ToolResponse.model_validate(structured)
            assert envelope.status == "empty"
            assert envelope.error is None
            assert envelope.data is not None
            assert envelope.data["queries"] == []
            # Recovery hint points at `describe_table` so the agent has
            # an actionable next step when the empty state isn't an
            # error but is also not actionable from this tool.
            assert "describe_table" in (envelope.follow_up_hints or ())
        finally:
            store.close()


class TestEnvelopeErrors:
    def test_unknown_table_yields_unknown_name_error(self, tmp_path: Path) -> None:
        import asyncio

        server, store = _build_test_server(tmp_path)
        try:
            _content, structured = asyncio.run(
                server.call_tool(
                    "get_example_queries",
                    {"qualified_name": "public.nope"},
                )
            )
            envelope = ToolResponse.model_validate(structured)
            assert envelope.status == "error"
            assert envelope.error is not None
            assert envelope.error.kind == "unknown_name"
            # Recovery routes the agent to discovery.
            assert envelope.error.recovery.suggested_tool == "find_relevant_tables"
        finally:
            store.close()

    def test_malformed_name_yields_malformed_name_error(self, tmp_path: Path) -> None:
        import asyncio

        server, store = _build_test_server(tmp_path)
        try:
            _content, structured = asyncio.run(
                server.call_tool(
                    "get_example_queries",
                    {"qualified_name": "orders"},  # no schema
                )
            )
            envelope = ToolResponse.model_validate(structured)
            assert envelope.status == "error"
            assert envelope.error is not None
            assert envelope.error.kind == "malformed_name"
            assert envelope.error.recovery.suggested_tool == "find_relevant_tables"
        finally:
            store.close()

    def test_internal_error_wraps_unexpected_store_exception(self, tmp_path: Path) -> None:
        # Drop the example_queries table out from under the live store
        # to force an OperationalError on the next list call. The MCP
        # tool boundary must wrap the unexpected exception in an
        # `internal_error` envelope rather than propagating.
        import asyncio

        server, store = _build_test_server(tmp_path)
        try:
            conn = store._require_conn()
            conn.execute("DROP TABLE example_queries")
            conn.commit()
            _content, structured = asyncio.run(
                server.call_tool(
                    "get_example_queries",
                    {"qualified_name": "public.orders"},
                )
            )
            envelope = ToolResponse.model_validate(structured)
            assert envelope.status == "error"
            assert envelope.error is not None
            assert envelope.error.kind == "internal_error"
        finally:
            store.close()


class TestToolDescriptionCharterCompliant:
    """Spot-check the inline description; the full lint is enforced via
    `scripts/charter_lint.py` and its companion test file.
    """

    def test_description_starts_with_use_this_when(self, tmp_path: Path) -> None:
        import asyncio

        server, store = _build_test_server(tmp_path)
        try:
            tools = asyncio.run(server.list_tools())
            tool = next(t for t in tools if t.name == "get_example_queries")
            assert tool.description.lower().startswith("use this when")
        finally:
            store.close()

    def test_description_names_a_sibling(self, tmp_path: Path) -> None:
        import asyncio

        server, store = _build_test_server(tmp_path)
        try:
            tools = asyncio.run(server.list_tools())
            tool = next(t for t in tools if t.name == "get_example_queries")
            siblings = {
                "find_relevant_tables",
                "describe_table",
                "describe_column",
                "suggest_joins",
            }
            assert any(s in tool.description for s in siblings)
        finally:
            store.close()

    def test_description_under_500_chars(self, tmp_path: Path) -> None:
        import asyncio

        server, store = _build_test_server(tmp_path)
        try:
            tools = asyncio.run(server.list_tools())
            tool = next(t for t in tools if t.name == "get_example_queries")
            assert len(tool.description) <= 500
        finally:
            store.close()
