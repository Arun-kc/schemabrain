"""FastMCP wiring tests — envelope shape, tool metadata, error mapping.

The tool *logic* is exhaustively tested in `test_mcp_tools.py`; these
tests cover the FastMCP wiring layer:

  - All four tools are registered with charter-compliant
    "Use this when..." descriptions and the right metadata hints.
  - In-process `call_tool` returns the `ToolResponse[T]` envelope (no
    exceptions ever propagate through MCP for caller-shape failures).
  - Each exception class from `*_impl` maps to the right envelope
    error kind + recovery hint per Charter v1.0.

Envelope shape semantics are pinned in `test_mcp_envelope.py`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP

from schemabrain.core.embedding import ColumnEmbedding
from schemabrain.core.models import Column, ForeignKey, Table
from schemabrain.core.store import SQLiteStore
from schemabrain.mcp import build_server
from schemabrain.mcp.envelope import CHARTER_VERSION


def _column(name: str, *, ordinal_position: int = 1) -> Column:
    return Column(
        name=name,
        table_name="users",
        schema_name="public",
        data_type="TEXT",
        nullable=False,
        ordinal_position=ordinal_position,
    )


class _StubEmbedder:
    model_name = "test-emb"
    dimension = 4

    def embed(self, text: str) -> tuple[float, ...]:
        del text
        return (1.0, 0.0, 0.0, 0.0)


class _ZeroEmbedder:
    """Embedder whose query vector is orthogonal to the stored vector.

    Used to drive the `find_relevant_tables` empty path — all cosine
    scores collapse to 0.0 so no candidates survive the >0 filter.
    """

    model_name = "test-emb"
    dimension = 4

    def embed(self, text: str) -> tuple[float, ...]:
        del text
        return (0.0, 0.0, 0.0, 1.0)


@pytest.fixture
def server_with_one_table(tmp_path: Path) -> Generator[FastMCP, None, None]:
    store = SQLiteStore(tmp_path / "store.db")
    sid = "src1"
    store.write_table(
        Table(
            name="users",
            schema_name="public",
            columns=(_column("email", ordinal_position=1),),
        ),
        source_connection_id=sid,
    )
    store.write_table_embeddings(
        "public",
        "users",
        source_connection_id=sid,
        embeddings={
            "email": ColumnEmbedding(vector=(1.0, 0.0, 0.0, 0.0), model="test-emb", dimension=4)
        },
    )
    app = build_server(store=store, source_connection_id=sid, embedder=_StubEmbedder())
    yield app
    store.close()


@pytest.fixture
def server_with_one_table_zero_embedder(tmp_path: Path) -> Generator[FastMCP, None, None]:
    """Same store as `server_with_one_table` but with an embedder whose
    query vector zeros out cosine against all stored vectors.
    """
    store = SQLiteStore(tmp_path / "store.db")
    sid = "src1"
    store.write_table(
        Table(
            name="users",
            schema_name="public",
            columns=(_column("email", ordinal_position=1),),
        ),
        source_connection_id=sid,
    )
    store.write_table_embeddings(
        "public",
        "users",
        source_connection_id=sid,
        embeddings={
            "email": ColumnEmbedding(vector=(1.0, 0.0, 0.0, 0.0), model="test-emb", dimension=4)
        },
    )
    app = build_server(store=store, source_connection_id=sid, embedder=_ZeroEmbedder())
    yield app
    store.close()


class TestToolRegistry:
    def test_four_tools_are_registered(self, server_with_one_table) -> None:
        names = {t.name for t in asyncio.run(server_with_one_table.list_tools())}
        assert names == {
            "find_relevant_tables",
            "describe_table",
            "describe_column",
            "suggest_joins",
        }

    def test_descriptions_lead_with_use_this_when(self, server_with_one_table) -> None:
        # Charter Principle 2: descriptions lead with "Use this when…"
        # Slice 1.3 ships the lint rule that enforces this in CI; here
        # we pin the wiring-layer output so a refactor can't silently
        # drop the rule before the lint catches it.
        for tool in asyncio.run(server_with_one_table.list_tools()):
            assert tool.description is not None
            assert tool.description.lower().startswith("use this when"), (
                f"tool {tool.name!r} description must start with "
                f"'Use this when…', got {tool.description[:40]!r}"
            )

    def test_descriptions_name_at_least_one_composition(self, server_with_one_table) -> None:
        # Charter Principle 2: every description must name at least one
        # canonical composition pattern. Mention of another tool name is
        # the cheap check; slice 1.3's lint refines this.
        all_tool_names = {
            "find_relevant_tables",
            "describe_table",
            "describe_column",
            "suggest_joins",
        }
        for tool in asyncio.run(server_with_one_table.list_tools()):
            other_names = all_tool_names - {tool.name}
            assert any(other in (tool.description or "") for other in other_names), (
                f"tool {tool.name!r} description must reference at least one "
                f"sibling tool to satisfy Charter Principle 2"
            )

    def test_all_tools_have_read_only_hint_true(self, server_with_one_table) -> None:
        # All four v1.0 tools are read-only. Slice 1.3 will lint this
        # against the per-tool charter manifest.
        for tool in asyncio.run(server_with_one_table.list_tools()):
            assert tool.annotations is not None, f"tool {tool.name!r} missing annotations"
            assert tool.annotations.readOnlyHint is True
            assert tool.annotations.destructiveHint is False
            assert tool.annotations.idempotentHint is True
            assert tool.annotations.openWorldHint is True

    def test_descriptions_disambiguate_via_instead_when(self, server_with_one_table) -> None:
        # Charter Principle 2 Rule 2: every description must redirect the
        # agent toward a sibling tool when its own use-case doesn't fit
        # (the "instead when" pattern). Slice 1.3's lint will enforce this
        # phrase mechanically — pin it now so the descriptions can't drift.
        for tool in asyncio.run(server_with_one_table.list_tools()):
            assert "instead when" in (tool.description or "").lower(), (
                f"tool {tool.name!r} must include 'instead when' to satisfy "
                f"Charter Principle 2 Rule 2"
            )

    def test_descriptions_under_500_chars(self, server_with_one_table) -> None:
        # Charter Enforcement Level 1: descriptions ≤ 500 chars. Slice 1.3
        # ships the CI lint; this test pins the contract today so a future
        # description edit can't quietly bloat past the gate.
        for tool in asyncio.run(server_with_one_table.list_tools()):
            length = len(tool.description or "")
            assert length <= 500, (
                f"tool {tool.name!r} description is {length} chars; "
                f"Charter Enforcement Level 1 caps at 500"
            )


class TestFindRelevantTablesEnvelope:
    def test_success_envelope(self, server_with_one_table) -> None:
        _content, structured = asyncio.run(
            server_with_one_table.call_tool(
                "find_relevant_tables", {"query": "anything", "limit": 5}
            )
        )
        assert structured["status"] == "success"
        assert structured["error"] is None
        assert structured["charter_version"] == CHARTER_VERSION
        assert structured["confidence"] in {"HIGH", "MEDIUM", "LOW"}
        hits = structured["data"]
        assert isinstance(hits, list)
        assert len(hits) == 1
        h = hits[0]
        assert h["qualified_name"] == "public.users"
        assert h["best_column"] == "email"
        assert h["score"] == pytest.approx(1.0)
        assert h["token_estimate"] > 0
        # Follow-up hint encodes the canonical discover-then-describe flow.
        assert "describe_table" in (structured["follow_up_hints"] or [])

    def test_success_top_hit_score_drives_confidence_bucket(self, server_with_one_table) -> None:
        # Score is 1.0 here → HIGH (>= 0.8). The bucketing thresholds
        # are pinned by the charter v1.0; if they move, this test moves.
        _content, structured = asyncio.run(
            server_with_one_table.call_tool(
                "find_relevant_tables", {"query": "anything", "limit": 5}
            )
        )
        assert structured["confidence"] == "HIGH"

    def test_empty_status_when_no_hits(self, server_with_one_table_zero_embedder) -> None:
        # All cosine scores collapse to 0.0; the >0 filter drops every
        # candidate → status="empty", not "success" with empty list.
        _content, structured = asyncio.run(
            server_with_one_table_zero_embedder.call_tool(
                "find_relevant_tables", {"query": "anything", "limit": 5}
            )
        )
        assert structured["status"] == "empty"
        assert structured["data"] == []
        assert structured["error"] is None
        # No confidence judgment when there's nothing to be confident about.
        assert structured["confidence"] is None

    def test_empty_status_when_limit_is_zero(self, server_with_one_table) -> None:
        # limit=0 short-circuits before any retrieval. Surfaces as empty,
        # not error — the agent passed a degenerate-but-syntactically-OK arg.
        _content, structured = asyncio.run(
            server_with_one_table.call_tool(
                "find_relevant_tables", {"query": "anything", "limit": 0}
            )
        )
        assert structured["status"] == "empty"
        assert structured["data"] == []


class TestDescribeTableEnvelope:
    def test_success_envelope(self, server_with_one_table) -> None:
        _content, structured = asyncio.run(
            server_with_one_table.call_tool("describe_table", {"qualified_name": "public.users"})
        )
        assert structured["status"] == "success"
        assert structured["error"] is None
        assert structured["confidence"] == "HIGH"
        data = structured["data"]
        assert data["qualified_name"] == "public.users"
        assert data["schema_name"] == "public"
        assert data["name"] == "users"
        assert len(data["columns"]) == 1
        assert data["columns"][0]["name"] == "email"
        assert data["foreign_keys"] == []
        assert data["token_estimate"] > 0
        # Canonical workflow: describe_table → describe_column / suggest_joins.
        hints = structured["follow_up_hints"] or []
        assert "describe_column" in hints
        assert "suggest_joins" in hints

    def test_unknown_table_maps_to_unknown_name_error(self, server_with_one_table) -> None:
        _content, structured = asyncio.run(
            server_with_one_table.call_tool("describe_table", {"qualified_name": "public.nope"})
        )
        assert structured["status"] == "error"
        assert structured["data"] is None
        err = structured["error"]
        assert err["kind"] == "unknown_name"
        assert "public.nope" in err["message"]
        # Recovery sends the agent to find_relevant_tables.
        assert err["recovery"]["suggested_tool"] == "find_relevant_tables"
        assert err["recovery"]["suggested_args"] is not None
        # Query is the table name from the failed input — the agent
        # has its best guess at what the user meant.
        assert err["recovery"]["suggested_args"].get("query") == "nope"

    def test_malformed_name_maps_to_malformed_name_error(self, server_with_one_table) -> None:
        _content, structured = asyncio.run(
            server_with_one_table.call_tool("describe_table", {"qualified_name": "nodot"})
        )
        assert structured["status"] == "error"
        assert structured["data"] is None
        err = structured["error"]
        assert err["kind"] == "malformed_name"
        assert "schema.name" in err["message"] or "schema.table" in err["message"]
        assert err["recovery"]["suggested_tool"] == "find_relevant_tables"


class TestDescribeColumnEnvelope:
    def test_success_envelope(self, server_with_one_table) -> None:
        _content, structured = asyncio.run(
            server_with_one_table.call_tool(
                "describe_column", {"qualified_name": "public.users.email"}
            )
        )
        assert structured["status"] == "success"
        assert structured["confidence"] == "HIGH"
        data = structured["data"]
        assert data["qualified_name"] == "public.users.email"
        assert data["schema_name"] == "public"
        assert data["table_name"] == "users"
        assert data["name"] == "email"
        assert data["outgoing_foreign_keys"] == []
        assert data["incoming_foreign_keys"] == []
        assert data["token_estimate"] > 0
        # describe_column → describe_table is the canonical "what siblings
        # does this column have?" follow-up.
        assert "describe_table" in (structured["follow_up_hints"] or [])

    def test_unknown_column_maps_to_unknown_name_error(self, server_with_one_table) -> None:
        _content, structured = asyncio.run(
            server_with_one_table.call_tool(
                "describe_column", {"qualified_name": "public.users.nope"}
            )
        )
        assert structured["status"] == "error"
        err = structured["error"]
        assert err["kind"] == "unknown_name"
        assert "public.users.nope" in err["message"]
        # Recovery: describe_table on the parent table — the agent can
        # see the real column list there and retry.
        assert err["recovery"]["suggested_tool"] == "describe_table"
        assert err["recovery"]["suggested_args"] == {"qualified_name": "public.users"}

    def test_unknown_table_via_describe_column_maps_to_unknown_name(
        self, server_with_one_table
    ) -> None:
        _content, structured = asyncio.run(
            server_with_one_table.call_tool(
                "describe_column", {"qualified_name": "public.ghost.email"}
            )
        )
        assert structured["status"] == "error"
        err = structured["error"]
        assert err["kind"] == "unknown_name"
        # Parent-table case routes to find_relevant_tables, not describe_table —
        # the agent doesn't know any real table to drill into.
        assert err["recovery"]["suggested_tool"] == "find_relevant_tables"

    def test_malformed_name_maps_to_malformed_name_error(self, server_with_one_table) -> None:
        _content, structured = asyncio.run(
            server_with_one_table.call_tool("describe_column", {"qualified_name": "only_two.parts"})
        )
        assert structured["status"] == "error"
        err = structured["error"]
        assert err["kind"] == "malformed_name"
        assert "schema.table.column" in err["message"]
        assert err["recovery"]["suggested_tool"] in {
            "find_relevant_tables",
            "describe_table",
        }


@pytest.fixture
def server_with_fk_pair(tmp_path: Path) -> Generator[FastMCP, None, None]:
    """Two tables with one FK between them — minimum for suggest_joins."""
    store = SQLiteStore(tmp_path / "store.db")
    sid = "src1"

    users = Table(
        name="users",
        schema_name="public",
        columns=(
            Column(
                name="id",
                table_name="users",
                schema_name="public",
                data_type="BIGINT",
                nullable=False,
                ordinal_position=1,
                is_primary_key=True,
            ),
        ),
    )
    orders = Table(
        name="orders",
        schema_name="public",
        columns=(
            Column(
                name="id",
                table_name="orders",
                schema_name="public",
                data_type="BIGINT",
                nullable=False,
                ordinal_position=1,
                is_primary_key=True,
            ),
            Column(
                name="user_id",
                table_name="orders",
                schema_name="public",
                data_type="BIGINT",
                nullable=False,
                ordinal_position=2,
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
    store.write_table(users, source_connection_id=sid)
    store.write_table(orders, source_connection_id=sid)
    app = build_server(store=store, source_connection_id=sid, embedder=_StubEmbedder())
    yield app
    store.close()


class TestSuggestJoinsEnvelope:
    def test_success_envelope(self, server_with_fk_pair) -> None:
        _content, structured = asyncio.run(
            server_with_fk_pair.call_tool(
                "suggest_joins", {"tables": ["public.orders", "public.users"]}
            )
        )
        assert structured["status"] == "success"
        assert structured["confidence"] == "HIGH"
        data = structured["data"]
        assert len(data["paths"]) == 1
        path = data["paths"][0]
        assert path["start_qualified_name"] == "public.orders"
        assert path["end_qualified_name"] == "public.users"
        assert path["hops"] == 1
        assert path["edges"][0]["fk_name"] == "orders_user_id_fkey"
        assert path["edges"][0]["left_columns"] == ["user_id"]
        assert path["edges"][0]["right_columns"] == ["id"]
        assert data["unreachable_pairs"] == []
        assert data["token_estimate"] > 0
        # FK-graph data is schema-sourced (declared constraints) — surface
        # that explicitly so a client agent knows this isn't LLM-inferred.
        assert structured["provenance"] == {
            "source": "schema",
            "model": None,
            "observed_in": None,
        }

    def test_single_table_input_maps_to_malformed_name_error(self, server_with_fk_pair) -> None:
        _content, structured = asyncio.run(
            server_with_fk_pair.call_tool("suggest_joins", {"tables": ["public.users"]})
        )
        assert structured["status"] == "error"
        err = structured["error"]
        assert err["kind"] == "malformed_name"
        assert "at least 2" in err["message"]

    def test_unknown_table_maps_to_unknown_name_error(self, server_with_fk_pair) -> None:
        _content, structured = asyncio.run(
            server_with_fk_pair.call_tool(
                "suggest_joins",
                {"tables": ["public.users", "public.ghost"]},
            )
        )
        assert structured["status"] == "error"
        err = structured["error"]
        assert err["kind"] == "unknown_name"
        assert "public.ghost" in err["message"]
        assert err["recovery"]["suggested_tool"] == "find_relevant_tables"

    def test_malformed_qualified_name_in_input_maps_to_malformed_name(
        self, server_with_fk_pair
    ) -> None:
        _content, structured = asyncio.run(
            server_with_fk_pair.call_tool(
                "suggest_joins",
                {"tables": ["public.users", "bare_no_schema"]},
            )
        )
        assert structured["status"] == "error"
        err = structured["error"]
        assert err["kind"] == "malformed_name"

    def test_non_positive_max_hops_maps_to_malformed_name(self, server_with_fk_pair) -> None:
        _content, structured = asyncio.run(
            server_with_fk_pair.call_tool(
                "suggest_joins",
                {"tables": ["public.users", "public.orders"], "max_hops": 0},
            )
        )
        assert structured["status"] == "error"
        err = structured["error"]
        assert err["kind"] == "malformed_name"
        assert "max_hops" in err["message"]


class TestRunStdio:
    def test_run_stdio_constructs_app_and_invokes_run_with_stdio_transport(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # We can't actually drive stdio in a unit test (it would block),
        # but we can stub `FastMCP.run` and verify run_stdio constructs
        # the app and dispatches with `transport="stdio"`. This pins
        # the contract so a future SDK that renames the transport key
        # gets caught here.
        from schemabrain.mcp.server import run_stdio

        store = SQLiteStore(tmp_path / "s.db")
        sid = "src1"

        captured: dict[str, object] = {}

        def _capture_run(self: FastMCP, transport: str = "stdio", **_: object) -> None:
            captured["transport"] = transport
            captured["app_name"] = self.name

        monkeypatch.setattr(FastMCP, "run", _capture_run)

        run_stdio(store=store, source_connection_id=sid, embedder=_StubEmbedder())

        assert captured["transport"] == "stdio"
        assert captured["app_name"] == "schemabrain"
        store.close()


class TestConfidenceBucketing:
    """Pin Charter Principle 4 thresholds: ≥0.8 HIGH, ≥0.5 MEDIUM, else LOW.

    Exercised through the public `_confidence_from_score` helper rather
    than synthesizing exotic embeddings — the bucketing logic is
    independent of where the score came from.
    """

    def test_high_at_floor(self) -> None:
        from schemabrain.mcp.server import _confidence_from_score

        assert _confidence_from_score(0.8) == "HIGH"
        assert _confidence_from_score(0.99) == "HIGH"

    def test_medium_in_range(self) -> None:
        from schemabrain.mcp.server import _confidence_from_score

        assert _confidence_from_score(0.5) == "MEDIUM"
        assert _confidence_from_score(0.79) == "MEDIUM"

    def test_low_below_medium_floor(self) -> None:
        from schemabrain.mcp.server import _confidence_from_score

        assert _confidence_from_score(0.49) == "LOW"
        assert _confidence_from_score(0.01) == "LOW"


class TestRecoveryHelpers:
    """Helper coverage for the None-branch paths that the impl-driven
    tests cannot reach (parsing succeeds before exceptions fire, so
    these branches need direct exercise).
    """

    def test_safe_table_part_two_part(self) -> None:
        from schemabrain.mcp.server import _safe_table_part

        assert _safe_table_part("public.orders") == "orders"

    def test_safe_table_part_three_part(self) -> None:
        from schemabrain.mcp.server import _safe_table_part

        assert _safe_table_part("public.orders.user_id") == "orders"

    def test_safe_table_part_returns_none_for_unparseable(self) -> None:
        from schemabrain.mcp.server import _safe_table_part

        assert _safe_table_part("just_a_word") is None
        assert _safe_table_part("a.b.c.d") is None
        assert _safe_table_part(".") is None

    def test_maybe_query_arg_with_parseable(self) -> None:
        from schemabrain.mcp.server import _maybe_query_arg

        assert _maybe_query_arg("public.orders") == {"query": "orders"}

    def test_maybe_query_arg_returns_none_for_unparseable(self) -> None:
        from schemabrain.mcp.server import _maybe_query_arg

        assert _maybe_query_arg("just_a_word") is None

    def test_parent_table_qualified_name_extracts_schema_table(self) -> None:
        from schemabrain.mcp.server import _parent_table_qualified_name

        assert _parent_table_qualified_name("public.orders.user_id") == "public.orders"

    def test_parent_table_qualified_name_returns_none_for_non_three_part(self) -> None:
        from schemabrain.mcp.server import _parent_table_qualified_name

        assert _parent_table_qualified_name("public.orders") is None
        assert _parent_table_qualified_name("only_one_part") is None


class TestUnknownColumnRecoveryArgs:
    """Pin the recovery args on unknown_name (column) — the agent should
    receive `{"qualified_name": "<parent>"}` so retry is one targeted call.
    """

    def test_unknown_column_recovery_passes_parent_qualified_name(
        self, server_with_one_table
    ) -> None:
        _content, structured = asyncio.run(
            server_with_one_table.call_tool(
                "describe_column", {"qualified_name": "public.users.nope"}
            )
        )
        err = structured["error"]
        assert err["recovery"]["suggested_args"] == {"qualified_name": "public.users"}

    def test_unknown_parent_table_via_describe_column_recovery_carries_query(
        self, server_with_one_table
    ) -> None:
        _content, structured = asyncio.run(
            server_with_one_table.call_tool(
                "describe_column", {"qualified_name": "public.ghost.email"}
            )
        )
        err = structured["error"]
        assert err["recovery"]["suggested_args"] == {"query": "ghost"}


class TestInternalErrorCatchAll:
    """The four boundary `Exception` catches map unexpected raises to
    `internal_error`. Per Charter v1.0, `internal_error` is "A bug; the
    agent should not retry. Logged for repair." We verify both halves:
      - the envelope's user-facing message is sanitized (no `str(exc)`
        leak — server paths or internal state could leak otherwise),
      - the full exception IS logged on the server side so an operator
        can act on it.
    """

    _SANITIZED_FRAGMENT = "unexpected internal error"

    def test_find_relevant_tables_internal_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        server_with_one_table,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def _boom(**_: object) -> list:
            raise RuntimeError("retrieval went sideways")

        monkeypatch.setattr("schemabrain.mcp.server.find_relevant_tables_impl", _boom)
        with caplog.at_level("ERROR", logger="schemabrain.mcp.server"):
            _content, structured = asyncio.run(
                server_with_one_table.call_tool(
                    "find_relevant_tables", {"query": "x", "limit": 5}
                )
            )
        assert structured["status"] == "error"
        assert structured["error"]["kind"] == "internal_error"
        # Sanitized message: no `str(exc)` leak.
        assert self._SANITIZED_FRAGMENT in structured["error"]["message"].lower()
        assert "retrieval went sideways" not in structured["error"]["message"]
        # Traceback IS in the server log where the operator can find it.
        assert any(
            "retrieval went sideways" in (rec.exc_text or "") for rec in caplog.records
        )

    def test_describe_table_internal_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        server_with_one_table,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def _boom(**_: object) -> object:
            raise RuntimeError("store exploded")

        monkeypatch.setattr("schemabrain.mcp.server.describe_table_impl", _boom)
        with caplog.at_level("ERROR", logger="schemabrain.mcp.server"):
            _content, structured = asyncio.run(
                server_with_one_table.call_tool(
                    "describe_table", {"qualified_name": "public.users"}
                )
            )
        assert structured["status"] == "error"
        assert structured["error"]["kind"] == "internal_error"
        assert "store exploded" not in structured["error"]["message"]

    def test_describe_column_internal_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        server_with_one_table,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def _boom(**_: object) -> object:
            raise RuntimeError("column store exploded")

        monkeypatch.setattr("schemabrain.mcp.server.describe_column_impl", _boom)
        with caplog.at_level("ERROR", logger="schemabrain.mcp.server"):
            _content, structured = asyncio.run(
                server_with_one_table.call_tool(
                    "describe_column", {"qualified_name": "public.users.email"}
                )
            )
        assert structured["status"] == "error"
        assert structured["error"]["kind"] == "internal_error"
        assert "column store exploded" not in structured["error"]["message"]

    def test_suggest_joins_internal_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        server_with_fk_pair,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def _boom(**_: object) -> object:
            raise RuntimeError("bfs exploded")

        monkeypatch.setattr("schemabrain.mcp.server.suggest_joins_impl", _boom)
        with caplog.at_level("ERROR", logger="schemabrain.mcp.server"):
            _content, structured = asyncio.run(
                server_with_fk_pair.call_tool(
                    "suggest_joins", {"tables": ["public.orders", "public.users"]}
                )
            )
        assert structured["status"] == "error"
        assert structured["error"]["kind"] == "internal_error"
        assert "bfs exploded" not in structured["error"]["message"]
