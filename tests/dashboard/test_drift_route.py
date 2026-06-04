"""Tests for the GET /api/drift sidecar route.

The Drift surface is read-only and store-only. It surfaces exactly two
honestly-verifiable drift kinds — ``config`` (policy YAML vs serve
sentinel, reusing ``_compute_policy_drift``) and ``enrichment``
(prompt-version staleness) — plus a freshness roll-up. ``fresh`` is true
iff there are zero items. Schema/definition drift is deliberately absent
(it would require a live DB the sidecar does not have).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from schemabrain.core.description import ColumnDescription
from schemabrain.core.models import Column, Table
from schemabrain.core.source_id import make_source_id
from schemabrain.core.store import SQLiteStore
from schemabrain.dashboard.sidecar import DEFAULT_POLICY_YAML_PATH, is_ui_available
from schemabrain.enrichment.prompts import PROMPT_VERSION

pytestmark = pytest.mark.skipif(
    not is_ui_available(),
    reason="`schemabrain[ui]` extra not installed",
)

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

SRC = "drift-test-source"
SENTINEL_REL = "schemabrain/.serve_policy_mtime"
YAML_REL = "schemabrain/pii_policy.yaml"

_FRESHNESS_KEYS = {
    "fresh",
    "change_count",
    "high_risk",
    "last_enriched",
    "last_indexed",
    "source_label",
    "prompt_version",
}


def _seed_user_table(store_path: Path) -> None:
    store = SQLiteStore(store_path)
    try:
        store.write_table(
            Table(
                name="users",
                schema_name="public",
                columns=(
                    Column(
                        name="id",
                        table_name="users",
                        schema_name="public",
                        data_type="BIGINT",
                        nullable=False,
                        ordinal_position=1,
                        is_primary_key=True,
                    ),
                    Column(
                        name="email",
                        table_name="users",
                        schema_name="public",
                        data_type="TEXT",
                        nullable=False,
                        ordinal_position=2,
                    ),
                ),
            ),
            source_connection_id=SRC,
        )
    finally:
        store.close()


def _seed_description(store_path: Path, *, prompt_version: str) -> None:
    """Seed one column description with the given prompt version (drives
    the enrichment-drift signal + populates last_enriched)."""
    store = SQLiteStore(store_path)
    try:
        store.write_table_descriptions(
            "public",
            "users",
            source_connection_id=SRC,
            descriptions={
                "email": ColumnDescription(
                    text="the user's email address",
                    model="claude-haiku-4-5",
                    prompt_version=prompt_version,
                    input_tokens=12,
                    cached_input_tokens=0,
                    output_tokens=6,
                    cost_usd=0.0001,
                )
            },
        )
    finally:
        store.close()


def _write_sentinel(tmp_path: Path, payload: dict[str, object]) -> Path:
    sentinel = tmp_path / SENTINEL_REL
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return sentinel


def _write_yaml(tmp_path: Path, body: str = "version: 1\nblock: []\n") -> Path:
    yaml_path = tmp_path / YAML_REL
    yaml_path.parent.mkdir(parents=True, exist_ok=True)
    yaml_path.write_text(body, encoding="utf-8")
    return yaml_path


def _drifted_policy_sentinel(tmp_path: Path) -> None:
    """Write a YAML + sentinel pair whose mtimes diverge → config drift."""
    yaml_path = _write_yaml(tmp_path)
    original_mtime = yaml_path.stat().st_mtime
    _write_sentinel(
        tmp_path,
        {
            "policy_path": str(yaml_path.resolve()),
            "recorded_at_mtime": original_mtime,
            "recorded_at_iso": "2026-05-31T12:00:00+00:00",
            "yaml_existed_at_boot": True,
        },
    )
    future_mtime = original_mtime + 10.0
    os.utime(yaml_path, (future_mtime, future_mtime))


# ---------- shape + fresh default ----------


def test_drift_response_shape_is_stable(
    client: TestClient, store_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _seed_user_table(store_path)
    resp = client.get("/api/drift", params={"source_connection_id": SRC})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"source_connection_id", "freshness", "items"}
    assert body["source_connection_id"] == SRC
    assert set(body["freshness"].keys()) == _FRESHNESS_KEYS
    assert isinstance(body["items"], list)


def test_drift_fresh_default_when_no_drift(
    client: TestClient, store_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source with descriptions on the live prompt + no policy sentinel
    is fully fresh: zero items, fresh=True, last_enriched populated."""
    monkeypatch.chdir(tmp_path)
    _seed_user_table(store_path)
    _seed_description(store_path, prompt_version=PROMPT_VERSION)
    resp = client.get("/api/drift", params={"source_connection_id": SRC})
    body = resp.json()
    assert body["items"] == []
    fr = body["freshness"]
    assert fr["fresh"] is True
    assert fr["change_count"] == 0
    assert fr["high_risk"] == 0
    assert fr["last_enriched"] is not None
    assert fr["last_indexed"] is not None
    assert fr["source_label"] == SRC
    assert fr["prompt_version"] == PROMPT_VERSION


