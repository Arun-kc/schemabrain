"""Shared fixtures for dashboard sidecar tests.

The fixtures here build an empty store + audit schema in a tmp dir and
yield a constructed FastAPI app. Tests opt into pre-seeded rows via
helper fixtures (refusal seed, regular tool-call seed) defined here so
each test file does not re-implement the row construction.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from schemabrain.dashboard.sidecar import is_ui_available

pytestmark = pytest.mark.skipif(
    not is_ui_available(),
    reason="`schemabrain[ui]` extra not installed",
)

if TYPE_CHECKING:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    """A fresh SQLite store + initialised audit schema."""
    from schemabrain.audit.ddl import ensure_audit_schema
    from schemabrain.core.store import SQLiteStore

    path = tmp_path / "store.db"
    # Initialise the core schema via SQLiteStore so `mcp_audit`
    # writers can later land rows without FK violations.
    store = SQLiteStore(path)
    store.close()
    conn = sqlite3.connect(str(path))
    ensure_audit_schema(conn)
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def app(store_path: Path) -> FastAPI:
    from schemabrain.dashboard.sidecar import SidecarConfig, create_sidecar

    return create_sidecar(SidecarConfig(store_path=store_path))


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c


@pytest.fixture
def seed_refusal(store_path: Path):
    """Helper that returns a function to write a refusal audit row."""

    def _seed() -> int:
        from schemabrain.audit.writer import AuditWriter
        from schemabrain.mcp.envelope import (
            Recovery,
            ToolError,
            ToolResponse,
        )

        writer = AuditWriter(store_path)
        try:
            envelope: ToolResponse[None] = ToolResponse(
                status="refused",
                error=ToolError(
                    kind="pii_blocked",
                    message="Refused: would touch payment_card",
                    recovery=Recovery(),
                    pii_categories=("payment_card",),
                ),
            )
            from schemabrain.audit.writer import build_audit_row

            draft = build_audit_row(
                tool_name="get_metric",
                source_connection_id="test-source",
                response=envelope,
            )
            row = writer.write(draft)
            return row.id
        finally:
            writer.close()

    return _seed
