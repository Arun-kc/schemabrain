"""The dashboard sidecar must stamp CSP + hardening headers on every
response — the JSON routes AND the static-export bytes alike.

Pins the security posture the static dashboard ships behind:
  - a Content-Security-Policy with a closed default-src, the high-value
    sinks locked (object-src 'none', frame-ancestors 'none', base-uri
    'self'), and connect-src 'self' so the same-origin SSE stream works
  - X-Content-Type-Options / X-Frame-Options / Referrer-Policy /
    Permissions-Policy
  - NO HSTS: the sidecar serves plain HTTP on 127.0.0.1.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from schemabrain.dashboard.sidecar import (
    CONTENT_SECURITY_POLICY,
    SECURITY_HEADERS,
    is_ui_available,
)

pytestmark = pytest.mark.skipif(
    not is_ui_available(),
    reason="`schemabrain[ui]` extra not installed",
)

if TYPE_CHECKING:
    from fastapi.testclient import TestClient


def test_security_headers_present_on_every_response(client: TestClient) -> None:
    for path in ["/api/health", "/api/meta", "/api/audit/rows", "/api/audit/verify"]:
        response = client.get(path)
        assert response.headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        assert "geolocation=()" in response.headers["Permissions-Policy"]


def test_csp_locks_high_value_sinks(client: TestClient) -> None:
    csp = client.get("/api/meta").headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "base-uri 'self'" in csp


def test_csp_connect_src_permits_same_origin_sse(client: TestClient) -> None:
    # The audit SSE stream (/api/audit/stream) is same-origin; connect-src
    # 'self' must allow it without opening a cross-origin exfil path.
    csp = client.get("/api/meta").headers["Content-Security-Policy"]
    assert "connect-src 'self'" in csp


def test_no_hsts_over_plain_localhost_http(client: TestClient) -> None:
    # HSTS over plain HTTP on 127.0.0.1 would be meaningless/harmful.
    assert "Strict-Transport-Security" not in client.get("/api/meta").headers
    assert "Strict-Transport-Security" not in SECURITY_HEADERS


def test_security_headers_reach_the_static_html_fallback(store_path, monkeypatch, tmp_path) -> None:
    """A static surface (e.g. /pii) must carry the CSP too.

    The html-fallback middleware short-circuits with a FileResponse
    before the inner middleware stack runs, so the security headers must
    be the OUTERMOST middleware to reach it. This test pins that by
    pointing STATIC_DIR at a temp export with a pii.html and asserting
    the CSP rides the served bytes."""
    from fastapi.testclient import TestClient

    from schemabrain.dashboard import sidecar as sidecar_module
    from schemabrain.dashboard.sidecar import SidecarConfig, create_sidecar

    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "pii.html").write_text("<!doctype html><title>pii</title>")
    monkeypatch.setattr(sidecar_module, "STATIC_DIR", static_dir)

    app = create_sidecar(SidecarConfig(store_path=store_path))
    with TestClient(app) as client:
        response = client.get("/pii")
        assert response.status_code == 200
        assert response.headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY
        assert response.headers["X-Frame-Options"] == "DENY"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