def test_drift_last_enriched_none_when_never_enriched(
    client: TestClient, store_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Indexed but not enriched (no description rows) → last_enriched None,
    never a fabricated 0."""
    monkeypatch.chdir(tmp_path)
    _seed_user_table(store_path)
    resp = client.get("/api/drift", params={"source_connection_id": SRC})
    assert resp.json()["freshness"]["last_enriched"] is None


# ---------- config drift ----------


def test_drift_config_item_appears_when_policy_edited(
    client: TestClient, store_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _seed_user_table(store_path)
    _seed_description(store_path, prompt_version=PROMPT_VERSION)
    _drifted_policy_sentinel(tmp_path)
    resp = client.get("/api/drift", params={"source_connection_id": SRC})
    body = resp.json()
    assert body["freshness"]["fresh"] is False
    config_items = [i for i in body["items"] if i["kind"] == "config"]
    assert len(config_items) == 1
    item = config_items[0]
    assert item["risk"] == "med"
    assert item["when"] is not None
    assert item["action"]["command"] == "schemabrain serve"


def test_drift_no_config_item_when_no_sentinel(
    client: TestClient, store_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _seed_user_table(store_path)
    resp = client.get("/api/drift", params={"source_connection_id": SRC})
    assert [i for i in resp.json()["items"] if i["kind"] == "config"] == []


# ---------- enrichment drift ----------


def test_drift_enrichment_item_appears_when_prompt_stale(
    client: TestClient, store_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _seed_user_table(store_path)
    _seed_description(store_path, prompt_version="2026-01-01-1")
    resp = client.get("/api/drift", params={"source_connection_id": SRC})
    body = resp.json()
    assert body["freshness"]["fresh"] is False
    enrichment_items = [i for i in body["items"] if i["kind"] == "enrichment"]
    assert len(enrichment_items) == 1
    item = enrichment_items[0]
    assert item["risk"] == "med"
    assert item["when"] is None
    assert "--enrich" in item["action"]["command"]
    assert PROMPT_VERSION in item["detail"]


def test_drift_no_enrichment_item_when_prompt_current(
    client: TestClient, store_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _seed_user_table(store_path)
    _seed_description(store_path, prompt_version=PROMPT_VERSION)
    resp = client.get("/api/drift", params={"source_connection_id": SRC})
    assert [i for i in resp.json()["items"] if i["kind"] == "enrichment"] == []


def test_drift_both_kinds_together(
    client: TestClient, store_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _seed_user_table(store_path)
    _seed_description(store_path, prompt_version="2026-01-01-1")
    _drifted_policy_sentinel(tmp_path)
    body = client.get("/api/drift", params={"source_connection_id": SRC}).json()
    assert body["freshness"]["change_count"] == 2
    assert {i["kind"] for i in body["items"]} == {"config", "enrichment"}


# ---------- edges: no sources, unknown source, fail-closed ----------


def test_drift_409_when_store_has_no_sources(
    client: TestClient, store_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    resp = client.get("/api/drift")
    assert resp.status_code == 409


def test_drift_last_indexed_none_for_unknown_source(
    client: TestClient, store_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit source override that isn't in the store resolves but
    carries no freshness timestamps (covers the .get miss branch)."""
    monkeypatch.chdir(tmp_path)
    _seed_user_table(store_path)
    resp = client.get("/api/drift", params={"source_connection_id": "ghost-source"})
    fr = resp.json()["freshness"]
    assert fr["source_label"] == "ghost-source"
    assert fr["last_indexed"] is None
    assert fr["last_enriched"] is None


def test_drift_fail_closed_on_malformed_sentinel(
    client: TestClient, store_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupt sentinel must not 500 the route or fabricate config
    drift — it stays HTTP 200 with no config item (fail-closed)."""
    monkeypatch.chdir(tmp_path)
    _seed_user_table(store_path)
    _write_yaml(tmp_path)
    sentinel = tmp_path / SENTINEL_REL
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("{not valid json", encoding="utf-8")
    resp = client.get("/api/drift", params={"source_connection_id": SRC})
    assert resp.status_code == 200
    assert [i for i in resp.json()["items"] if i["kind"] == "config"] == []


# ---------- credential safety ----------


def test_drift_never_echoes_raw_connection_url(
    client: TestClient, store_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A URL-shaped source_connection_id override (which carries the DB
    password) must be hashed to its canonical id before it reaches the
    response — the password must never appear in the body, and the echoed
    ids must be the credential-safe hash, not the raw URL."""
    monkeypatch.chdir(tmp_path)
    _seed_user_table(store_path)
    secret_url = "postgresql://admin:SuperSecret@db.internal:5432/prod"
    resp = client.get("/api/drift", params={"source_connection_id": secret_url})
    assert resp.status_code == 200
    assert "SuperSecret" not in resp.text
    assert secret_url not in resp.text
    body = resp.json()
    expected_id = make_source_id(secret_url)
    assert body["source_connection_id"] == expected_id
    assert body["freshness"]["source_label"] == expected_id


# ---------- config-drift path parity (writer cli ↔ reader sidecar) ----------


def test_default_policy_path_matches_cli_constant() -> None:
    """The drift/policy reader's DEFAULT_POLICY_YAML_PATH must point at the
    same file the CLI writer records its mtime against; otherwise the
    sentinel path-match guard silently disables config-drift detection.
    Pinned here so a typo in either literal fails loudly (mirrors the
    shared-sentinel-path discipline)."""
    from schemabrain.cli import _DEFAULT_POLICY_PATH

    assert Path(_DEFAULT_POLICY_PATH) == DEFAULT_POLICY_YAML_PATH
