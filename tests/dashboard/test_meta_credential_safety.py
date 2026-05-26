"""Regression tests for /api/meta — never echo DB credentials.

The sidecar accepts ``SidecarConfig.source_connection_id`` as a string
that the boot script (e.g. ``schemabrain dashboard``) passes through.
Some callers historically passed the raw connection URL; the meta
route then echoed it back, leaking the DB password to anyone with
local-network access to 127.0.0.1:7878.

These tests pin the invariant that the meta response never contains
``://`` patterns with credentials in the source_id-shaped fields,
regardless of what the config holds.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from schemabrain.dashboard.sidecar import is_ui_available

pytestmark = pytest.mark.skipif(
    not is_ui_available(),
    reason="`schemabrain[ui]` extra not installed",
)

if TYPE_CHECKING:
    pass


_CREDENTIAL_URL = "postgresql+psycopg://alice:s3cr3t-pass@prod-db.internal:5432/orders"
_BARE_PASSWORD = "s3cr3t-pass"


def _client_with_config_source(store_path: Path, source: str):
    from fastapi.testclient import TestClient

    from schemabrain.dashboard.sidecar import SidecarConfig, create_sidecar

    app = create_sidecar(SidecarConfig(store_path=store_path, source_connection_id=source))
    return TestClient(app)


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


def test_meta_redacts_password_when_config_holds_url(tmp_path: Path) -> None:
    """The raw URL with credentials must never appear in /api/meta."""
    store_path = _empty_store(tmp_path)
    with _client_with_config_source(store_path, _CREDENTIAL_URL) as client:
        response = client.get("/api/meta")
        assert response.status_code == 200
        body_text = response.text
        assert _BARE_PASSWORD not in body_text, (
            "DB password leaked through /api/meta — see "
            "schemabrain/core/source_id.py for the canonical hash function"
        )
        assert "alice" not in body_text, "DB username leaked through /api/meta"


def test_meta_default_source_is_hashed_when_config_url_given(tmp_path: Path) -> None:
    """When config.source_connection_id is a URL, default_source_connection_id must be its hash."""
    from schemabrain.core.source_id import make_source_id

    store_path = _empty_store(tmp_path)
    expected_hash = make_source_id(_CREDENTIAL_URL)
    with _client_with_config_source(store_path, _CREDENTIAL_URL) as client:
        payload = client.get("/api/meta").json()
        assert payload["default_source_connection_id"] == expected_hash


def test_meta_default_source_preserved_when_config_is_already_a_label(
    tmp_path: Path,
) -> None:
    """When config holds a non-URL label, it's safe to echo as-is."""
    store_path = _empty_store(tmp_path)
    with _client_with_config_source(store_path, "demo-source") as client:
        payload = client.get("/api/meta").json()
        assert payload["default_source_connection_id"] == "demo-source"


def test_meta_default_source_falls_back_to_first_indexed_source(tmp_path: Path) -> None:
    """When config provides no source, fall back to the store's first registered source."""
    from schemabrain.core.models import Column, Table
    from schemabrain.core.store import SQLiteStore

    store_path = _empty_store(tmp_path)
    with SQLiteStore(store_path) as store:
        store.write_table(
            Table(
                schema_name="public",
                name="t",
                columns=(
                    Column(
                        schema_name="public",
                        table_name="t",
                        name="id",
                        data_type="integer",
                        nullable=False,
                        ordinal_position=1,
                    ),
                ),
            ),
            source_connection_id="indexed-source-1",
        )
    from fastapi.testclient import TestClient

    from schemabrain.dashboard.sidecar import SidecarConfig, create_sidecar

    app = create_sidecar(SidecarConfig(store_path=store_path))
    with TestClient(app) as client:
        payload = client.get("/api/meta").json()
        assert payload["default_source_connection_id"] == "indexed-source-1"


def test_meta_response_contains_no_credential_url_pattern(tmp_path: Path) -> None:
    """Defensive regex: no ``://USER:PASSWORD@HOST`` pattern in /api/meta response."""
    store_path = _empty_store(tmp_path)
    with _client_with_config_source(store_path, _CREDENTIAL_URL) as client:
        body = client.get("/api/meta").text
    cred_url_pattern = re.compile(r"://[^/:\s\"]+:[^/@\s\"]+@[^\s\"]+")
    matches = cred_url_pattern.findall(body)
    assert not matches, f"credential URL pattern leaked in /api/meta: {matches}"
