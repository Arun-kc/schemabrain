"""Tests for `schemabrain.errors` — the guided-error catalog + renderer.

Goals (not pixel-perfect output assertion):

  1. Each translator returns a `GuidedError` with the expected `kind`
     and a non-empty `fix`. The exact wording of `why`/`fix` may evolve;
     `kind` is the stable contract because users grep CI logs for it.
  2. `render_error` writes the same load-bearing tokens whether stderr
     is a TTY (rich markup) or not (plain text).
  3. `url_wrong_driver` correctly preserves user-supplied URL quirks
     (host, port, credentials, dbname) while swapping only the scheme.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from schemabrain.errors import (
    GuidedError,
    anthropic_auth_failed,
    postgres_operational_error,
    render_error,
    silent_rewrite_to_psycopg,
    store_path_unwritable,
    url_wrong_driver,
)


@pytest.fixture
def tty_console() -> tuple[Console, io.StringIO]:
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, width=120, record=True)
    return console, buf


@pytest.fixture
def plain_console() -> tuple[Console, io.StringIO]:
    """A Console that emulates non-TTY stderr — no terminal, no color."""
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, no_color=True, width=120)
    return console, buf


class TestUrlWrongDriver:
    def test_bare_postgresql_returns_guided_error(self) -> None:
        err = url_wrong_driver("postgresql", "postgresql://user:pw@host:5432/db")
        assert err is not None
        assert err.kind == "url_wrong_driver"
        assert "postgresql+psycopg://user:pw@host:5432/db" in (err.fix or "")

    def test_postgres_alias_returns_guided_error(self) -> None:
        err = url_wrong_driver("postgres", "postgres://host/db")
        assert err is not None
        assert err.kind == "url_wrong_driver"
        assert "postgresql+psycopg://host/db" in (err.fix or "")

    def test_psycopg2_explicit_driver_returns_guided_error(self) -> None:
        err = url_wrong_driver("postgresql+psycopg2", "postgresql+psycopg2://host/db")
        assert err is not None
        assert err.kind == "url_wrong_driver"
        assert "psycopg v3" in (err.why or "")

    def test_asyncpg_driver_returns_guided_error(self) -> None:
        err = url_wrong_driver("postgresql+asyncpg", "postgresql+asyncpg://host/db")
        assert err is not None
        assert err.kind == "url_wrong_driver"
        assert "asyncpg" in (err.why or "")

    def test_correct_driver_returns_none(self) -> None:
        # Happy path: no guided error needed.
        assert url_wrong_driver("postgresql+psycopg", "postgresql+psycopg://host/db") is None

    def test_unknown_scheme_returns_none(self) -> None:
        # `mysql://` falls through here; `_canonical_url`'s
        # "Unsupported scheme" path handles it instead.
        assert url_wrong_driver("mysql", "mysql://host/db") is None

    def test_scheme_swap_preserves_credentials_port_and_query(self) -> None:
        # Echo the user's URL with ONLY the scheme corrected — quirks
        # they intentionally added (port, query string) survive.
        err = url_wrong_driver(
            "postgresql",
            "postgresql://alice:s3cret@example.com:6543/mydb?sslmode=require",
        )
        assert err is not None
        assert "postgresql+psycopg://alice:s3cret@example.com:6543/mydb?sslmode=require" in (
            err.fix or ""
        )

    def test_scheme_swap_handles_url_without_scheme_separator(self) -> None:
        # Defensive branch: if the URL somehow reached the translator
        # without "://" in it (unusual — `urlparse` populates the
        # scheme field even on malformed input), the swap helper still
        # produces a valid corrected URL by treating the whole input
        # as the host+path portion.
        err = url_wrong_driver("postgresql", "no-slashes-here")
        assert err is not None
        assert "postgresql+psycopg://no-slashes-here" in (err.fix or "")


class TestSilentRewriteToPsycopg:
    def test_bare_postgresql_rewrites(self) -> None:
        # The README footgun — every Postgres tool accepts `postgresql://`
        # but SQLAlchemy needs the `+psycopg` suffix. Forcing every
        # first-time user to learn this would be pure friction.
        out = silent_rewrite_to_psycopg("postgresql", "postgresql://user:pw@host:5432/db")
        assert out == "postgresql+psycopg://user:pw@host:5432/db"

    def test_postgres_alias_rewrites(self) -> None:
        # `postgres://` is the Heroku-style default. Same silent rewrite.
        out = silent_rewrite_to_psycopg("postgres", "postgres://host/db")
        assert out == "postgresql+psycopg://host/db"

    def test_explicit_psycopg2_returns_none(self) -> None:
        # Explicit wrong driver — the user typed `+psycopg2` on purpose,
        # so they deserve the guided error from `url_wrong_driver`, not
        # a silent override that loses their explicit choice.
        assert (
            silent_rewrite_to_psycopg("postgresql+psycopg2", "postgresql+psycopg2://host/db")
            is None
        )

    def test_explicit_asyncpg_returns_none(self) -> None:
        assert (
            silent_rewrite_to_psycopg("postgresql+asyncpg", "postgresql+asyncpg://host/db") is None
        )

    def test_correct_driver_returns_none(self) -> None:
        # Already canonical — no rewrite needed; lets callers know they
        # can skip the urlparse re-walk.
        assert (
            silent_rewrite_to_psycopg("postgresql+psycopg", "postgresql+psycopg://host/db") is None
        )

    def test_unknown_scheme_returns_none(self) -> None:
        # `mysql://` falls through to `_canonical_url`'s "Unsupported
        # scheme" path — silent rewrite must not mask that.
        assert silent_rewrite_to_psycopg("mysql", "mysql://host/db") is None

    def test_rewrite_preserves_credentials_port_and_query(self) -> None:
        # Quirks the user added intentionally (port, query string,
        # credentials) must survive the rewrite verbatim — only the
        # scheme flips.
        out = silent_rewrite_to_psycopg(
            "postgresql",
            "postgresql://alice:s3cret@example.com:6543/mydb?sslmode=require",
        )
        assert out == "postgresql+psycopg://alice:s3cret@example.com:6543/mydb?sslmode=require"


class TestPostgresOperationalError:
    def test_connection_refused_detected(self) -> None:
        exc = Exception(
            "connection to server at localhost (::1), port 5432 failed: connection refused"
        )
        err = postgres_operational_error(exc)
        assert err.kind == "postgres_connection_refused"
        assert err.fix is not None

    def test_auth_failed_detected(self) -> None:
        exc = Exception('FATAL:  password authentication failed for user "alice"')
        err = postgres_operational_error(exc)
        assert err.kind == "postgres_auth_failed"

    def test_database_missing_detected(self) -> None:
        exc = Exception('FATAL:  database "missingdb" does not exist')
        err = postgres_operational_error(exc)
        assert err.kind == "postgres_database_missing"

    def test_role_missing_detected(self) -> None:
        exc = Exception('FATAL:  role "ghost" does not exist')
        err = postgres_operational_error(exc)
        assert err.kind == "postgres_role_missing"

    def test_host_unresolved_detected(self) -> None:
        exc = Exception("could not translate host name 'nope.invalid' to address")
        err = postgres_operational_error(exc)
        assert err.kind == "postgres_host_unresolved"

    def test_unknown_falls_back_to_generic(self) -> None:
        exc = Exception("a weird new postgres error not in the catalog")
        err = postgres_operational_error(exc, url_hint="postgresql+psycopg://x")
        assert err.kind == "postgres_connection_failed"
        # url_hint is surfaced in the fix line so the user can see what
        # we were trying to connect to.
        assert "postgresql+psycopg://x" in (err.fix or "")


class TestAnthropicAuthFailed:
    def test_returns_guided_error_with_console_url(self) -> None:
        err = anthropic_auth_failed(Exception("401 unauthorized"))
        assert err.kind == "anthropic_auth_failed"
        assert "console.anthropic.com" in (err.fix or "")
        # The fallback (--no-enrich) is a load-bearing next-step: keeps
        # users unblocked when they can't fix the key immediately.
        assert "--no-enrich" in (err.next_step or "")


class TestStorePathUnwritable:
    def test_returns_guided_error_with_path(self) -> None:
        err = store_path_unwritable("/root/forbidden.db", OSError("Permission denied"))
        assert err.kind == "store_path_unwritable"
        assert "/root/forbidden.db" in err.message


class TestRenderError:
    def test_writes_error_line_with_message(self, tty_console: tuple[Console, io.StringIO]) -> None:
        console, _ = tty_console
        render_error(
            GuidedError(kind="x", message="something broke", why="cause", fix="do this"),
            console=console,
        )
        rendered = console.export_text()
        assert "error: something broke" in rendered
        assert "why:" in rendered
        assert "fix:" in rendered
        assert "cause" in rendered
        assert "do this" in rendered

    def test_omits_lines_for_none_fields(self, tty_console: tuple[Console, io.StringIO]) -> None:
        console, _ = tty_console
        render_error(
            GuidedError(kind="x", message="just the headline"),
            console=console,
        )
        rendered = console.export_text()
        assert "error: just the headline" in rendered
        # Optional fields with None values must NOT print empty
        # "why:" / "fix:" / "next:" labels.
        assert "why:" not in rendered
        assert "fix:" not in rendered
        assert "next:" not in rendered

    def test_next_step_line_renders_when_present(
        self, tty_console: tuple[Console, io.StringIO]
    ) -> None:
        # NOTE: `console.export_text()` is destructive (clears the
        # buffer by default in rich 14+), so call it ONCE and inspect
        # the captured string.
        console, _ = tty_console
        render_error(
            GuidedError(
                kind="x", message="m", why="w", fix="f", next_step="see https://example.com"
            ),
            console=console,
        )
        rendered = console.export_text()
        assert "next:" in rendered
        assert "see https://example.com" in rendered

    def test_plain_console_renders_without_ansi(
        self, plain_console: tuple[Console, io.StringIO]
    ) -> None:
        # Non-TTY consumers (CI logs, file pipes, `tee`) must get the
        # same content with NO ANSI escape bytes. Rich strips markup
        # automatically when force_terminal=False + no_color=True.
        console, buf = plain_console
        render_error(
            GuidedError(kind="x", message="plain test", why="w", fix="f"),
            console=console,
        )
        output = buf.getvalue()
        assert "error: plain test" in output
        assert "why:" in output
        assert "fix:" in output
        # The CSI introducer (\x1b[) marks every ANSI color code; its
        # absence is proof rich stripped them.
        assert "\x1b[" not in output


class TestGuidedErrorDataclass:
    def test_is_frozen(self) -> None:
        # Immutability is part of the contract — translators return
        # an `err`; callers MUST NOT mutate it before rendering.
        # dataclass(frozen=True) raises FrozenInstanceError on
        # attribute set.
        from dataclasses import FrozenInstanceError

        err = GuidedError(kind="x", message="m")
        with pytest.raises(FrozenInstanceError):
            err.kind = "y"  # type: ignore[misc]

    def test_message_only_construction(self) -> None:
        # why/fix/next_step are all optional — short errors get a
        # single "error: ..." line, full guidance only when useful.
        err = GuidedError(kind="x", message="brief")
        assert err.why is None
        assert err.fix is None
        assert err.next_step is None
