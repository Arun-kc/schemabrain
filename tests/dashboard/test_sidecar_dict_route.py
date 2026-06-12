"""Tests for the read-only ``GET /api/dict`` data-dictionary route.

The route serialises the SAME ``build_dictionary`` model the ``schemabrain
docs`` CLI renders, so the dashboard surface and its client-side Markdown
export stay aligned with the committed CLI golden. These tests pin the
wire shape, the source scoping, and the read-only / no-source behaviour
against the offline SaaS demo store (the golden's fixture).
"""

from __future__ import annotations

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
    from fastapi.testclient import TestClient


@pytest.fixture
def dict_store(tmp_path: Path) -> Path:
    """A fully-populated offline SaaS store (the golden fixture)."""
    from schemabrain.datadict.demo_store import build_saas_dictionary_store

    path = tmp_path / "dict.db"
    build_saas_dictionary_store(path)
    return path


@pytest.fixture
def dict_client(dict_store: Path) -> Iterator[TestClient]:
    from fastapi.testclient import TestClient

    from schemabrain.dashboard.sidecar import SidecarConfig, create_sidecar

    app = create_sidecar(SidecarConfig(store_path=dict_store))
    with TestClient(app) as c:
        yield c


def test_dict_route_matches_aggregator_model(dict_client: TestClient, dict_store: Path) -> None:
    """The route returns exactly ``dictionary_model_to_json(build_dictionary(...))``."""
    from schemabrain.core.store import SQLiteStore
    from schemabrain.datadict.aggregator import build_dictionary
    from schemabrain.datadict.demo_store import SOURCE_ID
    from schemabrain.datadict.model_json import dictionary_model_to_json

    with SQLiteStore(dict_store) as store:
        expected = dictionary_model_to_json(
            build_dictionary(store=store, source_connection_id=SOURCE_ID)
        )

    response = dict_client.get("/api/dict", params={"source_connection_id": SOURCE_ID})
    assert response.status_code == 200
    assert response.json() == expected


def test_dict_route_wire_shape_is_stable(dict_client: TestClient) -> None:
    """Schema version + the per-column / per-join fields the renderers need."""
    from schemabrain.datadict.demo_store import SOURCE_ID

    payload = dict_client.get("/api/dict", params={"source_connection_id": SOURCE_ID}).json()
    assert payload["schema_version"]  # non-empty store.SCHEMA_VERSION echo
    assert len(payload["sources"]) == 1
    entities = payload["sources"][0]["entities"]
    assert entities, "demo store has curated entities"

    api_key = next(e for e in entities if e["name"] == "api_key")
    assert api_key["qualified_table"] == "public.api_keys"
    assert api_key["group"] == "identity"
    # Columns carry the identity-role + PII fields the golden's table needs
    # and that the per-entity columns route does NOT expose.
    column = next(c for c in api_key["columns"] if c["name"] == "id")
    assert set(column) == {
        "name",
        "data_type",
        "nullable",
        "is_primary_key",
        "is_identity",
        "description",
        "pii_sensitivity",
        "pii_categories",
    }
    assert column["is_primary_key"] is True
    assert column["is_identity"] is True
    # The catastrophic credential column carries its category tag.
    hashed = next(c for c in api_key["columns"] if c["name"] == "api_key_hash")
    assert "credential" in hashed["pii_categories"]


def test_dict_route_join_carries_redacted_on_clause(dict_client: TestClient) -> None:
    """Joins ship the server-rendered ON clause + cardinality + provenance."""
    from schemabrain.datadict.demo_store import SOURCE_ID

    payload = dict_client.get("/api/dict", params={"source_connection_id": SOURCE_ID}).json()
    joins = [j for e in payload["sources"][0]["entities"] for j in e["joins"]]
    assert joins, "the demo store defines canonical joins"
    a_join = joins[0]
    assert set(a_join) >= {
        "name",
        "source_entity",
        "target_entity",
        "on_clause",
        "cardinality",
        "provenance",
    }
    # The ON clause is server-rendered; the client never assembles it.
    assert a_join["on_clause"]


def test_dict_route_409_when_store_has_no_sources(tmp_path: Path) -> None:
    """An empty store (no indexed source) is a 409, not a 500."""
    from fastapi.testclient import TestClient

    from schemabrain.core.store import SQLiteStore
    from schemabrain.dashboard.sidecar import SidecarConfig, create_sidecar

    empty = tmp_path / "empty.db"
    SQLiteStore(empty).close()
    app = create_sidecar(SidecarConfig(store_path=empty))
    with TestClient(app) as c:
        assert c.get("/api/dict").status_code == 409


def test_dict_route_carries_dashboard_schema_header(dict_client: TestClient) -> None:
    from schemabrain.datadict.demo_store import SOURCE_ID

    response = dict_client.get("/api/dict", params={"source_connection_id": SOURCE_ID})
    assert response.headers["X-Schemabrain-Dashboard-Schema"] == "1.10"
