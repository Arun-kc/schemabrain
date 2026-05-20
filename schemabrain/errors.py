"""Guided error messages for the `schemabrain` CLI.

Replaces single-line `error: <raw>`
prints with a structured `error / why / fix / next` block, so a user
hitting a footgun knows what happened, why, what to change, and where
to look next. The same `Console` infrastructure as `cli_ui.RichReporter`
renders the block — TTY gets color, non-TTY gets plain text.

Two contracts:

1. **Every translator returns a `GuidedError`.** It never raises, never
   logs, never decides exit codes. The CLI calls `render_error` then
   chooses an exit code based on the error kind.

2. **`render_error` is the ONLY place stderr writes happen for error
   output.** The CLI must not `print("error: ...")` directly anywhere
   that has a translator — that bypasses the guided format and creates
   drift between paths a user actually sees.

The error catalog (`error_kind` values) is part of the user-facing
contract because users grep CI logs for these strings. Treat additions
as additive and renames as breaking.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rich.console import Console


@dataclass(frozen=True)
class GuidedError:
    """A failure mode wrapped with remediation context.

    `kind` is a stable, greppable identifier (snake_case). `message` is
    the one-line summary printed as `error: <message>`. `why` explains
    the root cause in plain English (one sentence). `fix` is the
    concrete action the user should take (often a corrected command).
    `next_step` is optional — used when the fix alone isn't enough and
    the user needs a place to look (a docs page, a related command).
    """

    kind: str
    message: str
    why: str | None = None
    fix: str | None = None
    next_step: str | None = None


# Wrong-driver detection. SQLAlchemy resolves a bare `postgresql://`
# URL to the psycopg2 driver by default. We ship psycopg v3 only — any
# scheme that asks for a different driver fails at `create_engine`
# time with a confusing `ModuleNotFoundError: psycopg2`. Reject these
# at the URL-parse boundary instead.
_WRONG_DRIVER_HINT: dict[str, str] = {
    "postgresql": "bare postgresql:// resolves to psycopg2 in SQLAlchemy; Schema Brain uses psycopg v3",
    "postgres": "bare postgres:// resolves to psycopg2 in SQLAlchemy; Schema Brain uses psycopg v3",
    "postgresql+psycopg2": "psycopg v2 is not bundled; Schema Brain uses psycopg v3",
    "postgresql+asyncpg": "asyncpg is not bundled; Schema Brain uses psycopg v3 (sync)",
}


def url_wrong_driver(scheme: str, url: str) -> GuidedError | None:
    """Translate a URL scheme into a `GuidedError` if the driver is wrong.

    Returns `None` if the scheme is `postgresql+psycopg` (the one we
    ship). Anything else listed in `_WRONG_DRIVER_HINT` returns a guided
    error pointing at the right scheme; unknown schemes fall through
    (handled by `_canonical_url`'s "Unsupported scheme" path).
    """
    if scheme == "postgresql+psycopg":
        return None
    if scheme not in _WRONG_DRIVER_HINT:
        return None
    corrected = _swap_scheme(url, "postgresql+psycopg")
    return GuidedError(
        kind="url_wrong_driver",
        message=f"connection URL uses {scheme}:// — Schema Brain only ships psycopg v3",
        why=_WRONG_DRIVER_HINT[scheme],
        fix=f"re-run with: {corrected}",
        next_step="see docs/setup.md for the canonical URL format",
    )


def silent_rewrite_to_psycopg(scheme: str, url: str) -> str | None:
    """Return `url` with the scheme rewritten to `postgresql+psycopg`, if eligible.

    Eligible schemes are the README-friendly bare ones — ``postgresql``
    and ``postgres`` — that every Postgres tool on the planet accepts but
    that fail in SQLAlchemy without a driver suffix. Rewriting silently
    means a first-time user pasting a vanilla URL from Heroku, psql, or
    DataGrip just works, instead of hitting a guided error mid-wizard.

    Returns ``None`` for any other scheme. Explicit wrong-driver choices
    (``postgresql+psycopg2``, ``postgresql+asyncpg``) deliberately fall
    through here so ``url_wrong_driver`` can still surface the guided
    error — the user typed those on purpose and deserves to know we
    can't honor that explicit choice, rather than silently overriding it.
    Unknown schemes (``mysql``, garbage, no scheme) also fall through so
    the existing "Unsupported scheme" path in ``_canonical_url`` keeps
    its single responsibility.
    """
    if scheme in ("postgresql", "postgres"):
        return _swap_scheme(url, "postgresql+psycopg")
    return None


def _swap_scheme(url: str, new_scheme: str) -> str:
    """Replace the scheme of `url` with `new_scheme`, preserving the rest.

    Pure string manipulation rather than re-using `urlparse` so the
    user sees their original URL with only the scheme corrected —
    including any quirks (port, query string, fragment) preserved
    verbatim. Echoing back a re-normalized form would surprise users.
    """
    if "://" not in url:
        return f"{new_scheme}://{url}"
    _, rest = url.split("://", 1)
    return f"{new_scheme}://{rest}"


def postgres_operational_error(exc: Exception, url_hint: str | None = None) -> GuidedError:
    """Translate a psycopg `OperationalError` (or SQLAlchemy wrapper) into
    a guided error.

    Inspects the exception message for known sub-cases:
    - connection refused → likely wrong port / Postgres not running
    - authentication failed → bad password
    - database "X" does not exist → typo'd db name
    - role "X" does not exist → typo'd username
    - could not translate host name → DNS / bad host
    - SSL / TLS handshake / connection closed → network or TLS misconfig

    Falls back to a generic "could not connect" guided error if no
    pattern matches.
    """
    text = str(exc).lower()
    if "connection refused" in text:
        return GuidedError(
            kind="postgres_connection_refused",
            message="could not reach the Postgres server",
            why="the host accepted the TCP connection request but nothing is listening on that port",
            fix="check the host/port in your URL and that Postgres is actually running on it",
            next_step="for Docker: `docker ps | grep postgres` to confirm the container is up",
        )
    if "password authentication failed" in text or "authentication failed" in text:
        return GuidedError(
            kind="postgres_auth_failed",
            message="Postgres rejected the username/password",
            why="the credentials in the URL did not match a known role/password pair",
            fix="double-check user:password in the URL; quote special characters if any (`@`, `:`, `/`)",
            next_step=None,
        )
    if "does not exist" in text and "database" in text:
        return GuidedError(
            kind="postgres_database_missing",
            message="that database does not exist on the server",
            why="connected to the server fine, but the dbname in the URL is unknown",
            fix="verify the dbname (the part after the last `/` in your URL)",
            next_step="list databases via `psql -h <host> -U <user> -l`",
        )
    if "does not exist" in text and "role" in text:
        return GuidedError(
            kind="postgres_role_missing",
            message="that Postgres user does not exist",
            why="connected fine, but the username in the URL has no matching role",
            fix="verify the username (between `://` and `:` in your URL)",
            next_step=None,
        )
    if "could not translate host name" in text or "name or service not known" in text:
        return GuidedError(
            kind="postgres_host_unresolved",
            message="could not resolve the Postgres host name",
            why="DNS lookup for the host part of the URL failed",
            fix="verify the hostname in your URL is correct and reachable",
            next_step="for local Docker containers, the host is usually `localhost`",
        )
    return GuidedError(
        kind="postgres_connection_failed",
        message="could not connect to Postgres",
        why=str(exc).splitlines()[0] if str(exc) else "no further detail from the driver",
        fix=f"verify the connection URL{f' ({url_hint})' if url_hint else ''}",
        next_step="see docs/setup.md for the canonical URL format",
    )


def anthropic_auth_failed(exc: Exception) -> GuidedError:
    """Translate an Anthropic SDK authentication failure into a guided error.

    The SDK raises `anthropic.AuthenticationError` (HTTP 401) for
    invalid / revoked / wrong-format keys. We don't import the SDK
    here — the CLI catches by type and passes the exception in.
    """
    return GuidedError(
        kind="anthropic_auth_failed",
        message="Anthropic API rejected the key",
        why="the ANTHROPIC_API_KEY environment variable is missing, malformed, or revoked",
        fix="verify ANTHROPIC_API_KEY at https://console.anthropic.com/settings/keys",
        next_step="to index without enrichment, re-run with --no-enrich",
    )


def store_path_unwritable(path: str, exc: Exception) -> GuidedError:
    """Translate an OSError on store creation into a guided error."""
    return GuidedError(
        kind="store_path_unwritable",
        message=f"could not open the local store at {path!r}",
        why=str(exc).splitlines()[0] if str(exc) else "OS-level write failure",
        fix="pick a writable path with --store-path, or create the parent directory",
        next_step=None,
    )


def render_error(err: GuidedError, *, console: Console) -> None:
    """Write a `GuidedError` to a `Console` in the guided format.

    Format (TTY gets color; non-TTY gets the same text without
    markup):

        error: <message>
          why:  <why>
          fix:  <fix>
          next: <next_step>

    Indentation makes the why/fix/next read as a sub-block; aligned
    labels keep them scannable. Any of the three sub-fields may be
    `None`, in which case its line is omitted entirely.
    """
    console.print(f"[bold red]error:[/] {err.message}")
    if err.why:
        console.print(f"  [dim]why:[/]  {err.why}")
    if err.fix:
        console.print(f"  [dim]fix:[/]  {err.fix}")
    if err.next_step:
        console.print(f"  [dim]next:[/] {err.next_step}")
