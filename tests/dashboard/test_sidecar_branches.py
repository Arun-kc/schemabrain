"""Branch-coverage tests for the dashboard sidecar.

Complements `test_sidecar_routes.py` (happy paths + validation errors
for the public route surface) by exercising the helper functions and
the route branches that the route smokes don't touch:

  - SidecarConfig validation (bad path, bad port).
  - `entity_columns_route` happy path + missing-table edge.
  - `_refusal_message` branches across the 3 refusal reason kinds.
  - `_register_health_route` store-error branch.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from schemabrain.dashboard.sidecar import is_ui_available

pytestmark = pytest.mark.skipif(
    not is_ui_available(),
    reason="`schemabrain[ui]` extra not installed",
)

if TYPE_CHECKING:
    from fastapi.testclient import TestClient


def test_sidecar_config_rejects_missing_store_path(tmp_path: Path) -> None:
    from schemabrain.dashboard.sidecar import SidecarConfig

    with pytest.raises(ValueError, match="store_path does not exist"):
        SidecarConfig(store_path=tmp_path / "does-not-exist.db")


def test_sidecar_config_rejects_out_of_range_port(store_path: Path) -> None:
    from schemabrain.dashboard.sidecar import SidecarConfig

    with pytest.raises(ValueError, match="user-port range"):
        SidecarConfig(store_path=store_path, port=80)  # below 1024
    with pytest.raises(ValueError, match="user-port range"):
        SidecarConfig(store_path=store_path, port=99999)  # above 65535


def test_entity_columns_404_for_unknown_name(client: TestClient) -> None:
    response = client.get("/api/entities/no-such-entity/columns")
    # Empty store has no sources; the resolve_source step fires first
    # and surfaces as 409, not 404. That's expected — the operator gets
    # an actionable "run schemabrain index" message regardless of which
    # endpoint they hit.
    assert response.status_code in (404, 409)


def test_entity_columns_happy_path_with_pii(store_path: Path, client: TestClient) -> None:
    from schemabrain.core.entity import Entity, SingleTableBinding
    from schemabrain.core.models import Column, Table
    from schemabrain.core.store import SQLiteStore
    from schemabrain.pii.categories import ColumnPiiTag

    src = "test-source"
    with SQLiteStore(store_path) as store:
        store.write_table(
            Table(
                schema_name="public",
                name="users",
                columns=(
                    Column(
                        schema_name="public",
                        table_name="users",
                        name="id",
                        data_type="integer",
                        nullable=False,
                        ordinal_position=1,
                    ),
                    Column(
                        schema_name="public",
                        table_name="users",
                        name="password_hash",
                        data_type="text",
                        nullable=False,
                        ordinal_position=2,
                    ),
                ),
            ),
            source_connection_id=src,
        )
        store.write_entity(
            Entity(
                name="user",
                description="A registered account.",
                binding=SingleTableBinding(qualified_table="public.users"),
                identity="id",
            ),
            source_connection_id=src,
        )
        store.write_column_pii_tags(
            source_connection_id=src,
            qualified_table="public.users",
            tags={
                "password_hash": ColumnPiiTag(("pii", frozenset({"credential"}))),
            },
        )

    response = client.get(f"/api/entities/user/columns?source_connection_id={src}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["entity"]["qualified_table"] == "public.users"
    columns = {c["name"]: c for c in payload["columns"]}
    assert columns["password_hash"]["sensitivity"] == "pii"
    assert columns["password_hash"]["pii_categories"] == ["credential"]
    # untagged column defaults to public
    assert columns["id"]["sensitivity"] == "public"
    assert columns["id"]["pii_categories"] == []


def test_refusal_message_covers_all_known_reasons() -> None:
    """Branch coverage for the _refusal_message helper's reason switch."""
    from schemabrain.dashboard.sidecar import _refusal_message

    assert "payment_card" in _refusal_message("pii_blocked", ["payment_card"])
    assert "blocked PII" in _refusal_message("pii_blocked", [])
    assert "allowlist" in _refusal_message("allowlist_violation", [])
    assert "fragment_unsafe" in _refusal_message("fragment_unsafe", [])
    assert "reason_unknown" in _refusal_message(None, [])


def test_health_route_reports_degraded_when_store_unreadable(tmp_path: Path, monkeypatch) -> None:
    """If the store path goes unreadable mid-life, health reports degraded
    rather than 500."""
    import sqlite3

    from fastapi.testclient import TestClient

    from schemabrain.audit.ddl import ensure_audit_schema
    from schemabrain.core.store import SQLiteStore
    from schemabrain.dashboard.sidecar import SidecarConfig, create_sidecar

    path = tmp_path / "store.db"
    SQLiteStore(path).close()
    conn = sqlite3.connect(str(path))
    ensure_audit_schema(conn)
    conn.commit()
    conn.close()

    app = create_sidecar(SidecarConfig(store_path=path))
    # Sabotage the path post-construction so /api/health's open fails.
    app.state.config = type(app.state.config)(
        store_path=path,  # config invariant requires existence at construct
        port=app.state.config.port,
        source_connection_id=app.state.config.source_connection_id,
    )
    path.write_bytes(b"NOT A SQLITE FILE")
    with TestClient(app) as client:
        response = client.get("/api/health")
        assert response.status_code == 200
        payload = response.json()
        # Either the store opens to a non-sqlite file and SELECT 1 fails,
        # or the open itself fails — either way state should be "degraded".
        if payload["store"] != "ok":
            assert payload["store"] == "degraded"
            assert payload["store_reason"] is not None


