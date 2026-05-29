"""Shared harness for firewall stress tests.

Provides:
  - `build_test_server(...)` — wires a `build_server` in-process with a
    real `AuditWriter` backed by a temp SQLite file, plus an optional
    `EngineMetricExecutor` against the live Postgres demo DB.
  - `call_tool_sync(...)` — runs `app.call_tool(...)` from sync code
    (each call inside its own asyncio event loop). Designed for use
    inside `ThreadPoolExecutor` workers where each thread drives one
    independent call. Returns the structured envelope dict.
  - `count_audit_rows(...)` / `verify_chain(...)` — convenience wrappers
    around the audit DB.

This module is test-only and does NOT modify `schemabrain/` source.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from schemabrain.audit.verify import ChainMismatch, walk_chain
from schemabrain.audit.writer import AuditWriter
from schemabrain.connectors._url import safe_engine_url
from schemabrain.core.store import SQLiteStore
from schemabrain.enrichment.embeddings import Embedder
from schemabrain.mcp import build_server
from schemabrain.mcp.metric_executor import EngineMetricExecutor


class _CheapStubEmbedder:
    """Deterministic 4-dim embedder so embedding cache lookups never
    fail and we measure transport + audit cost, not ONNX inference.
    """

    model_name = "stub-emb"
    dimension = 4

    def embed(self, text: str) -> tuple[float, ...]:
        # Single-bucket vector — every query hits the same direction so
        # cosine-vs-stored remains well-defined for any row.
        del text
        return (1.0, 0.0, 0.0, 0.0)


@dataclass(frozen=True)
class HarnessHandle:
    app: FastMCP
    store: SQLiteStore
    audit_writer: AuditWriter
    store_path: Path
    source_connection_id: str


def build_test_server(
    *,
    store_path: Path,
    source_connection_id: str = "src1",
    pg_url: str | None = None,
    statement_timeout_ms: int | None = None,
    max_rows_per_result: int | None = None,
    embedder: Embedder | None = None,
) -> HarnessHandle:
    """Wire `build_server` against a pre-indexed store.

    When `pg_url` is supplied, an `EngineMetricExecutor` is constructed
    that mirrors the production engine wiring in `cli.py::_cmd_serve`
    (NullPool, `default_transaction_read_only=on`, optional
    `statement_timeout`).
    """
    store = SQLiteStore(store_path)
    audit_writer = AuditWriter(store_path)
    metric_executor: EngineMetricExecutor | None = None
    if pg_url is not None:
        options_parts: list[str] = ["-c default_transaction_read_only=on"]
        if statement_timeout_ms is not None:
            options_parts.append(f"-c statement_timeout={statement_timeout_ms}")
        engine = create_engine(
            safe_engine_url(pg_url),
            poolclass=NullPool,
            connect_args={"options": " ".join(options_parts)},
        )
        metric_executor = EngineMetricExecutor(engine, max_rows=max_rows_per_result)

    app = build_server(
        store=store,
        source_connection_id=source_connection_id,
        embedder=embedder or _CheapStubEmbedder(),
        metric_executor=metric_executor,
        audit_writer=audit_writer,
    )
    return HarnessHandle(
        app=app,
        store=store,
        audit_writer=audit_writer,
        store_path=store_path,
        source_connection_id=source_connection_id,
    )


def call_tool_sync(app: FastMCP, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Drive `app.call_tool` from sync code; return structured envelope.

    Each call gets a fresh event loop so this is safe to fan out across
    a `ThreadPoolExecutor`. The cost is small relative to MCP dispatch +
    audit write — measured at ~0.1ms loop construction.
    """
    loop = asyncio.new_event_loop()
    try:
        _content, structured = loop.run_until_complete(app.call_tool(name, args))
        return structured  # type: ignore[no-any-return]
    finally:
        loop.close()


def count_audit_rows(store_path: Path) -> int:
    conn = sqlite3.connect(str(store_path))
    try:
        row = conn.execute("SELECT count(*) FROM mcp_audit").fetchone()
        return int(row[0])
    finally:
        conn.close()


def verify_chain(store_path: Path) -> Sequence[ChainMismatch]:
    """Run full audit-chain walk; return every detected mismatch.

    Empty sequence = chain intact.
    """
    conn = sqlite3.connect(str(store_path))
    conn.row_factory = sqlite3.Row
    try:
        return list(walk_chain(conn, full=True))
    finally:
        conn.close()


def audit_size_bytes(store_path: Path) -> int:
    return store_path.stat().st_size if store_path.exists() else 0
