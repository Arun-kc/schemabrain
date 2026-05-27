"""End-to-end smoke tests for the 8 read-only routes + SSE + health.

Each route returns documented shape against an empty (or seeded)
store. Coverage target: every route hit at least once with the
"happy path" + at least one validation error path.

SSE is asserted at the route-existence + content-type level only;
streaming semantics are E2E-only (Playwright) per master plan R-6.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from schemabrain.dashboard.sidecar import is_ui_available

pytestmark = pytest.mark.skipif(
    not is_ui_available(),
    reason="`schemabrain[ui]` extra not installed",
)

if TYPE_CHECKING:
    from fastapi.testclient import TestClient


def test_health_returns_ok(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["store"] == "ok"
    assert payload["uptime_s"] >= 0


def test_meta_returns_charter_info(client: TestClient) -> None:
    response = client.get("/api/meta")
    assert response.status_code == 200
    payload = response.json()
    assert payload["charter_version"] == "1.2"
    assert payload["dashboard_schema_version"] == "1.0"
    assert payload["fingerprint_version"] == "fp-v1"
    assert payload["source_connection_ids"] == []


def test_charter_version_header_set_on_every_response(client: TestClient) -> None:
    for path in [
        "/api/health",
        "/api/meta",
        "/api/audit/rows",
        "/api/audit/verify",
        "/api/audit/refusals",
    ]:
        response = client.get(path)
        assert response.headers["X-Schemabrain-Charter-Version"] == "1.2"
        assert response.headers["X-Schemabrain-Dashboard-Schema"] == "1.0"


def test_entities_route_409_when_no_sources(client: TestClient) -> None:
    response = client.get("/api/entities")
    # Empty store has no indexed source — surface as 409 Conflict
    # rather than 500 so the UI can render an actionable empty state.
    assert response.status_code == 409
    assert "schemabrain index" in response.json()["detail"]


def test_audit_rows_empty_store(client: TestClient) -> None:
    response = client.get("/api/audit/rows")
    assert response.status_code == 200
    payload = response.json()
    assert payload["items"] == []
    assert payload["total"] == 0
    assert payload["limit"] == 50
    assert payload["offset"] == 0


def test_audit_rows_validates_pagination(client: TestClient) -> None:
    assert client.get("/api/audit/rows?limit=0").status_code == 400
    assert client.get("/api/audit/rows?limit=501").status_code == 400
    assert client.get("/api/audit/rows?offset=-1").status_code == 400


def test_audit_verify_intact_on_empty_store(client: TestClient) -> None:
    response = client.get("/api/audit/verify")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "intact"
    assert payload["mismatches"] == []


def test_audit_refusals_empty_store(client: TestClient) -> None:
    response = client.get("/api/audit/refusals")
    assert response.status_code == 200
    assert response.json()["items"] == []


def test_audit_refusals_after_seeded_row(client: TestClient, seed_refusal) -> None:
    row_id = seed_refusal()
    response = client.get("/api/audit/refusals")
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["id"] == row_id
    assert payload["items"][0]["status"] == "refused"
    assert payload["items"][0]["refusal_reason"] == "pii_blocked"
    assert payload["items"][0]["pii_categories"] == ["payment_card"]


def test_refusal_detail_includes_reconstructed_envelope(client: TestClient, seed_refusal) -> None:
    row_id = seed_refusal()
    response = client.get(f"/api/audit/refusals/{row_id}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["envelope_reconstructed"] is True
    envelope = payload["envelope"]
    assert envelope["status"] == "refused"
    assert envelope["data"] is None
    assert envelope["error"]["kind"] == "pii_blocked"
    assert envelope["error"]["pii_categories"] == ["payment_card"]
    assert envelope["charter_version"] == "1.2"


def test_audit_row_detail_404_for_missing(client: TestClient) -> None:
    assert client.get("/api/audit/rows/999").status_code == 404


def test_audit_refusal_detail_404_for_missing(client: TestClient) -> None:
    assert client.get("/api/audit/refusals/999").status_code == 404


def test_audit_verify_400_on_malformed_since(client: TestClient) -> None:
    response = client.get("/api/audit/verify?since=not-a-cursor")
    assert response.status_code == 400


def test_sse_stream_route_exists(app) -> None:
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/api/audit/stream" in paths


def test_pii_matrix_409_when_no_sources(client: TestClient) -> None:
    """Empty store has no sources — surface as 409 with actionable detail."""
    response = client.get("/api/entities/pii-matrix")
    assert response.status_code == 409


def test_pii_matrix_returns_aggregated_shape(store_path, client: TestClient) -> None:
    """One round-trip returns every entity + per-category counts + totals."""
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
                    Column(
                        schema_name="public",
                        table_name="users",
                        name="email",
                        data_type="text",
                        nullable=False,
                        ordinal_position=3,
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
                "email": ColumnPiiTag(("pii", frozenset({"contact"}))),
            },
        )

    response = client.get(f"/api/entities/pii-matrix?source_connection_id={src}")
    assert response.status_code == 200
    payload = response.json()

    assert payload["source_connection_id"] == src
    assert payload["totals"]["entities"] == 1
    assert payload["totals"]["columns"] == 3
    assert payload["totals"]["catastrophic_columns"] == 1
    assert payload["totals"]["pii_columns"] == 2
    assert payload["totals"]["internal_or_public_columns"] == 1

    assert len(payload["entities"]) == 1
    user = payload["entities"][0]
    assert user["name"] == "user"
    assert user["qualified_table"] == "public.users"
    assert user["counts"]["credential"] == 1
    assert user["counts"]["contact"] == 1
    assert user["counts"]["payment_card"] == 0
    assert user["catastrophic_column_count"] == 1
    assert user["has_catastrophic"] is True

    # Pin canonical column order at the route boundary. Previously the
    # sidecar serialized `list(frozenset(...))`, which is hash-driven
    # and reshuffles across Python processes — the dashboard inherited
    # that as a header-order regression. The order below mirrors the
    # `PIICategory` Literal in `schemabrain/pii/categories.py`; the
    # frontend's `PII_CATEGORIES` array in `web/lib/types/pii.ts`
    # carries the same order. The two surfaces MUST agree.
    assert payload["categories"] == [
        "contact",
        "financial",
        "payment_card",
        "health",
        "genetic",
        "biometric",
        "behavioral",
        "online_identifier",
        "credential",
        "government_id",
        "location",
        "demographic_protected",
    ]
    assert set(payload["catastrophic_categories"]) == {
        "credential",
        "payment_card",
        "government_id",
    }


def test_pii_matrix_counts_confidential_and_internal_sensitivities(
    store_path, client: TestClient
) -> None:
    """Cover the confidential + internal sensitivity bucket branches."""
    from schemabrain.core.entity import Entity, SingleTableBinding
    from schemabrain.core.models import Column, Table
    from schemabrain.core.store import SQLiteStore
    from schemabrain.pii.categories import ColumnPiiTag

    src = "test-source"
    with SQLiteStore(store_path) as store:
        store.write_table(
            Table(
                schema_name="public",
                name="mixed",
                columns=(
                    Column(
                        schema_name="public",
                        table_name="mixed",
                        name="conf_col",
                        data_type="text",
                        nullable=False,
                        ordinal_position=1,
                    ),
                    Column(
                        schema_name="public",
                        table_name="mixed",
                        name="int_col",
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
                name="mixed",
                description="Mixed sensitivities.",
                binding=SingleTableBinding(qualified_table="public.mixed"),
                identity="conf_col",
            ),
            source_connection_id=src,
        )
        store.write_column_pii_tags(
            source_connection_id=src,
            qualified_table="public.mixed",
            tags={
                "conf_col": ColumnPiiTag(("confidential", frozenset())),
                "int_col": ColumnPiiTag(("internal", frozenset())),
            },
        )

    response = client.get(f"/api/entities/pii-matrix?source_connection_id={src}")
    payload = response.json()
    assert payload["totals"]["confidential_columns"] == 1
    assert payload["totals"]["internal_or_public_columns"] == 1
    assert payload["totals"]["pii_columns"] == 0


def test_pii_matrix_empty_state_when_no_catastrophic(store_path, client: TestClient) -> None:
    """Entity with no PII columns — totals report zero catastrophic."""
    from schemabrain.core.entity import Entity, SingleTableBinding
    from schemabrain.core.models import Column, Table
    from schemabrain.core.store import SQLiteStore

    src = "test-source"
    with SQLiteStore(store_path) as store:
        store.write_table(
            Table(
                schema_name="public",
                name="feature_flags",
                columns=(
                    Column(
                        schema_name="public",
                        table_name="feature_flags",
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
                name="feature_flag",
                description="A toggleable feature.",
                binding=SingleTableBinding(qualified_table="public.feature_flags"),
                identity="id",
            ),
            source_connection_id=src,
        )

    response = client.get(f"/api/entities/pii-matrix?source_connection_id={src}")
    payload = response.json()
    assert payload["totals"]["catastrophic_columns"] == 0
    assert payload["entities"][0]["has_catastrophic"] is False
