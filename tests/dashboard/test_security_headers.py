"""The dashboard sidecar must stamp CSP + hardening headers on every
response — the JSON routes AND the static-export bytes alike.

Pins the security posture the static dashboard ships behind:
  - a Content-Security-Policy with a closed default-src, the high-value
    sinks locked (object-src 'none', frame-ancestors 'none', base-uri
    'self'), and connect-src 'self' so the same-origin SSE stream works
  - a HASH-pinned ``script-src`` — ``'self'`` plus the build-time
    inline-script SHA-256 hashes (issue #185); never ``'unsafe-inline'``
  - X-Content-Type-Options / X-Frame-Options / Referrer-Policy /
    Permissions-Policy
  - NO HSTS: the sidecar serves plain HTTP on 127.0.0.1.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from schemabrain.dashboard import STATIC_DIR
from schemabrain.dashboard.sidecar import (
    SECURITY_HEADERS,
    build_content_security_policy,
    is_ui_available,
)

pytestmark = pytest.mark.skipif(
    not is_ui_available(),
    reason="`schemabrain[ui]` extra not installed",
)

if TYPE_CHECKING:
    from fastapi.testclient import TestClient


# A pair of realistic-looking CSP source hashes (44-char base64 sha256).
_FAKE_HASHES = [
    "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    "sha256-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB=",
]


def _script_src(csp: str) -> str:
    """Pull the ``script-src`` directive out of a serialized CSP string."""
    for directive in csp.split("; "):
        if directive.startswith("script-src"):
            return directive
    raise AssertionError(f"no script-src directive in CSP: {csp!r}")


def _write_manifest(static_dir: Path, hashes: object) -> None:
    (static_dir / "csp-script-hashes.json").write_text(json.dumps(hashes), encoding="utf-8")


def test_security_headers_present_on_every_response(client: TestClient) -> None:
    expected_csp = build_content_security_policy(STATIC_DIR)
    for path in ["/api/health", "/api/meta", "/api/audit/rows", "/api/audit/verify"]:
        response = client.get(path)
        assert response.headers["Content-Security-Policy"] == expected_csp
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


def test_served_csp_never_contains_unsafe_inline_in_script_src(client: TestClient) -> None:
    # Issue #185: a static export has no server to mint nonces, so the
    # inline bootstrap + RSC-flight scripts are pinned by hash instead.
    # The served script-src must NEVER fall back to 'unsafe-inline',
    # regardless of whether a hash manifest is present.
    csp = client.get("/api/meta").headers["Content-Security-Policy"]
    assert "'unsafe-inline'" not in _script_src(csp)


def test_no_hsts_over_plain_localhost_http(client: TestClient) -> None:
    # HSTS over plain HTTP on 127.0.0.1 would be meaningless/harmful.
    assert "Strict-Transport-Security" not in client.get("/api/meta").headers
    assert "Strict-Transport-Security" not in SECURITY_HEADERS


def test_csp_script_src_is_hash_pinned_when_manifest_present(tmp_path: Path) -> None:
    """A present hash manifest produces `script-src 'self' '<hash>'…`."""
    _write_manifest(tmp_path, _FAKE_HASHES)
    script_src = _script_src(build_content_security_policy(tmp_path))
    assert "'unsafe-inline'" not in script_src
    # Pin EXACT order + quoting: the manifest's list order is the served
    # order (the extractor sorts it), and each hash is wrapped in single
    # quotes. A future change that reorders or re-quotes is a regression.
    assert script_src == "script-src 'self' " + " ".join(f"'{h}'" for h in _FAKE_HASHES)


def test_csp_script_src_is_strict_when_manifest_absent(tmp_path: Path) -> None:
    """No manifest → strict `script-src 'self'`, never `'unsafe-inline'`."""
    # tmp_path has no csp-script-hashes.json — the dev / pre-build path.
    assert _script_src(build_content_security_policy(tmp_path)) == "script-src 'self'"


@pytest.mark.parametrize(
    "payload",
    [
        "not json at all {{{",  # malformed JSON
        json.dumps({"hashes": ["sha256-x"]}),  # object, not a list
        json.dumps([1, 2, 3]),  # list of non-strings
        json.dumps(["md5-nope", "sha256-ok="]),  # mixed: drop the bad scheme
        json.dumps(["sha256-abc' 'unsafe-inline"]),  # quote-breakout injection
        json.dumps(["sha256-bad value", "sha256-semi;colon"]),  # space / semicolon
        json.dumps([]),  # empty list
    ],
)
def test_csp_script_src_fails_closed_on_bad_manifest(tmp_path: Path, payload: str) -> None:
    """A malformed/partial manifest must fail CLOSED to strict, not open.

    The one rule the manifest must never break: a broken manifest can
    only ever *narrow* script-src toward `'self'`; it can never reintroduce
    `'unsafe-inline'`. In particular, a hand-edited entry that smuggles a
    quote (``sha256-abc' 'unsafe-inline``) must be dropped wholesale —
    otherwise wrapping it as ``'{h}'`` would break out of the token and
    re-inject ``'unsafe-inline'`` into the directive.
    """
    (tmp_path / "csp-script-hashes.json").write_text(payload, encoding="utf-8")
    script_src = _script_src(build_content_security_policy(tmp_path))
    assert "'unsafe-inline'" not in script_src
    assert script_src.startswith("script-src 'self'")
    # Only the well-formed sha256- entry survives the mixed case.
    if "sha256-ok=" in payload:
        assert "'sha256-ok='" in script_src
        assert "md5-nope" not in script_src
    # Quote/space/semicolon-bearing entries are rejected entirely → strict.
    if "unsafe-inline" in payload or "semi;colon" in payload:
        assert script_src == "script-src 'self'"


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
        assert response.headers["Content-Security-Policy"] == build_content_security_policy(
            static_dir
        )
        assert response.headers["X-Frame-Options"] == "DENY"
        assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_served_csp_is_hash_pinned_end_to_end(store_path, monkeypatch, tmp_path) -> None:
    """The middleware reads the manifest from the live STATIC_DIR.

    Build a sidecar against a static export that ships BOTH a page and a
    hash manifest; the served CSP header must carry the hashes — proving
    the wiring from manifest file → middleware → response header, not just
    the pure builder in isolation."""
    from fastapi.testclient import TestClient

    from schemabrain.dashboard import sidecar as sidecar_module
    from schemabrain.dashboard.sidecar import SidecarConfig, create_sidecar

    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "pii.html").write_text("<!doctype html><title>pii</title>")
    _write_manifest(static_dir, _FAKE_HASHES)
    monkeypatch.setattr(sidecar_module, "STATIC_DIR", static_dir)

    app = create_sidecar(SidecarConfig(store_path=store_path))
    with TestClient(app) as client:
        csp = client.get("/pii").headers["Content-Security-Policy"]
    script_src = _script_src(csp)
    assert "'unsafe-inline'" not in script_src
    for h in _FAKE_HASHES:
        assert f"'{h}'" in script_src
