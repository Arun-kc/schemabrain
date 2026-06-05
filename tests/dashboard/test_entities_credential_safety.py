"""Regression tests for /api/entities — never echo DB credentials.

`_resolve_source` returns an operator-supplied `source_connection_id`
override verbatim, so a URL-shaped override (carrying the DB password)
must be coerced to its canonical hashed id before the route echoes it
back — the same defence /api/meta and /api/drift already apply. These
tests pin that invariant for the entities index route.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from schemabrain.dashboard.sidecar import is_ui_available

pytestmark = pytest.mark.skipif(
    not is_ui_available(),
    reason="`schemabrain[ui]` extra not installed",
)

_CREDENTIAL_URL = "postgresql+psycopg://alice:s3cr3t-pass@prod-db.internal:5432/orders"
_BARE_PASSWORD = "s3cr3t-pass"


def _empty_store(tmp_path: Path) -> Path:
    from schemabrain.audit.ddl import ensure_audit_schema
    from schemabrain.core.store import SQLiteStore

    path = tmp_path / "store.db"
    SQLiteStore(path).close()
    conn = sqlite3.connect(str(path))
    ensure_audit_schema(conn)
    conn.commit()
    conn.close()
    return path


def _client(store_path: Path):
    from fastapi.testclient import TestClient

    from schemabrain.dashboard.sidecar import SidecarConfig, create_sidecar

    return TestClient(create_sidecar(SidecarConfig(store_path=store_path)))


def test_entities_redacts_password_in_source_override(tmp_path: Path) -> None:
    """A URL-shaped override must not surface its password in the response."""
    store_path = _empty_store(tmp_path)
    with _client(store_path) as client:
        response = client.get("/api/entities", params={"source_connection_id": _CREDENTIAL_URL})
        assert response.status_code == 200
        body = response.text
        assert _BARE_PASSWORD not in body, "DB password leaked through /api/entities"
        assert "alice" not in body, "DB username leaked through /api/entities"


def test_entities_echoes_hashed_source_for_url_override(tmp_path: Path) -> None:
    """The echoed source_connection_id is the canonical hash, not the raw URL."""
    from schemabrain.core.source_id import make_source_id

    store_path = _empty_store(tmp_path)
    expected_hash = make_source_id(_CREDENTIAL_URL)
    with _client(store_path) as client:
        payload = client.get(
            "/api/entities", params={"source_connection_id": _CREDENTIAL_URL}
        ).json()
        assert payload["source_connection_id"] == expected_hash


def test_entities_preserves_plain_label_override(tmp_path: Path) -> None:
    """A non-URL label is safe to echo as-is."""
    store_path = _empty_store(tmp_path)
    with _client(store_path) as client:
        payload = client.get("/api/entities", params={"source_connection_id": "demo-source"}).json()
        assert payload["source_connection_id"] == "demo-source"


def test_entities_response_contains_no_credential_url_pattern(tmp_path: Path) -> None:
    """Defensive regex: no ``://USER:PASSWORD@HOST`` pattern in the response."""
    store_path = _empty_store(tmp_path)
    with _client(store_path) as client:
        body = client.get("/api/entities", params={"source_connection_id": _CREDENTIAL_URL}).text
    cred_url_pattern = re.compile(r"://[^/:\s\"]+:[^/@\s\"]+@[^\s\"]+")
    assert not cred_url_pattern.findall(body)
