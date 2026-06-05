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
    assert payload["dashboard_schema_version"] == "1.7"
    assert payload["fingerprint_version"] == "fp-v1"
    assert payload["source_connection_ids"] == []


def test_meta_sources_empty_on_empty_store(client: TestClient) -> None:
    payload = client.get("/api/meta").json()
    assert payload["sources"] == []


def test_meta_sources_rollup_after_index(client: TestClient, store_path) -> None:
    """A label source surfaces in `sources` with honest counts: state is
    always "indexed", engine is None (a label is not a connection URL),
    and last_indexed_at is a real timestamp once a table exists."""
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
                        data_type="bigint",
                        nullable=False,
                        ordinal_position=1,
                        is_primary_key=True,
                    ),
                ),
            ),
            source_connection_id="demo-source",
        )
        store.write_entity(
            Entity(
                name="customer",
                description="",
                binding=SingleTableBinding(qualified_table="public.users"),
                identity="id",
                origin="manual",
            ),
            source_connection_id="demo-source",
        )

    payload = client.get("/api/meta").json()
    assert payload["source_connection_ids"] == ["demo-source"]
    assert len(payload["sources"]) == 1
    source = payload["sources"][0]
    assert source["source_id"] == "demo-source"
    assert source["state"] == "indexed"
    assert source["tables"] == 1
    assert source["entities"] == 1
    assert isinstance(source["last_indexed_at"], int)
    # 'demo-source' is a label, not a connection URL → dialect unknown.
    assert source["engine"] is None


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
        assert response.headers["X-Schemabrain-Dashboard-Schema"] == "1.7"


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


def test_merkle_root_empty_store_is_sha256_of_empty(client: TestClient) -> None:
    import hashlib

    response = client.get("/api/audit/merkle/root")
    assert response.status_code == 200
    payload = response.json()
    assert payload["tree_size"] == 0
    # Empty tree root is the well-known SHA-256 of the empty string,
    # never null and never all-zeros.
    assert payload["root_hex"] == hashlib.sha256(b"").hexdigest()


def test_merkle_root_after_seeded_rows(client: TestClient, seed_refusal) -> None:
    seed_refusal()
    seed_refusal()
    seed_refusal()
    payload = client.get("/api/audit/merkle/root").json()
    assert payload["tree_size"] == 3
    assert len(payload["root_hex"]) == 64
    assert all(c in "0123456789abcdef" for c in payload["root_hex"])


def test_row_proof_reconciles_to_global_root(client: TestClient, seed_refusal) -> None:
    """The proof returned for a real row, verified independently against
    the separately-fetched global root, must reconcile — the exact check
    the browser performs."""
    from schemabrain.audit.merkle import verify_inclusion

    ids = [seed_refusal(), seed_refusal(), seed_refusal()]
    root_hex = client.get("/api/audit/merkle/root").json()["root_hex"]

    for row_id in ids:
        proof = client.get(f"/api/audit/rows/{row_id}/proof").json()
        assert proof["tree_size"] == 3
        assert proof["root_hex"] == root_hex
        assert verify_inclusion(
            proof["leaf_index"],
            proof["tree_size"],
            bytes.fromhex(proof["leaf_hash_hex"]),
            [bytes.fromhex(h) for h in proof["audit_path"]],
            bytes.fromhex(root_hex),
        )


def test_row_proof_leaf_index_is_id_ascending(client: TestClient, seed_refusal) -> None:
    """leaf_index is the id-ASC position, NOT the id-DESC render order —
    the lowest-id row is leaf 0 (guards the rows-route DESC footgun)."""
    ids = sorted([seed_refusal(), seed_refusal(), seed_refusal()])
    assert client.get(f"/api/audit/rows/{ids[0]}/proof").json()["leaf_index"] == 0
    assert client.get(f"/api/audit/rows/{ids[-1]}/proof").json()["leaf_index"] == 2


def test_row_proof_404_for_missing(client: TestClient) -> None:
    assert client.get("/api/audit/rows/999/proof").status_code == 404


def test_row_proof_422_for_non_int_id(client: TestClient) -> None:
    assert client.get("/api/audit/rows/not-an-int/proof").status_code == 422


def test_merkle_routes_422_on_uncanonicalisable_row(
    client: TestClient, seed_refusal, store_path
) -> None:
    """A row whose canonical shape is broken (corruption / future-schema drift)
    must surface as an actionable 422, never a 500 — the routes meant to detect
    tampering must not crash on a malformed row."""
    import sqlite3

    seed_refusal()
    # Simulate an out-of-band corruption (drop the append-only guard, then write
    # a fingerprint of the wrong width so canonical_audit_row rejects it).
    conn = sqlite3.connect(str(store_path))
    conn.execute("DROP TRIGGER IF EXISTS mcp_audit_no_update")
    conn.execute(
        "UPDATE mcp_audit SET fingerprint = X'aabb' WHERE id = (SELECT min(id) FROM mcp_audit)"
    )
    conn.commit()
    conn.close()

    root = client.get("/api/audit/merkle/root")
    assert root.status_code == 422
    assert "canonicalisation" in root.json()["detail"]

    rows = client.get("/api/audit/rows").json()["items"]
    assert rows, "rows route should still serve the (corrupt) row"
    proof = client.get(f"/api/audit/rows/{rows[0]['id']}/proof")
    assert proof.status_code == 422


def test_single_row_proof_has_empty_path(client: TestClient, seed_refusal) -> None:
    row_id = seed_refusal()
    proof = client.get(f"/api/audit/rows/{row_id}/proof").json()
    assert proof["tree_size"] == 1
    assert proof["leaf_index"] == 0
    assert proof["audit_path"] == []
    # The sole leaf IS the root.
    assert proof["leaf_hash_hex"] == proof["root_hex"]


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


def test_pii_matrix_includes_per_column_confidence(store_path, client: TestClient) -> None:
    """ADR 0009 — the matrix carries a per-column band for the heatmap."""
    from schemabrain.core.entity import Entity, SingleTableBinding
    from schemabrain.core.models import Column, Table
    from schemabrain.core.store import SQLiteStore

    src = "conf-source"
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
                "password_hash": ("pii", frozenset({"credential"})),
                "email": ("pii", frozenset({"contact"})),
            },
            confidence={
                "password_hash": ("floor_locked", None),
                "email": ("high", 0.9),
            },
        )

    response = client.get(f"/api/entities/pii-matrix?source_connection_id={src}")
    assert response.status_code == 200
    payload = response.json()

    columns = {c["column_name"]: c for c in payload["columns"]}
    assert set(columns) == {"id", "password_hash", "email"}

    pw = columns["password_hash"]
    assert pw["pii_confidence"] == "floor_locked"
    assert pw["categories"] == ["credential"]
    assert pw["sensitivity"] == "pii"
    assert pw["qualified_table"] == "public.users"
    assert pw["entity"] == "user"

    assert columns["email"]["pii_confidence"] == "high"
    assert columns["email"]["categories"] == ["contact"]

    # Public column — no band, no categories, never a fabricated 0.
    assert columns["id"]["pii_confidence"] is None
    assert columns["id"]["categories"] == []
    assert columns["id"]["sensitivity"] == "public"


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
