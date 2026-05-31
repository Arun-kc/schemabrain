"""Tests for the GET /api/pii/policy sidecar route."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore
from schemabrain.dashboard.sidecar import is_ui_available

pytestmark = pytest.mark.skipif(
    not is_ui_available(),
    reason="`schemabrain[ui]` extra not installed",
)

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

SRC = "policy-test-source"


def _seed_user_table_with_tags(store_path: Path) -> None:
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
        store.write_column_pii_tags(
            source_connection_id=SRC,
            qualified_table="public.users",
            tags={"email": ("pii", frozenset({"contact"}))},
        )
    finally:
        store.close()


def test_policy_route_returns_catastrophic_default_when_no_yaml(
    client: TestClient, store_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _seed_user_table_with_tags(store_path)
    resp = client.get("/api/pii/policy", params={"source_connection_id": SRC})
    assert resp.status_code == 200
    body = resp.json()
    assert body["source_connection_id"] == SRC
    assert body["block_source"] == "default"
    assert set(body["active_block"]) == {"credential", "payment_card", "government_id"}
    assert set(body["catastrophic_floor"]) == {"credential", "payment_card", "government_id"}
    assert body["yaml_parse_error"] is None


def test_policy_route_reads_block_from_yaml_when_present(
    client: TestClient, store_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _seed_user_table_with_tags(store_path)
    yaml_dir = tmp_path / "schemabrain"
    yaml_dir.mkdir()
    (yaml_dir / "pii_policy.yaml").write_text(
        "version: 1\nblock:\n  - credential\n  - contact\n",
        encoding="utf-8",
    )
    resp = client.get("/api/pii/policy", params={"source_connection_id": SRC})
    assert resp.status_code == 200
    body = resp.json()
    assert body["block_source"] == "yaml"
    assert set(body["active_block"]) == {"credential", "contact"}


def test_policy_route_per_column_verdict(
    client: TestClient, store_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-column verdict surfaces with effective_enforcement = blocked
    when the column's categories intersect the active block."""
    monkeypatch.chdir(tmp_path)
    _seed_user_table_with_tags(store_path)
    yaml_dir = tmp_path / "schemabrain"
    yaml_dir.mkdir()
    (yaml_dir / "pii_policy.yaml").write_text(
        "version: 1\nblock:\n  - contact\n",
        encoding="utf-8",
    )
    resp = client.get("/api/pii/policy", params={"source_connection_id": SRC})
    body = resp.json()
    email_entry = next(e for e in body["per_column"] if e["column_name"] == "email")
    assert email_entry["effective_enforcement"] == "blocked"
    assert email_entry["origin"] == "heuristic"
    assert email_entry["categories"] == ["contact"]


