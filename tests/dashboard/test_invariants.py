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
