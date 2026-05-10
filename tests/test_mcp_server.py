"""FastMCP integration smoke tests.

The tool *logic* is exhaustively tested in `test_mcp_tools.py`; these
tests cover the FastMCP wiring layer:
  - Both tools are registered with sane names + descriptions.
  - In-process `call_tool` returns structured Pydantic data the SDK
    can serialize to MCP `CallToolResult`.
  - Tool errors propagate cleanly through the SDK.

We exercise FastMCP via its in-process `call_tool` API rather than
spinning up stdio — that's left to the manual E2E run since it
requires subprocess + a real client.
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP

from schemabrain.core.embedding import ColumnEmbedding
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore
from schemabrain.mcp import build_server


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


class TestFastMCPIntegration:
    def test_three_tools_are_registered(self, server_with_one_table) -> None:
        names = {t.name for t in asyncio.run(server_with_one_table.list_tools())}
        assert names == {"find_relevant_tables", "describe_table", "describe_column"}

    def test_tool_descriptions_are_non_empty(self, server_with_one_table) -> None:
        for tool in asyncio.run(server_with_one_table.list_tools()):
            assert tool.description, f"tool {tool.name!r} has empty description"
            assert len(tool.description) > 30

    def test_find_relevant_tables_round_trips_via_call_tool(self, server_with_one_table) -> None:
        # FastMCP returns (content_blocks, structured_dict). The
        # structured_dict is the JSON-friendly tool output.
        _content, structured = asyncio.run(
            server_with_one_table.call_tool(
                "find_relevant_tables", {"query": "anything", "limit": 5}
            )
        )
        # FastMCP wraps a list-typed return in `{"result": [...]}`
        # by default. We accept either shape so a future SDK change
        # that drops the wrapper doesn't silently break us.
        hits = structured.get("result", structured)
        assert isinstance(hits, list)
        assert len(hits) == 1
        h = hits[0]
        assert h["qualified_name"] == "public.users"
        assert h["best_column"] == "email"
        assert h["score"] == pytest.approx(1.0)
        assert h["token_estimate"] > 0

    def test_describe_table_round_trips_via_call_tool(self, server_with_one_table) -> None:
        _content, structured = asyncio.run(
            server_with_one_table.call_tool("describe_table", {"qualified_name": "public.users"})
        )
        assert structured["qualified_name"] == "public.users"
        assert structured["schema_name"] == "public"
        assert structured["name"] == "users"
        assert len(structured["columns"]) == 1
        assert structured["columns"][0]["name"] == "email"
        assert structured["foreign_keys"] == []
        assert structured["token_estimate"] > 0

    def test_describe_table_unknown_name_propagates_error(self, server_with_one_table) -> None:
        # FastMCP wraps tool exceptions as ToolError; the underlying
        # message must still mention the missing qualified name so the
        # client-side error tells the agent what went wrong.
        with pytest.raises(Exception) as exc_info:
            asyncio.run(
                server_with_one_table.call_tool("describe_table", {"qualified_name": "public.nope"})
            )
        assert "public.nope" in str(exc_info.value)

    def test_describe_table_malformed_name_propagates_error(self, server_with_one_table) -> None:
        with pytest.raises(Exception) as exc_info:
            asyncio.run(
                server_with_one_table.call_tool("describe_table", {"qualified_name": "nodot"})
            )
        assert "qualified_name" in str(exc_info.value)

    def test_describe_column_round_trips_via_call_tool(self, server_with_one_table) -> None:
        _content, structured = asyncio.run(
            server_with_one_table.call_tool(
                "describe_column", {"qualified_name": "public.users.email"}
            )
        )
        assert structured["qualified_name"] == "public.users.email"
        assert structured["schema_name"] == "public"
        assert structured["table_name"] == "users"
        assert structured["name"] == "email"
        assert structured["outgoing_foreign_keys"] == []
        assert structured["incoming_foreign_keys"] == []
        assert structured["token_estimate"] > 0

    def test_describe_column_unknown_column_propagates_error(self, server_with_one_table) -> None:
        with pytest.raises(Exception) as exc_info:
            asyncio.run(
                server_with_one_table.call_tool(
                    "describe_column", {"qualified_name": "public.users.nope"}
                )
            )
        # ColumnNotFoundError message format includes the qualified name.
        assert "public.users.nope" in str(exc_info.value)

    def test_describe_column_malformed_name_propagates_error(self, server_with_one_table) -> None:
        with pytest.raises(Exception) as exc_info:
            asyncio.run(
                server_with_one_table.call_tool(
                    "describe_column", {"qualified_name": "only_two.parts"}
                )
            )
        assert "qualified_name" in str(exc_info.value)


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
