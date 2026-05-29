"""Audit-chain integrity under concurrent MCP calls (FW-008 verification).

A medium-confidence race was flagged: `AuditWriter.write` computes
`chain_hash = sha256(prev || canonical)` where `prev` is the writer's
in-memory `_last_chain_hash`. If two threads enter `write` and the
mutation of `_last_chain_hash` is not protected by the SQLite
serialise + the `self._lock` mutex, two concurrent calls could derive
the same `prev`, write two rows whose `chain_hash` shares a parent,
and `audit verify` would later report a break.

This test fires 200 concurrent calls across 32 worker threads against
the live `build_server` audit-writer path and asserts:
  1. Every call lands an audit row (no swallowed writes).
  2. The full chain walk yields ZERO mismatches.

Marked as a regression gate. If a future refactor removes the lock
around `_last_chain_hash` mutation, this test fails with a concrete
mismatch dump pointing at the broken row id.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from schemabrain.audit.writer import AuditWriter
from schemabrain.core.embedding import ColumnEmbedding
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore
from schemabrain.mcp import build_server
from tests.firewall._harness import call_tool_sync, verify_chain

_PG_URL = os.environ.get(
    "DBURL",
    "postgresql+psycopg://postgres:local@127.0.0.1:5433/postgres",
)


class _StubEmbedder:
    model_name = "stub"
    dimension = 4

    def embed(self, text: str) -> tuple[float, ...]:
        del text
        return (1.0, 0.0, 0.0, 0.0)


@pytest.mark.parametrize(
    "concurrency,total_calls",
    [
        (32, 200),
        (64, 400),
    ],
)
def test_audit_chain_holds_under_concurrent_writes(
    tmp_path: Path,
    concurrency: int,
    total_calls: int,
) -> None:
    """Fire `total_calls` concurrent MCP calls; verify chain integrity.

    Uses an in-process store + audit writer (no Postgres dependency)
    so the result isolates writer-side concurrency from connector noise.
    """
    store_path = tmp_path / "store.db"
    store = SQLiteStore(store_path)
    sid = "src1"
    store.write_table(
        Table(
            name="orders",
            schema_name="public",
            columns=(
                Column(
                    name="id",
                    table_name="orders",
                    schema_name="public",
                    data_type="INT",
                    nullable=False,
                    ordinal_position=1,
                ),
            ),
        ),
        source_connection_id=sid,
    )
    store.write_table_embeddings(
        "public",
        "orders",
        source_connection_id=sid,
        embeddings={
            "id": ColumnEmbedding(vector=(1.0, 0.0, 0.0, 0.0), model="stub", dimension=4),
        },
    )
    audit_writer = AuditWriter(store_path)
    app = build_server(
        store=store,
        source_connection_id=sid,
        embedder=_StubEmbedder(),
        audit_writer=audit_writer,
    )

    def one_call(_idx: int) -> str:
        env = call_tool_sync(app, "describe_table", {"qualified_name": "public.orders"})
        return env.get("status", "?")

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        statuses = list(ex.map(one_call, range(total_calls)))

    audit_writer.close()
    store.close()

    # Every call must have succeeded; if writer drops calls under
    # contention, count will be wrong.
    import sqlite3 as _sqlite3

    conn = _sqlite3.connect(str(store_path))
    try:
        row_count = conn.execute("SELECT count(*) FROM mcp_audit").fetchone()[0]
    finally:
        conn.close()

    assert row_count == total_calls, (
        f"expected {total_calls} audit rows, got {row_count} — writer "
        f"dropped {total_calls - row_count} concurrent writes "
        f"(statuses sampled: {statuses[:5]})"
    )

    mismatches = verify_chain(store_path)
    assert not mismatches, (
        f"audit chain broken under concurrency={concurrency}: "
        f"{len(mismatches)} mismatch(es). First: row_id="
        f"{mismatches[0].row_id} expected={mismatches[0].expected_hex[:16]}... "
        f"actual={mismatches[0].actual_hex[:16]}..."
    )
