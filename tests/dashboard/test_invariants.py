"""Architectural invariants asserted against the live sidecar.

These run against the constructed FastAPI app — not source-code grep
— so the assertions catch drift even when a contributor adds a route
via an unexpected decorator pattern.
"""

from __future__ import annotations

import pytest

from schemabrain.dashboard.sidecar import (
    BIND_HOST,
    assert_route_table_is_read_only,
    is_ui_available,
)

pytestmark = pytest.mark.skipif(
    not is_ui_available(),
    reason="`schemabrain[ui]` extra not installed",
)


def test_bind_host_is_localhost_only() -> None:
    assert BIND_HOST == "127.0.0.1"


def test_no_write_routes_declared(app) -> None:
    assert_route_table_is_read_only(app)


def test_all_api_routes_use_get_only(app) -> None:
    """Belt-and-suspenders check: every /api/* route declares GET (+ optional HEAD)."""
    allowed = {"GET", "HEAD", "OPTIONS"}
    for route in app.routes:
        path = getattr(route, "path", "") or ""
        if not path.startswith("/api/"):
            continue
        methods = getattr(route, "methods", None) or set()
        unexpected = methods - allowed
        assert not unexpected, f"route {path} declares non-read methods {sorted(unexpected)}"


def test_openapi_docs_are_disabled(client) -> None:
    """The sidecar serves the dashboard UI, not API documentation pages."""
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_html_fallback_serves_extensionless_path_when_html_sibling_exists(
    store_path, tmp_path, monkeypatch
):
    """`/pii` should serve `pii.html` when the file exists on disk.

    Bridges Next.js's static-export filename convention (`pii.html`)
    with the React app's URL convention (`/pii`). Only fires for
    non-API GETs and only when the `.html` sibling exists on disk.
    """
    from fastapi.testclient import TestClient

    from schemabrain.dashboard import sidecar as sidecar_mod
    from schemabrain.dashboard.sidecar import SidecarConfig, create_sidecar

    # Seed a temporary static dir with a pii.html file so the mount
    # registers and the html-fallback middleware activates.
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html><body>landing</body></html>")
    (static_dir / "pii.html").write_text("<html><body>pii page</body></html>")
    monkeypatch.setattr(sidecar_mod, "STATIC_DIR", static_dir)

    app = create_sidecar(SidecarConfig(store_path=store_path))
    with TestClient(app) as client:
        # Extensionless path with a .html sibling — middleware rewrites
        response = client.get("/pii")
        assert response.status_code == 200
        assert "pii page" in response.text

        # Real 404 still 404s (no .html sibling)
        assert client.get("/no-such-page").status_code == 404

        # API routes are unaffected
        assert client.get("/api/health").status_code == 200