def test_policy_route_describe_blocked_when_only_floor_intersects(
    client: TestClient, store_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Column tagged `credential` (in catastrophic floor) but active
    block is just `contact`: get_metric path allows, but describe_*
    blocks via the floor union."""
    monkeypatch.chdir(tmp_path)
    store = SQLiteStore(store_path)
    try:
        store.write_table(
            Table(
                name="sessions",
                schema_name="public",
                columns=(
                    Column(
                        name="id",
                        table_name="sessions",
                        schema_name="public",
                        data_type="BIGINT",
                        nullable=False,
                        ordinal_position=1,
                        is_primary_key=True,
                    ),
                    Column(
                        name="token",
                        table_name="sessions",
                        schema_name="public",
                        data_type="TEXT",
                        nullable=False,
                        ordinal_position=2,
                    ),
                ),
            ),
            source_connection_id=SRC,
        )
        store.write_column_pii_tags(
            source_connection_id=SRC,
            qualified_table="public.sessions",
            tags={"token": ("pii", frozenset({"credential"}))},
        )
    finally:
        store.close()
    yaml_dir = tmp_path / "schemabrain"
    yaml_dir.mkdir()
    (yaml_dir / "pii_policy.yaml").write_text(
        "version: 1\nblock:\n  - contact\n",
        encoding="utf-8",
    )
    resp = client.get("/api/pii/policy", params={"source_connection_id": SRC})
    body = resp.json()
    token_entry = next(e for e in body["per_column"] if e["column_name"] == "token")
    # `credential` IS in the catastrophic floor but NOT in the
    # operator-configured `{contact}` block — so describe_* would
    # refuse via the floor union, but get_metric would let it through.
    # The verdict surfaces the "describe-only" intermediate state.
    assert token_entry["effective_enforcement"] == "describe_blocked"


def test_policy_route_floor_holds_when_yaml_block_is_empty(
    client: TestClient, store_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin the most direct circumvention attempt: operator writes a
    YAML with `block: []` to suppress an over-eager block, expecting
    enforcement to be off. The catastrophic floor MUST still gate the
    describe-side verdict for any column tagged with credential /
    payment_card / government_id — otherwise an empty block would
    silently re-expose every secret column the heuristic classifier
    already tagged.

    Without this pin, a future refactor that introduces an
    `if not active_block: effective_block = active_block`
    short-circuit at sidecar.py:641 would regress to verdict='allowed'
    for credential columns and the rest of the suite would still pass.
    """
    monkeypatch.chdir(tmp_path)
    store = SQLiteStore(store_path)
    try:
        store.write_table(
            Table(
                name="sessions",
                schema_name="public",
                columns=(
                    Column(
                        name="id",
                        table_name="sessions",
                        schema_name="public",
                        data_type="BIGINT",
                        nullable=False,
                        ordinal_position=1,
                        is_primary_key=True,
                    ),
                    Column(
                        name="token",
                        table_name="sessions",
                        schema_name="public",
                        data_type="TEXT",
                        nullable=False,
                        ordinal_position=2,
                    ),
                ),
            ),
            source_connection_id=SRC,
        )
        store.write_column_pii_tags(
            source_connection_id=SRC,
            qualified_table="public.sessions",
            tags={"token": ("pii", frozenset({"credential"}))},
        )
    finally:
        store.close()
    yaml_dir = tmp_path / "schemabrain"
    yaml_dir.mkdir()
    (yaml_dir / "pii_policy.yaml").write_text(
        "version: 1\nblock: []\n",
        encoding="utf-8",
    )

    resp = client.get("/api/pii/policy", params={"source_connection_id": SRC})
    assert resp.status_code == 200
    body = resp.json()

    # YAML was loaded successfully (parse error path NOT taken).
    assert body["block_source"] == "yaml"
    assert body["yaml_parse_error"] is None
    # Active block is empty — operator's intent honoured at the
    # metric-aggregate layer.
    assert body["active_block"] == []
    # But the describe-side effective block STILL surfaces the floor.
    assert set(body["effective_block_for_describe"]) == {
        "credential",
        "payment_card",
        "government_id",
    }
    # And the per-column verdict for the credential token row is
    # describe_blocked — NOT allowed. This is the load-bearing assertion.
    token_entry = next(e for e in body["per_column"] if e["column_name"] == "token")
    assert token_entry["effective_enforcement"] == "describe_blocked"
    # Belt-and-suspenders: no per_column row whose categories intersect
    # the floor may ever report 'allowed' regardless of YAML state.
    floor = {"credential", "payment_card", "government_id"}
    for entry in body["per_column"]:
        if set(entry["categories"]) & floor:
            assert entry["effective_enforcement"] != "allowed", (
                f"floor regression: {entry['qualified_column']} "
                f"categories={entry['categories']} reported as 'allowed'"
            )


def test_policy_route_diff_preview_floor_holds_when_yaml_block_is_empty(
    client: TestClient, store_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Companion pin for the diff_preview branch under the same
    empty-block scenario. `current_blocked` reflects the metric-block
    posture (empty → 0), but `if_catastrophic_only` must still count
    every column tagged with a floor category."""
    monkeypatch.chdir(tmp_path)
    store = SQLiteStore(store_path)
    try:
        store.write_table(
            Table(
                name="sessions",
                schema_name="public",
                columns=(
                    Column(
                        name="id",
                        table_name="sessions",
                        schema_name="public",
                        data_type="BIGINT",
                        nullable=False,
                        ordinal_position=1,
                        is_primary_key=True,
                    ),
                    Column(
                        name="token",
                        table_name="sessions",
                        schema_name="public",
                        data_type="TEXT",
                        nullable=False,
                        ordinal_position=2,
                    ),
                ),
            ),
            source_connection_id=SRC,
        )
        store.write_column_pii_tags(
            source_connection_id=SRC,
            qualified_table="public.sessions",
            tags={"token": ("pii", frozenset({"credential"}))},
        )
    finally:
        store.close()
    yaml_dir = tmp_path / "schemabrain"
    yaml_dir.mkdir()
    (yaml_dir / "pii_policy.yaml").write_text(
        "version: 1\nblock: []\n",
        encoding="utf-8",
    )

    resp = client.get("/api/pii/policy", params={"source_connection_id": SRC})
    body = resp.json()
    diff = body["diff_preview"]
    # Operator's metric-block posture is empty.
    assert diff["current_blocked"] == 0
    # But the catastrophic-only preview still shows the token row.
    assert diff["if_catastrophic_only"] == 1
    # Effective describe-side block is still the floor.
    assert set(body["effective_block_for_describe"]) == {
        "credential",
        "payment_card",
        "government_id",
    }


def test_policy_route_category_rollup_counts(
    client: TestClient, store_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _seed_user_table_with_tags(store_path)
    resp = client.get("/api/pii/policy", params={"source_connection_id": SRC})
    body = resp.json()
    contact_entry = next(e for e in body["category_rollup"] if e["category"] == "contact")
    assert contact_entry["column_count"] == 1
    assert contact_entry["entity_count"] == 1
    assert contact_entry["sample_columns"] == ["public.users.email"]


def test_policy_route_diff_preview_present(
    client: TestClient, store_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _seed_user_table_with_tags(store_path)
    resp = client.get("/api/pii/policy", params={"source_connection_id": SRC})
    body = resp.json()
    diff = body["diff_preview"]
    assert "current_blocked" in diff
    assert "if_all_blocked" in diff
    assert "if_catastrophic_only" in diff
    assert "if_none_blocked" in diff
    assert diff["if_none_blocked"] == 0


def test_policy_route_surfaces_yaml_parse_error(
    client: TestClient, store_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _seed_user_table_with_tags(store_path)
    yaml_dir = tmp_path / "schemabrain"
    yaml_dir.mkdir()
    # Missing required `block:` key — should parse error.
    (yaml_dir / "pii_policy.yaml").write_text(
        "version: 1\ndescription: malformed\n",
        encoding="utf-8",
    )
    resp = client.get("/api/pii/policy", params={"source_connection_id": SRC})
    body = resp.json()
    # Falls back to default on parse error, surfaces the error.
    assert body["block_source"] == "default"
    assert body["yaml_parse_error"] is not None
    assert "block" in body["yaml_parse_error"]


# ---------- policy_drift signal ----------


SENTINEL_REL = "schemabrain/.serve_policy_mtime"
YAML_REL = "schemabrain/pii_policy.yaml"


def _write_sentinel(tmp_path: Path, payload: dict[str, object]) -> Path:
    """Write the sentinel JSON serve would have written at startup."""
    sentinel = tmp_path / SENTINEL_REL
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return sentinel


def _write_yaml(tmp_path: Path, body: str = "version: 1\nblock: []\n") -> Path:
    yaml_path = tmp_path / YAML_REL
    yaml_path.parent.mkdir(parents=True, exist_ok=True)
    yaml_path.write_text(body, encoding="utf-8")
    return yaml_path


def test_policy_drift_field_present_in_response_shape(
    client: TestClient, store_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The response shape MUST always carry the `policy_drift` field
    so the TypeScript PolicyResponse interface is honoured."""
    monkeypatch.chdir(tmp_path)
    _seed_user_table_with_tags(store_path)
    resp = client.get("/api/pii/policy", params={"source_connection_id": SRC})
    body = resp.json()
    assert "policy_drift" in body
    drift = body["policy_drift"]
    assert set(drift.keys()) == {"detected", "recorded_at", "current_mtime"}


def test_policy_drift_undetected_when_no_sentinel(
    client: TestClient, store_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No sentinel + no YAML — drift is False and both timestamps are
    None (this is the steady state when neither serve nor a YAML file
    is around)."""
    monkeypatch.chdir(tmp_path)
    _seed_user_table_with_tags(store_path)
    resp = client.get("/api/pii/policy", params={"source_connection_id": SRC})
    drift = resp.json()["policy_drift"]
    assert drift["detected"] is False
    assert drift["recorded_at"] is None
    assert drift["current_mtime"] is None


def test_policy_drift_undetected_when_sentinel_matches_yaml_mtime(
    client: TestClient, store_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sentinel + YAML present + recorded mtime ≈ current mtime →
    no drift; both timestamps populated."""
    monkeypatch.chdir(tmp_path)
    _seed_user_table_with_tags(store_path)
    yaml_path = _write_yaml(tmp_path)
    mtime = yaml_path.stat().st_mtime
    _write_sentinel(
        tmp_path,
        {
            "policy_path": str(yaml_path.resolve()),
            "recorded_at_mtime": mtime,
            "recorded_at_iso": "2026-05-31T12:00:00+00:00",
            "yaml_existed_at_boot": True,
        },
    )
    resp = client.get("/api/pii/policy", params={"source_connection_id": SRC})
    drift = resp.json()["policy_drift"]
    assert drift["detected"] is False
    assert drift["recorded_at"] == "2026-05-31T12:00:00+00:00"
    assert drift["current_mtime"] is not None


def test_policy_drift_detected_when_yaml_edited_after_serve(
    client: TestClient, store_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sentinel records an mtime; YAML is then edited so its mtime
    bumps above the recorded value → drift fires.

    Uses `os.utime` to set a future mtime explicitly rather than
    `time.sleep` — keeps the test fast and out of the `slow` marker.
    """
    import os

    monkeypatch.chdir(tmp_path)
    _seed_user_table_with_tags(store_path)
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
    # Rewrite the YAML and bump its mtime past the 1ms epsilon
    # explicitly — no sleep needed.
    yaml_path.write_text(
        "version: 1\nblock:\n  - contact\n",
        encoding="utf-8",
    )
    future_mtime = original_mtime + 10.0
    os.utime(yaml_path, (future_mtime, future_mtime))

    resp = client.get("/api/pii/policy", params={"source_connection_id": SRC})
    drift = resp.json()["policy_drift"]
    assert drift["detected"] is True
    assert drift["recorded_at"] == "2026-05-31T12:00:00+00:00"
    assert drift["current_mtime"] is not None
    # current_mtime must be DIFFERENT from recorded (post-edit).


def test_policy_drift_detected_when_yaml_deleted_after_serve(
    client: TestClient, store_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sentinel says yaml existed at boot; YAML is now gone → drift."""
    monkeypatch.chdir(tmp_path)
    _seed_user_table_with_tags(store_path)
    # Sentinel says YAML was there at boot, but no YAML file exists now.
    fake_yaml_path = (tmp_path / YAML_REL).resolve()
    _write_sentinel(
        tmp_path,
        {
            "policy_path": str(fake_yaml_path),
            "recorded_at_mtime": 1_717_000_000.0,
            "recorded_at_iso": "2026-05-31T12:00:00+00:00",
            "yaml_existed_at_boot": True,
        },
    )
    resp = client.get("/api/pii/policy", params={"source_connection_id": SRC})
    drift = resp.json()["policy_drift"]
    assert drift["detected"] is True
    assert drift["recorded_at"] == "2026-05-31T12:00:00+00:00"
    assert drift["current_mtime"] is None


def test_policy_drift_detected_when_yaml_appears_after_yaml_less_serve(
    client: TestClient, store_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sentinel says yaml DID NOT exist at boot; YAML appears later → drift."""
    monkeypatch.chdir(tmp_path)
    _seed_user_table_with_tags(store_path)
    yaml_path = _write_yaml(tmp_path)
    _write_sentinel(
        tmp_path,
        {
            "policy_path": str(yaml_path.resolve()),
            "recorded_at_mtime": None,
            "recorded_at_iso": "2026-05-31T12:00:00+00:00",
            "yaml_existed_at_boot": False,
        },
    )
    resp = client.get("/api/pii/policy", params={"source_connection_id": SRC})
    drift = resp.json()["policy_drift"]
    assert drift["detected"] is True
    assert drift["recorded_at"] == "2026-05-31T12:00:00+00:00"
    assert drift["current_mtime"] is not None


def test_policy_drift_undetected_when_sentinel_path_differs(
    client: TestClient, store_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sentinel was written by a serve watching a different YAML path
    — sidecar can't reason about that drift, so it reports False."""
    monkeypatch.chdir(tmp_path)
    _seed_user_table_with_tags(store_path)
    yaml_path = _write_yaml(tmp_path)
    _write_sentinel(
        tmp_path,
        {
            "policy_path": "/some/other/elsewhere/pii_policy.yaml",
            "recorded_at_mtime": yaml_path.stat().st_mtime + 100.0,
            "recorded_at_iso": "2026-05-31T12:00:00+00:00",
            "yaml_existed_at_boot": True,
        },
    )
    resp = client.get("/api/pii/policy", params={"source_connection_id": SRC})
    drift = resp.json()["policy_drift"]
    # Sentinel path doesn't match sidecar's hardcoded path — sidecar
    # has no way to verify, so reports False but keeps recorded_at
    # populated so the operator can see a sentinel exists.
    assert drift["detected"] is False
    assert drift["recorded_at"] == "2026-05-31T12:00:00+00:00"


def test_policy_drift_undetected_when_recorded_mtime_is_non_numeric(
    client: TestClient, store_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sentinel with structurally-valid JSON but a non-numeric
    `recorded_at_mtime` (string / list / object) MUST NOT crash the
    /api/pii/policy route. Without the float() coercion guard, this
    case returns HTTP 500 — the bug a hand-edited or corrupt sentinel
    can trigger."""
    monkeypatch.chdir(tmp_path)
    _seed_user_table_with_tags(store_path)
    yaml_path = _write_yaml(tmp_path)
    _write_sentinel(
        tmp_path,
        {
            "policy_path": str(yaml_path.resolve()),
            # Hostile value: a string where a float is expected.
            "recorded_at_mtime": "not-a-number",
            "recorded_at_iso": "2026-05-31T12:00:00+00:00",
            "yaml_existed_at_boot": True,
        },
    )
    resp = client.get("/api/pii/policy", params={"source_connection_id": SRC})
    # 200 status proves the route stayed available.
    assert resp.status_code == 200
    drift = resp.json()["policy_drift"]
    # Fail-closed posture: detected=False on type error.
    assert drift["detected"] is False
    # Other fields still populated so the operator can see a sentinel exists.
    assert drift["recorded_at"] == "2026-05-31T12:00:00+00:00"
    assert drift["current_mtime"] is not None


def test_policy_drift_undetected_when_sentinel_has_inconsistent_yaml_existed(
    client: TestClient, store_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sentinel claiming yaml_existed_at_boot=True with recorded_at_mtime=None
    is internally inconsistent — the writer cannot produce that state.
    Treat as fail-closed (detected=False) rather than fire a false-positive
    drift banner."""
    monkeypatch.chdir(tmp_path)
    _seed_user_table_with_tags(store_path)
    yaml_path = _write_yaml(tmp_path)
    _write_sentinel(
        tmp_path,
        {
            "policy_path": str(yaml_path.resolve()),
            "recorded_at_mtime": None,
            "recorded_at_iso": "2026-05-31T12:00:00+00:00",
            # Inconsistent: claims yaml existed AND mtime is null.
            "yaml_existed_at_boot": True,
        },
    )
    resp = client.get("/api/pii/policy", params={"source_connection_id": SRC})
    drift = resp.json()["policy_drift"]
    assert drift["detected"] is False
    assert drift["recorded_at"] == "2026-05-31T12:00:00+00:00"


def test_policy_drift_undetected_when_sentinel_recorded_at_iso_is_non_string(
    client: TestClient, store_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """recorded_at_iso of an unexpected type (e.g. int) must be
    coerced to None on the wire so the TypeScript PolicyDriftState
    interface (`recorded_at: string | null`) holds."""
    monkeypatch.chdir(tmp_path)
    _seed_user_table_with_tags(store_path)
    yaml_path = _write_yaml(tmp_path)
    _write_sentinel(
        tmp_path,
        {
            "policy_path": str(yaml_path.resolve()),
            "recorded_at_mtime": yaml_path.stat().st_mtime,
            # Hostile value: an integer where a string is expected.
            "recorded_at_iso": 12345,
            "yaml_existed_at_boot": True,
        },
    )
    resp = client.get("/api/pii/policy", params={"source_connection_id": SRC})
    assert resp.status_code == 200
    drift = resp.json()["policy_drift"]
    # The non-string is sanitized to None.
    assert drift["recorded_at"] is None


def test_policy_drift_undetected_when_sentinel_malformed_json(
    client: TestClient, store_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sentinel with broken JSON is treated as no-sentinel —
    fail-closed posture: better silent than misleading."""
    monkeypatch.chdir(tmp_path)
    _seed_user_table_with_tags(store_path)
    yaml_path = _write_yaml(tmp_path)
    sentinel = tmp_path / SENTINEL_REL
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("{not valid json", encoding="utf-8")

    resp = client.get("/api/pii/policy", params={"source_connection_id": SRC})
    drift = resp.json()["policy_drift"]
    assert drift["detected"] is False
    assert drift["recorded_at"] is None
    # current_mtime is still surfaced — operator can see YAML is here.
    assert drift["current_mtime"] is not None
    # silence unused-warning
    _ = yaml_path


def test_policy_route_operator_override_origin_visible(
    client: TestClient, store_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator override surfaces with origin='operator' in
    the per-column verdict."""
    monkeypatch.chdir(tmp_path)
    _seed_user_table_with_tags(store_path)
    store = SQLiteStore(store_path)
    try:
        store.upsert_column_pii_tag_override(
            source_connection_id=SRC,
            qualified_table="public.users",
            column_name="email",
            sensitivity="internal",
            categories=frozenset(),
        )
    finally:
        store.close()
    resp = client.get("/api/pii/policy", params={"source_connection_id": SRC})
    body = resp.json()
    email_entry = next(e for e in body["per_column"] if e["column_name"] == "email")
    assert email_entry["origin"] == "operator"
    assert email_entry["sensitivity"] == "internal"
    assert email_entry["categories"] == []
