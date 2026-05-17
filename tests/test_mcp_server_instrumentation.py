"""Integration tests: build_server + capturing bus = events flow."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP

from schemabrain.core.embedding import ColumnEmbedding
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore
from schemabrain.mcp import build_server
from schemabrain.observability.event import Event


class _CapturingBus:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)

    def close(self) -> None:
        return


class _StubEmbedder:
    def embed(self, text: str) -> tuple[float, ...]:
        return (1.0, 0.0, 0.0, 0.0)


def _column(name: str, *, ordinal_position: int = 1) -> Column:
    return Column(
        name=name,
        table_name="users",
        schema_name="public",
        data_type="TEXT",
        nullable=False,
        ordinal_position=ordinal_position,
    )


@pytest.fixture
def seeded_store_with_bus(
    tmp_path: Path,
) -> tuple[FastMCP, _CapturingBus, SQLiteStore]:
    store = SQLiteStore(tmp_path / "store.db")
    sid = "src1"
    store.write_table(
        Table(
            schema_name="public",
            name="users",
            kind="TABLE",
            columns=(_column("email", ordinal_position=1),),
        ),
        source_connection_id=sid,
    )
    store.write_table_embeddings(
        "public",
        "users",
        source_connection_id=sid,
        embeddings={"email": ColumnEmbedding(vector=(1.0, 0.0, 0.0, 0.0), model="t", dimension=4)},
    )
    bus = _CapturingBus()
    app = build_server(
        store=store,
        source_connection_id=sid,
        embedder=_StubEmbedder(),
        event_bus=bus,
        server_session_id="test-session-uuid",
    )
    return app, bus, store


async def _call_tool(app: FastMCP, name: str, arguments: dict[str, Any]) -> Any:
    return await app.call_tool(name, arguments)


def _last_text_content(content: Any) -> str:
    if isinstance(content, tuple):
        content = content[0]
    if not content:
        raise AssertionError("no content returned")
    block = content[0]
    return block.text


class TestEventBusEmits:
    def test_find_relevant_tables_emits_one_event(
        self,
        seeded_store_with_bus: tuple[FastMCP, _CapturingBus, SQLiteStore],
    ) -> None:
        app, bus, _ = seeded_store_with_bus
        asyncio.run(_call_tool(app, "find_relevant_tables", {"query": "email"}))
        assert len(bus.events) == 1
        ev = bus.events[0]
        assert ev.tool_name == "find_relevant_tables"
        assert ev.server_session_id == "test-session-uuid"
        assert ev.kind == "tool_call"
        assert ev.status in {"success", "empty"}
        assert ev.duration_ms is not None
        assert "query" in ev.args_summary

    def test_describe_table_emits_with_args(
        self,
        seeded_store_with_bus: tuple[FastMCP, _CapturingBus, SQLiteStore],
    ) -> None:
        app, bus, _ = seeded_store_with_bus
        asyncio.run(_call_tool(app, "describe_table", {"qualified_name": "public.users"}))
        assert len(bus.events) == 1
        assert bus.events[0].tool_name == "describe_table"
        assert bus.events[0].args_summary["qualified_name"] == "public.users"

    def test_unknown_table_emits_error_event(
        self,
        seeded_store_with_bus: tuple[FastMCP, _CapturingBus, SQLiteStore],
    ) -> None:
        app, bus, _ = seeded_store_with_bus
        asyncio.run(_call_tool(app, "describe_table", {"qualified_name": "public.nope"}))
        assert len(bus.events) == 1
        ev = bus.events[0]
        assert ev.tool_name == "describe_table"
        assert ev.status in {"error", "refused"}
        assert ev.error_kind == "unknown_name"

    def test_two_calls_two_events(
        self,
        seeded_store_with_bus: tuple[FastMCP, _CapturingBus, SQLiteStore],
    ) -> None:
        app, bus, _ = seeded_store_with_bus
        asyncio.run(_call_tool(app, "find_relevant_tables", {"query": "a"}))
        asyncio.run(_call_tool(app, "find_relevant_tables", {"query": "b"}))
        assert len(bus.events) == 2

    def test_session_id_constant_across_calls(
        self,
        seeded_store_with_bus: tuple[FastMCP, _CapturingBus, SQLiteStore],
    ) -> None:
        app, bus, _ = seeded_store_with_bus
        asyncio.run(_call_tool(app, "find_relevant_tables", {"query": "a"}))
        asyncio.run(_call_tool(app, "find_relevant_tables", {"query": "b"}))
        assert bus.events[0].server_session_id == bus.events[1].server_session_id


class TestBusOptional:
    def test_no_bus_passes_does_not_break_tools(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "s.db")
        sid = "src1"
        store.write_table(
            Table(
                schema_name="public",
                name="users",
                kind="TABLE",
                columns=(_column("email"),),
            ),
            source_connection_id=sid,
        )
        store.write_table_embeddings(
            "public",
            "users",
            source_connection_id=sid,
            embeddings={
                "email": ColumnEmbedding(vector=(1.0, 0.0, 0.0, 0.0), model="t", dimension=4)
            },
        )
        # No event_bus passed → NullEventBus default.
        app = build_server(store=store, source_connection_id=sid, embedder=_StubEmbedder())
        result = asyncio.run(_call_tool(app, "find_relevant_tables", {"query": "email"}))
        # Tool still works.
        text = _last_text_content(result)
        envelope = json.loads(text)
        assert envelope["status"] in {"success", "empty"}
        store.close()

    def test_server_session_id_auto_generated_when_omitted(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "s.db")
        sid = "src1"
        store.write_table(
            Table(
                schema_name="public",
                name="users",
                kind="TABLE",
                columns=(_column("email"),),
            ),
            source_connection_id=sid,
        )
        store.write_table_embeddings(
            "public",
            "users",
            source_connection_id=sid,
            embeddings={
                "email": ColumnEmbedding(vector=(1.0, 0.0, 0.0, 0.0), model="t", dimension=4)
            },
        )
        bus = _CapturingBus()
        app = build_server(
            store=store,
            source_connection_id=sid,
            embedder=_StubEmbedder(),
            event_bus=bus,
            # server_session_id omitted → auto UUID
        )
        asyncio.run(_call_tool(app, "find_relevant_tables", {"query": "email"}))
        assert bus.events[0].server_session_id  # non-empty UUID string
        store.close()