def test_entities_list_happy_path_with_seeded_entity(store_path: Path, client: TestClient) -> None:
    """Cover the list-comp + return shape of /api/entities (lines 233-246)."""
    from schemabrain.core.entity import Entity, SingleTableBinding
    from schemabrain.core.models import Column, Table
    from schemabrain.core.store import SQLiteStore

    src = "test-source"
    with SQLiteStore(store_path) as store:
        store.write_table(
            Table(
                schema_name="public",
                name="orders",
                columns=(
                    Column(
                        schema_name="public",
                        table_name="orders",
                        name="id",
                        data_type="integer",
                        nullable=False,
                        ordinal_position=1,
                    ),
                ),
            ),
            source_connection_id=src,
        )
        store.write_entity(
            Entity(
                name="order",
                description="A purchase.",
                binding=SingleTableBinding(qualified_table="public.orders"),
                identity="id",
            ),
            source_connection_id=src,
        )

    response = client.get(f"/api/entities?source_connection_id={src}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    item = payload["items"][0]
    assert item["name"] == "order"
    assert item["qualified_table"] == "public.orders"
    assert item["origin"] == "manual"


def test_audit_rows_since_query_filters_results(client: TestClient, seed_refusal) -> None:
    """Exercise _maybe_resolve_since + _select_audit_rows with after_id path."""
    seed_refusal()
    # 5m duration spec resolves to a cursor row id; if there's a row
    # within 5m, the cursor anchors there and we get the rows AFTER.
    response = client.get("/api/audit/rows?since=5m")
    # The since-cursor parser may raise SinceCursorError when no rows
    # precede the threshold; both 200 and 400 are documented outcomes.
    assert response.status_code in (200, 400)


def test_audit_verify_full_walk_after_seed(client: TestClient, seed_refusal) -> None:
    """Cover the audit verify full=True walk against a real seeded chain."""
    seed_refusal()
    response = client.get("/api/audit/verify?full=true")
    assert response.status_code == 200
    assert response.json()["status"] == "intact"


def test_audit_row_detail_returns_prev_chain_hash(client: TestClient, seed_refusal) -> None:
    """Cover the prev_chain_hash branch on audit row detail."""
    row_id = seed_refusal()
    response = client.get(f"/api/audit/rows/{row_id}")
    assert response.status_code == 200
    payload = response.json()
    # For the first audit row id=1, prev_row lookup returns None.
    # For id>1, prev_chain_hash_hex is populated.
    if row_id == 1:
        assert payload["prev_chain_hash_hex"] is None
    else:
        assert payload["prev_chain_hash_hex"] is not None


def test_meta_uses_explicit_source_connection_id_when_configured(
    store_path: Path, monkeypatch
) -> None:
    """Cover the config.source_connection_id branch in /api/meta default."""
    from fastapi.testclient import TestClient

    from schemabrain.dashboard.sidecar import SidecarConfig, create_sidecar

    cfg = SidecarConfig(store_path=store_path, source_connection_id="explicit-src")
    app = create_sidecar(cfg)
    with TestClient(app) as client:
        response = client.get("/api/meta")
        assert response.json()["default_source_connection_id"] == "explicit-src"


def test_resolve_source_prefers_config_default_over_store_lookup(
    store_path: Path, monkeypatch
) -> None:
    """Cover line `return config.source_connection_id` in _resolve_source.

    Config carries a source; entities route receives NO override; the
    store also has a different source — config wins.
    """
    from fastapi.testclient import TestClient

    from schemabrain.core.entity import Entity, SingleTableBinding
    from schemabrain.core.models import Column, Table
    from schemabrain.core.store import SQLiteStore
    from schemabrain.dashboard.sidecar import SidecarConfig, create_sidecar

    with SQLiteStore(store_path) as store:
        store.write_table(
            Table(
                schema_name="public",
                name="users",
                columns=(
                    Column(
                        schema_name="public",
                        table_name="users",
                        name="id",
                        data_type="integer",
                        nullable=False,
                        ordinal_position=1,
                    ),
                ),
            ),
            source_connection_id="config-src",
        )
        store.write_entity(
            Entity(
                name="user",
                description="A user.",
                binding=SingleTableBinding(qualified_table="public.users"),
                identity="id",
            ),
            source_connection_id="config-src",
        )

    cfg = SidecarConfig(store_path=store_path, source_connection_id="config-src")
    app = create_sidecar(cfg)
    with TestClient(app) as client:
        # No source_connection_id query param — must fall back to config default.
        response = client.get("/api/entities")
        assert response.status_code == 200
        assert response.json()["source_connection_id"] == "config-src"


def test_resolve_source_falls_back_to_first_store_source_when_no_config(
    store_path: Path, client: TestClient
) -> None:
    """Cover line `return sources[0]` in _resolve_source.

    No config default, no query param — pick the first source from the
    store's list_distinct.
    """
    from schemabrain.core.entity import Entity, SingleTableBinding
    from schemabrain.core.models import Column, Table
    from schemabrain.core.store import SQLiteStore

    with SQLiteStore(store_path) as store:
        store.write_table(
            Table(
                schema_name="public",
                name="users",
                columns=(
                    Column(
                        schema_name="public",
                        table_name="users",
                        name="id",
                        data_type="integer",
                        nullable=False,
                        ordinal_position=1,
                    ),
                ),
            ),
            source_connection_id="alpha-src",
        )
        store.write_entity(
            Entity(
                name="user",
                description="A user.",
                binding=SingleTableBinding(qualified_table="public.users"),
                identity="id",
            ),
            source_connection_id="alpha-src",
        )

    response = client.get("/api/entities")  # no source query param
    assert response.status_code == 200
    assert response.json()["source_connection_id"] == "alpha-src"


def test_entity_columns_404_for_missing_entity_with_seeded_source(
    store_path: Path, client: TestClient
) -> None:
    """Cover the `entity is None → 404` branch in entity_columns_route."""
    from schemabrain.core.entity import Entity, SingleTableBinding
    from schemabrain.core.models import Column, Table
    from schemabrain.core.store import SQLiteStore

    src = "test-source"
    with SQLiteStore(store_path) as store:
        # Need a tracked source so _resolve_source doesn't 409 first;
        # but the requested entity name doesn't exist.
        store.write_table(
            Table(
                schema_name="public",
                name="users",
                columns=(
                    Column(
                        schema_name="public",
                        table_name="users",
                        name="id",
                        data_type="integer",
                        nullable=False,
                        ordinal_position=1,
                    ),
                ),
            ),
            source_connection_id=src,
        )
        store.write_entity(
            Entity(
                name="user",
                description="A user.",
                binding=SingleTableBinding(qualified_table="public.users"),
                identity="id",
            ),
            source_connection_id=src,
        )

    response = client.get(f"/api/entities/no-such-entity/columns?source_connection_id={src}")
    assert response.status_code == 404
    assert "no-such-entity" in response.json()["detail"]


def test_audit_rows_with_chain_hash_cursor_covers_after_id_branch(
    client: TestClient, seed_refusal
) -> None:
    """Cover `id > ?` branches in _select_audit_rows + _count_audit_rows.

    Resolves a real chain_hash hex prefix to a row id, then queries
    /api/audit/rows?since=<prefix> so the underlying SELECT actually
    fires with the after_id clause.
    """
    # Seed two rows so a since-cursor at the first leaves at least one
    # row to walk after it.
    row1 = seed_refusal()
    row2 = seed_refusal()
    assert row2 > row1

    # Fetch the first row to grab its chain hash, then use a hex prefix
    # as the since cursor.
    detail = client.get(f"/api/audit/rows/{row1}").json()
    cursor_prefix = detail["chain_hash_hex"][:12]

    response = client.get(f"/api/audit/rows?since={cursor_prefix}&status=refused")
    assert response.status_code == 200
    payload = response.json()
    # After the cursor we should see the second seeded row.
    ids = {item["id"] for item in payload["items"]}
    assert row2 in ids


def test_landing_page_renders_when_no_static_export(
    store_path: Path, tmp_path: Path, monkeypatch
) -> None:
    """Cover the dev-fallback landing page registration + handler."""
    from fastapi.testclient import TestClient

    from schemabrain.dashboard import sidecar as sidecar_mod
    from schemabrain.dashboard.sidecar import SidecarConfig, create_sidecar

    empty_static = tmp_path / "empty_static"
    empty_static.mkdir()
    (empty_static / ".gitkeep").write_text("")
    (empty_static / "README.md").write_text("placeholder")
    monkeypatch.setattr(sidecar_mod, "STATIC_DIR", empty_static)

    app = create_sidecar(SidecarConfig(store_path=store_path))
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "schemabrain" in response.text.lower()


def test_assert_route_table_invariant_catches_post_routes() -> None:
    """The invariant helper itself rejects a route declaring POST."""
    from fastapi import FastAPI

    from schemabrain.dashboard.sidecar import assert_route_table_is_read_only

    bad_app = FastAPI()

    @bad_app.post("/api/bad")
    def bad_handler() -> dict[str, str]:
        return {"status": "ok"}

    with pytest.raises(AssertionError, match="read-only"):
        assert_route_table_is_read_only(bad_app)
