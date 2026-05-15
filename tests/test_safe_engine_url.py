"""Tests for `safe_engine_url` — URL query-string allowlist that
blocks session-config smuggling.

SQLAlchemy passes URL query string params through to libpq. A
malicious URL like
`postgresql+psycopg://user:pw@host/db?options=-c statement_timeout=1`
turns into a session config the agent never asked for. Classic
phishing-shape attack: "paste this connection string."

The fix is structural: parse the URL, allowlist only safe query
params (`sslmode`, `sslrootcert`, `application_name`), and reassemble
before handing to `create_engine`. The raw URL still flows to
`_make_source_id` so a smuggled URL and a clean URL pointing at the
same database produce the same source_id. The filter lives at the
connector boundary (`schemabrain.connectors._url`) so library callers
get it automatically, not only the CLI.

The function should be conservative: any unrecognised query param is
stripped silently. The allowlist is what's explicitly permitted, not
a blocklist of known-bad keys.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from schemabrain.connectors._url import safe_engine_url as _safe_engine_url


class TestStripsKnownSmugglingAttacks:
    """The Datadog-style attacks the function is built to neutralise."""

    def test_strips_options_param(self) -> None:
        attack = "postgresql+psycopg://user:pw@host:5432/db?options=-c statement_timeout=1"
        safe = _safe_engine_url(attack)
        assert "options" not in parse_qs(urlparse(safe).query)

    def test_strips_connect_timeout_smuggle(self) -> None:
        attack = "postgresql+psycopg://user:pw@host/db?connect_timeout=999999"
        safe = _safe_engine_url(attack)
        assert "connect_timeout" not in parse_qs(urlparse(safe).query)

    def test_strips_unknown_params_generally(self) -> None:
        attack = "postgresql+psycopg://user:pw@host/db?evil=anything&random=42"
        safe = _safe_engine_url(attack)
        parsed_q = parse_qs(urlparse(safe).query)
        assert "evil" not in parsed_q
        assert "random" not in parsed_q


class TestPreservesAllowlistedParams:
    """Legitimate connection-config params survive the filter."""

    def test_preserves_sslmode(self) -> None:
        url = "postgresql+psycopg://user:pw@host/db?sslmode=require"
        safe = _safe_engine_url(url)
        assert parse_qs(urlparse(safe).query)["sslmode"] == ["require"]

    def test_preserves_sslrootcert(self) -> None:
        url = "postgresql+psycopg://user:pw@host/db?sslrootcert=/path/to/cert"
        safe = _safe_engine_url(url)
        assert parse_qs(urlparse(safe).query)["sslrootcert"] == ["/path/to/cert"]

    def test_preserves_application_name(self) -> None:
        url = "postgresql+psycopg://user:pw@host/db?application_name=schemabrain"
        safe = _safe_engine_url(url)
        assert parse_qs(urlparse(safe).query)["application_name"] == ["schemabrain"]

    def test_preserves_multiple_allowlisted_params(self) -> None:
        url = "postgresql+psycopg://user:pw@host/db?sslmode=require&application_name=schemabrain"
        safe = _safe_engine_url(url)
        parsed_q = parse_qs(urlparse(safe).query)
        assert parsed_q["sslmode"] == ["require"]
        assert parsed_q["application_name"] == ["schemabrain"]


class TestMixedSafeAndUnsafe:
    """Allowlisted params kept, everything else dropped."""

    def test_keeps_safe_drops_unsafe(self) -> None:
        url = (
            "postgresql+psycopg://user:pw@host/db"
            "?sslmode=require&options=-c statement_timeout=1&evil=yes"
        )
        safe = _safe_engine_url(url)
        parsed_q = parse_qs(urlparse(safe).query)
        assert parsed_q["sslmode"] == ["require"]
        assert "options" not in parsed_q
        assert "evil" not in parsed_q


class TestPreservesIdentityComponents:
    """Scheme, userinfo, host, port, path must round-trip unchanged so
    `create_engine` still connects to the same database."""

    def test_scheme_userinfo_host_port_path_round_trip(self) -> None:
        url = "postgresql+psycopg://user:pw@host.example.com:6432/mydb?evil=yes"
        safe = _safe_engine_url(url)
        parsed = urlparse(safe)
        assert parsed.scheme == "postgresql+psycopg"
        assert parsed.username == "user"
        assert parsed.password == "pw"
        assert parsed.hostname == "host.example.com"
        assert parsed.port == 6432
        assert parsed.path == "/mydb"

    def test_no_query_string_when_nothing_allowlisted(self) -> None:
        url = "postgresql+psycopg://user:pw@host/db?evil=yes"
        safe = _safe_engine_url(url)
        assert urlparse(safe).query == ""

    def test_url_without_query_string_returns_unchanged(self) -> None:
        url = "postgresql+psycopg://user:pw@host/db"
        safe = _safe_engine_url(url)
        assert safe == url


class TestEdgeCases:
    def test_empty_query_string_handled(self) -> None:
        url = "postgresql+psycopg://user:pw@host/db?"
        safe = _safe_engine_url(url)
        # Trailing `?` may or may not be normalised away; what matters
        # is that the URL still parses cleanly and has no smuggled
        # params.
        assert "options" not in parse_qs(urlparse(safe).query)

    def test_duplicate_allowlisted_param_preserved(self) -> None:
        # Postgres treats later values as overrides for some params.
        # We preserve both rather than choosing one.
        url = "postgresql+psycopg://user:pw@host/db?sslmode=disable&sslmode=require"
        safe = _safe_engine_url(url)
        values = parse_qs(urlparse(safe).query)["sslmode"]
        assert values == ["disable", "require"]


@pytest.mark.parametrize(
    "smuggled_param",
    [
        "options",
        "connect_timeout",
        "fallback_application_name",
        "krbsrvname",
        "service",  # libpq service-file lookup
    ],
)
def test_known_smuggling_vectors_blocked(smuggled_param: str) -> None:
    """A handful of libpq params that can change connection behaviour
    in non-obvious ways. All blocked by allowlist default."""
    url = f"postgresql+psycopg://user:pw@host/db?{smuggled_param}=evil"
    safe = _safe_engine_url(url)
    assert smuggled_param not in parse_qs(urlparse(safe).query)


class TestConstructorIntegration:
    """Verify the connector boundary applies `safe_engine_url` so a
    library caller bypassing the CLI still gets the filter."""

    def test_postgres_data_source_strips_smuggled_options(self) -> None:
        """`PostgresDataSource` constructor must hand a filtered URL to
        SQLAlchemy. We monkeypatch `create_engine` to capture the URL
        actually passed in.
        """
        from unittest.mock import MagicMock, patch

        captured: dict[str, str] = {}

        def _capture(url: str, **_: object) -> MagicMock:
            captured["url"] = url
            return MagicMock()

        attack = "postgresql+psycopg://user:pw@host/db?options=-c statement_timeout=1"
        with patch("schemabrain.connectors.postgres.create_engine", _capture):
            from schemabrain.connectors.postgres import PostgresDataSource

            PostgresDataSource(attack)

        assert "options" not in parse_qs(urlparse(captured["url"]).query), (
            "PostgresDataSource must strip non-allowlisted query params before "
            "calling create_engine — library callers bypass CLI-side filters"
        )

    def test_postgres_profiler_strips_smuggled_options(self) -> None:
        from unittest.mock import MagicMock, patch

        captured: dict[str, str] = {}

        def _capture(url: str, **_: object) -> MagicMock:
            captured["url"] = url
            return MagicMock()

        attack = "postgresql+psycopg://user:pw@host/db?options=-c statement_timeout=1"
        with patch("schemabrain.profiler.postgres.create_engine", _capture):
            from schemabrain.profiler.postgres import PostgresProfiler

            PostgresProfiler(attack)

        assert "options" not in parse_qs(urlparse(captured["url"]).query)
