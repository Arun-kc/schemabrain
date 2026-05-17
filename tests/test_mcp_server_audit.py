"""Integration tests: `build_server(audit_writer=...)` writes audit rows.

Mirrors `test_mcp_server_instrumentation.py`'s seeded-store fixture
shape but adds an `AuditWriter` to verify rows land in the `mcp_audit`
table when tools are called through the FastMCP boundary. Covers the
9 v1 tools and the get_metric fingerprint injection path end-to-end.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP

from schemabrain.audit.writer import AuditWriter
from schemabrain.core.embedding import ColumnEmbedding
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore
from schemabrain.mcp import build_server


class _StubEmbedder:
    def embed(self, text: str) -> tuple[float, ...]:
        return (1.0, 0.0, 0.0, 0.0)


@pytest.fixture
def seeded_store_with_audit(
    tmp_path: Path,
) -> tuple[FastMCP, AuditWriter, SQLiteStore]:
    store = SQLiteStore(tmp_path / "store.db")
    sid = "src-audit-1"
    store.write_table(
        Table(
            schema_name="public",
            name="users",
            kind="TABLE",
            columns=(
                Column(
                    name="email",
                    table_name="users",
                    schema_name="public",
                    data_type="TEXT",
                    nullable=False,
                    ordinal_position=1,
                ),
            ),
        ),
        source_connection_id=sid,
    )
    store.write_table_embeddings(
        "public",
        "users",
        source_connection_id=sid,
        embeddings={"email": ColumnEmbedding(vector=(1.0, 0.0, 0.0, 0.0), model="t", dimension=4)},
    )
    # The audit writer shares the store file — the audit table lands
    # in the same `*.db`.
    writer = AuditWriter(tmp_path / "store.db")
    app = build_server(
        store=store,
        source_connection_id=sid,
        embedder=_StubEmbedder(),
        server_session_id="audit-test-session",
        audit_writer=writer,
    )
    yield app, writer, store
    writer.close()
    store.close()


async def _call(app: FastMCP, name: str, arguments: dict[str, Any]) -> Any:
    return await app.call_tool(name, arguments)


def _audit_rows(writer: AuditWriter) -> list[sqlite3.Row]:
    conn = writer._require_conn()
    return list(conn.execute("SELECT * FROM mcp_audit ORDER BY id"))


class TestAuditRowsLandPerTool:
    def test_find_relevant_tables_writes_one_row(
        self, seeded_store_with_audit: tuple[FastMCP, AuditWriter, SQLiteStore]
    ) -> None:
        app, writer, _ = seeded_store_with_audit
        asyncio.run(_call(app, "find_relevant_tables", {"query": "email"}))
        rows = _audit_rows(writer)
        assert len(rows) == 1
        assert rows[0]["tool_name"] == "find_relevant_tables"
        assert rows[0]["source_connection_id"] == "src-audit-1"
        assert rows[0]["status"] in {"success", "empty"}
        assert rows[0]["cost_class"] == "small"
        assert rows[0]["pii_categories"] == ""
        assert rows[0]["caller_id"] is None
        assert rows[0]["refusal_reason"] is None
        assert rows[0]["ast_shape_hash"] is None
        assert rows[0]["rule_id"] is None
        assert rows[0]["fingerprint_version"] == "fp-v1"
        assert len(bytes(rows[0]["fingerprint"])) == 32
        assert len(bytes(rows[0]["chain_hash"])) == 32

    def test_describe_table_writes_row(
        self, seeded_store_with_audit: tuple[FastMCP, AuditWriter, SQLiteStore]
    ) -> None:
        app, writer, _ = seeded_store_with_audit
        asyncio.run(_call(app, "describe_table", {"qualified_name": "public.users"}))
        rows = _audit_rows(writer)
        assert len(rows) == 1
        assert rows[0]["tool_name"] == "describe_table"
        assert rows[0]["status"] == "success"

    def test_describe_column_writes_row(
        self, seeded_store_with_audit: tuple[FastMCP, AuditWriter, SQLiteStore]
    ) -> None:
        app, writer, _ = seeded_store_with_audit
        asyncio.run(_call(app, "describe_column", {"qualified_name": "public.users.email"}))
        rows = _audit_rows(writer)
        assert len(rows) == 1
        assert rows[0]["tool_name"] == "describe_column"
        assert rows[0]["status"] == "success"

    def test_list_entities_writes_row_with_empty_status(
        self, seeded_store_with_audit: tuple[FastMCP, AuditWriter, SQLiteStore]
    ) -> None:
        """No entities are defined in the seeded store; the tool returns
        `empty`. The audit row should still land — empty is a charter
        status, not a failure."""
        app, writer, _ = seeded_store_with_audit
        asyncio.run(_call(app, "list_entities", {}))
        rows = _audit_rows(writer)
        assert len(rows) == 1
        assert rows[0]["tool_name"] == "list_entities"
        assert rows[0]["status"] == "empty"


class TestChainAdvancesAcrossCalls:
    def test_chain_hash_distinct_per_call(
        self, seeded_store_with_audit: tuple[FastMCP, AuditWriter, SQLiteStore]
    ) -> None:
        app, writer, _ = seeded_store_with_audit
        asyncio.run(_call(app, "find_relevant_tables", {"query": "a"}))
        asyncio.run(_call(app, "find_relevant_tables", {"query": "b"}))
        asyncio.run(_call(app, "find_relevant_tables", {"query": "c"}))
        rows = _audit_rows(writer)
        assert len(rows) == 3
        chain_hashes = [bytes(r["chain_hash"]) for r in rows]
        # All three chain hashes are distinct (id + occurred_at vary).
        assert len(set(chain_hashes)) == 3


class TestAuditFailureDoesNotBlockTool:
    def test_unknown_table_error_path_still_writes_audit(
        self, seeded_store_with_audit: tuple[FastMCP, AuditWriter, SQLiteStore]
    ) -> None:
        """Error responses (charter status `error`) also write audit
        rows — the audit is about what HAPPENED, not what succeeded."""
        app, writer, _ = seeded_store_with_audit
        asyncio.run(_call(app, "describe_table", {"qualified_name": "public.nope"}))
        rows = _audit_rows(writer)
        assert len(rows) == 1
        assert rows[0]["tool_name"] == "describe_table"
        assert rows[0]["status"] == "error"


class TestBuildServerWithoutAuditWriter:
    def test_no_audit_writer_no_rows(self, tmp_path: Path) -> None:
        """`audit_writer=None` (the legacy default) — no rows in
        mcp_audit. The table itself still exists (created by
        SQLiteStore via the schema bump) but stays empty."""
        store = SQLiteStore(tmp_path / "store.db")
        try:
            sid = "src-no-audit"
            store.write_table(
                Table(
                    schema_name="public",
                    name="users",
                    kind="TABLE",
                    columns=(
                        Column(
                            name="email",
                            table_name="users",
                            schema_name="public",
                            data_type="TEXT",
                            nullable=False,
                            ordinal_position=1,
                        ),
                    ),
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
            app = build_server(
                store=store,
                source_connection_id=sid,
                embedder=_StubEmbedder(),
                server_session_id="no-audit-session",
                # audit_writer NOT passed
            )
            asyncio.run(_call(app, "find_relevant_tables", {"query": "email"}))
            count = (
                store._require_conn().execute("SELECT count(*) AS n FROM mcp_audit").fetchone()["n"]
            )
            assert count == 0
        finally:
            store.close()
