"""Tests for the GET /api/pii/policy/preview sidecar route.

The preview route is a pure, read-only renderer (ADR 0006): it takes a
staged block set + column overrides as repeatable query params and
returns the canonical `policy_to_yaml` rendering of that staged policy
plus the line diff against the current on-disk policy. It reads
`./schemabrain/pii_policy.yaml` for the baseline but writes nothing and
touches no per-source store state.
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

# The catastrophic floor — the default `block:` when no YAML is on disk,
# and the set the dashboard always sends locked-on.
FLOOR = ["credential", "government_id", "payment_card"]


def _write_policy_yaml(tmp_path: Path, body: str) -> None:
    yaml_dir = tmp_path / "schemabrain"
    yaml_dir.mkdir(exist_ok=True)
    (yaml_dir / "pii_policy.yaml").write_text(body, encoding="utf-8")


def test_preview_zero_staged_matches_current_default_floor(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no YAML on disk, current = the floor default; sending the
    floor as the staged block (what the UI does at rest) is a no-op."""
    monkeypatch.chdir(tmp_path)
    resp = client.get("/api/pii/policy/preview", params={"block": FLOOR})
    assert resp.status_code == 200
    body = resp.json()
    assert body["changed"] is False
    assert body["staged_yaml"] == body["current_yaml"]
    assert all(line["kind"] == "context" for line in body["diff_lines"])
    assert body["staged_block"] == FLOOR
    assert body["current_parse_error"] is None


def test_preview_adding_category_shows_add_diff(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    resp = client.get("/api/pii/policy/preview", params={"block": [*FLOOR, "contact"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["changed"] is True
    assert "contact" in body["staged_block"]
    assert "contact" in body["staged_yaml"]
    added = [line["text"] for line in body["diff_lines"] if line["kind"] == "add"]
    assert any("contact" in text for text in added)


def test_preview_mark_safe_override_renders_column_overrides(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 'mark safe' override (sensitivity internal, empty categories)
    round-trips into a canonical column_overrides block."""
    monkeypatch.chdir(tmp_path)
    resp = client.get(
        "/api/pii/policy/preview",
        params={
            "block": FLOOR,
            "override": ["public.payment_methods.card_number_last4|internal|"],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["changed"] is True
    assert "column_overrides:" in body["staged_yaml"]
    assert "public.payment_methods.card_number_last4:" in body["staged_yaml"]
    assert "sensitivity: internal" in body["staged_yaml"]


def test_preview_override_with_categories(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit reclassification carries its comma-joined categories."""
    monkeypatch.chdir(tmp_path)
    resp = client.get(
        "/api/pii/policy/preview",
        params={"block": FLOOR, "override": ["public.users.notes|pii|contact,location"]},
    )
    assert resp.status_code == 200
    staged = resp.json()["staged_yaml"]
    assert "public.users.notes:" in staged
    assert "contact" in staged
    assert "location" in staged


def test_preview_removing_category_shows_remove_diff(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Current YAML carries an extra category; staging without it shows a
    remove line and the current_yaml reflects the on-disk file."""
    monkeypatch.chdir(tmp_path)
    _write_policy_yaml(
        tmp_path,
        "version: 1\nblock:\n  - contact\n  - credential\n  - payment_card\n  - government_id\n",
    )
    resp = client.get("/api/pii/policy/preview", params={"block": FLOOR})
    assert resp.status_code == 200
    body = resp.json()
    assert "contact" in body["current_yaml"]
    assert body["changed"] is True
    removed = [line["text"] for line in body["diff_lines"] if line["kind"] == "remove"]
    assert any("contact" in text for text in removed)


def test_preview_current_description_carried_into_staged(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Staging a block change must not spuriously drop the on-disk
    description — it's carried into the staged policy."""
    monkeypatch.chdir(tmp_path)
    _write_policy_yaml(
        tmp_path,
        'version: 1\ndescription: "PCI Q&A 1.1"\nblock:\n  - credential\n'
        "  - payment_card\n  - government_id\n",
    )
    resp = client.get("/api/pii/policy/preview", params={"block": [*FLOOR, "contact"]})
    body = resp.json()
    assert "PCI Q&A 1.1" in body["staged_yaml"]
    # description line is unchanged → not in the add/remove set
    changed_text = [
        line["text"] for line in body["diff_lines"] if line["kind"] in {"add", "remove"}
    ]
    assert not any("PCI Q&A 1.1" in text for text in changed_text)


def test_preview_invalid_category_returns_400(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    resp = client.get("/api/pii/policy/preview", params={"block": ["not_a_category"]})
    assert resp.status_code == 400


def test_preview_malformed_override_returns_400(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    resp = client.get(
        "/api/pii/policy/preview",
        params={"block": FLOOR, "override": ["public.users.email|internal"]},  # 2 parts
    )
    assert resp.status_code == 400


def test_preview_override_invalid_sensitivity_returns_400(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    resp = client.get(
        "/api/pii/policy/preview",
        params={"block": FLOOR, "override": ["public.users.email|sekret|"]},
    )
    assert resp.status_code == 400


def test_preview_duplicate_override_returns_400(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Policy.__post_init__ rejects two overrides for the same column."""
    monkeypatch.chdir(tmp_path)
    resp = client.get(
        "/api/pii/policy/preview",
        params={
            "block": FLOOR,
            "override": [
                "public.users.email|internal|",
                "public.users.email|confidential|",
            ],
        },
    )
    assert resp.status_code == 400


def test_preview_too_many_overrides_returns_400(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    overrides = [f"public.t.c{i}|internal|" for i in range(1001)]
    resp = client.get("/api/pii/policy/preview", params={"block": FLOOR, "override": overrides})
    assert resp.status_code == 400


def test_preview_surfaces_current_parse_error_and_falls_back(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed on-disk YAML still lets the preview render: current
    falls back to the floor default and the parse error is surfaced."""
    monkeypatch.chdir(tmp_path)
    _write_policy_yaml(tmp_path, "version: 2\nblock: []\n")  # unsupported version
    resp = client.get("/api/pii/policy/preview", params={"block": FLOOR})
    assert resp.status_code == 200
    body = resp.json()
    assert body["current_parse_error"] is not None
    # current fell back to the floor default → equal to staging the floor
    assert body["changed"] is False


def test_preview_empty_block_opt_out_renders(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit empty block (operator opt-out) is a legal policy and
    renders `block: []`."""
    monkeypatch.chdir(tmp_path)
    resp = client.get("/api/pii/policy/preview")  # no block param at all
    assert resp.status_code == 200
    body = resp.json()
    assert body["staged_block"] == []
    assert "block: []" in body["staged_yaml"]
