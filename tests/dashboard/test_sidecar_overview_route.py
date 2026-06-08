"""Tests for the read-only ``GET /api/overview`` route.

The route rolls the honest, store-only signals the per-surface routes
already expose (bound entities by group + confidence band, the per-column
protection verdict reused from ``/api/pii/policy``, drift freshness, 24h
refusals, audit totals, semantic-layer counts) into one read for the
Overview ("home") surface. These tests pin the wire shape, the internal
reconciliation invariants, the honest empty-value behaviour, and source
scoping against the offline SaaS demo store.
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
def overview_store(tmp_path: Path) -> Path:
    """A fully-populated offline SaaS store (entities, columns, joins, metrics,
    PII tags) — but no ``mcp_audit`` rows, which exercises the honest
    empty-audit path."""
    from schemabrain.datadict.demo_store import build_saas_dictionary_store

    path = tmp_path / "overview.db"
    build_saas_dictionary_store(path)
    return path


@pytest.fixture
def overview_client(overview_store: Path) -> Iterator[TestClient]:
    from fastapi.testclient import TestClient

    from schemabrain.dashboard.sidecar import SidecarConfig, create_sidecar

    app = create_sidecar(SidecarConfig(store_path=overview_store))
    with TestClient(app) as client:
        yield client


def _get(client: TestClient) -> dict:
    from schemabrain.datadict.demo_store import SOURCE_ID

    response = client.get("/api/overview", params={"source_connection_id": SOURCE_ID})
    assert response.status_code == 200
    return response.json()


def test_overview_wire_shape_is_stable(overview_client: TestClient) -> None:
    body = _get(overview_client)
    assert set(body) == {
        "source_connection_id",
        "source",
        "entities",
        "protection",
        "freshness",
        "refusals",
        "audit",
        "semantic",
    }
    assert set(body["source"]) == {"label", "tables", "last_indexed_at"}
    assert set(body["entities"]) == {"count", "catastrophic_count", "by_group", "confidence"}
    assert set(body["entities"]["by_group"]) == {"identity", "billing", "activity", "other"}
    assert set(body["entities"]["confidence"]) == {"high", "medium", "low", "unrated"}
    assert set(body["protection"]) == {"catastrophic", "policy_blocked", "allowed", "columns"}
    assert set(body["freshness"]) == {"fresh", "change_count", "risk", "last_enriched"}
    assert set(body["semantic"]) == {"metrics_count", "joins_count", "mined_count", "metric_names"}


def test_protection_tally_reconciles(overview_client: TestClient) -> None:
    """The three protection buckets always sum to the classified column total,
    and the SaaS store has a catastrophic share (the `--alarm` segment)."""
    protection = _get(overview_client)["protection"]
    assert (
        protection["catastrophic"] + protection["policy_blocked"] + protection["allowed"]
        == protection["columns"]
    )
    assert protection["catastrophic"] > 0
    assert protection["columns"] > 0


def test_entities_rollup_reconciles(overview_client: TestClient) -> None:
    """by_group and the confidence distribution each sum to the entity count;
    catastrophic_count never exceeds it."""
    entities = _get(overview_client)["entities"]
    count = entities["count"]
    assert count > 0
    assert sum(entities["by_group"].values()) == count
    assert sum(entities["confidence"].values()) == count
    assert 0 <= entities["catastrophic_count"] <= count


def test_semantic_counts_match_store(overview_client: TestClient, overview_store: Path) -> None:
    from schemabrain.core.store import SQLiteStore
    from schemabrain.datadict.demo_store import SOURCE_ID

    with SQLiteStore(overview_store) as store:
        metrics = store.list_metrics(source_connection_id=SOURCE_ID)
        joins = store.list_canonical_joins(source_connection_id=SOURCE_ID)

    semantic = _get(overview_client)["semantic"]
    assert semantic["metrics_count"] == len(metrics)
    assert semantic["joins_count"] == len(joins)
    assert semantic["mined_count"] >= 0
    assert semantic["metric_names"] == [m.name for m in metrics]


def test_audit_is_honest_on_an_empty_log(overview_client: TestClient) -> None:
    """A store with no `mcp_audit` rows reports 0 / [] — never a fabricated
    total like the prototype's hardcoded 1842."""
    body = _get(overview_client)
    assert body["audit"]["total"] == 0
    assert body["audit"]["recent"] == []
    assert body["refusals"]["count_24h"] == 0
    assert body["refusals"]["recent"] == []


def test_freshness_reflects_real_drift(overview_client: TestClient) -> None:
    """fresh is true iff there are zero drift items; the SaaS demo store is
    fresh (no config/enrichment drift), and never-enriched → last_enriched null."""
    freshness = _get(overview_client)["freshness"]
    assert freshness["fresh"] is (freshness["change_count"] == 0)
    assert freshness["last_enriched"] is None


def test_no_resolvable_source_is_409(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from schemabrain.core.store import SQLiteStore
    from schemabrain.dashboard.sidecar import SidecarConfig, create_sidecar

    # An existing store with the schema but no indexed sources.
    empty = tmp_path / "empty.db"
    with SQLiteStore(empty):
        pass
    app = create_sidecar(SidecarConfig(store_path=empty))
    with TestClient(app) as client:
        assert client.get("/api/overview").status_code == 409


def test_route_resolves_default_source_without_param(overview_client: TestClient) -> None:
    """No source_connection_id → the route resolves the single default source."""
    response = overview_client.get("/api/overview")
    assert response.status_code == 200
    assert response.json()["entities"]["count"] > 0
