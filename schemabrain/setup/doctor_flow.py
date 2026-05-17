"""`schemabrain doctor` — health checks + orchestration + terminal renderer.

The check matrix surfaces three families of state:

  - **Tooling**: is `uvx` invocable? is the installed `schemabrain`
    a stable release or a dev install?
  - **Host config**: does the Claude Desktop config carry a
    `schemabrain` entry? does that entry use `--url-env` (not the
    deprecated `--source` form that leaks creds to argv)? does its
    version pin match the installed package? does its `--store-path`
    match the store we're checking?
  - **Store + source**: does the local store's schema version match
    this code? does it have any entities indexed? was it written
    recently? if a source URL was supplied, can we reach it, and on
    Postgres, is the session read-only?

Each check is a self-contained function returning a `Check`. The
orchestrator `doctor()` composes the relevant checks based on which
inputs were supplied (config-only when no source URL, source +
read-only when --source/--url-env is given; read-only is gated off
on SQLite source URLs because SQLite has no session-level RO setting).

Warnings never affect the exit code — CI pipelines using doctor as a
health probe shouldn't flake on stale-pin or empty-store warnings.
Failures set exit code 1.
"""

from __future__ import annotations

import shutil
import time
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from rich.console import Console
from sqlalchemy import create_engine, text

from schemabrain.setup.checks import Check, CheckResult, render_json, run_checks
from schemabrain.setup.config_io import (
    MalformedConfigError,
    read_mcp_config,
    schemabrain_entry_in,
)
from schemabrain.setup.hosts import HostName, claude_desktop_config_path

_STALE_AFTER_DAYS = 30
_STALE_AFTER_SECONDS = _STALE_AFTER_DAYS * 24 * 60 * 60


def _installed_version() -> str:
    """Return the installed `schemabrain` version string.

    Wrapped so tests can monkeypatch this single function rather than
    `importlib.metadata.version` globally (which affects unrelated
    imports during test collection).
    """
    return version("schemabrain")


# ----- tooling checks --------------------------------------------------------


def check_uvx_invocable() -> Check:
    """`uvx` is the default snippet runner. Missing is a warn, not a fail.

    The absolute-path fallback in `build_snippet` still produces a
    working snippet when `uvx` is missing, so the runtime isn't
    broken — but reproducibility is weakened (the snippet then
    pins to the current Python env's installed entrypoint, not
    a uvx-resolved one).
    """
    if shutil.which("uvx") is not None:
        return Check(
            name="uvx_invocable",
            outcome="pass",
            message="uvx is on PATH",
        )
    return Check(
        name="uvx_invocable",
        outcome="warn",
        message="uvx is not on PATH; snippet will fall back to the installed entrypoint",
        suggested_next="install uvx via `pip install uv` for reproducible host launches",
    )


def check_installed_version() -> Check:
    """Confirm the installed `schemabrain` reports a stable release version."""
    try:
        v = _installed_version()
    except PackageNotFoundError:
        return Check(
            name="installed_version",
            outcome="warn",
            message="schemabrain package metadata not resolvable (editable install?)",
            suggested_next="run from a normal `pip install schemabrain` for predictable version pinning",
        )
    if "+" in v or "dev" in v.lower():
        # PEP 440 local version segment OR explicit "dev" marker;
        # neither is reproducible via uvx pin.
        return Check(
            name="installed_version",
            outcome="warn",
            message=f"schemabrain {v} looks like a development build",
            suggested_next="install a tagged release if you want reproducible host launches",
        )
    return Check(
        name="installed_version",
        outcome="pass",
        message=f"schemabrain {v}",
    )


# ----- host-config checks ----------------------------------------------------


def _safe_read_config(path: Path) -> tuple[dict[str, Any] | None, Check | None]:
    """Read a config file, returning either the parsed dict or an early Check.

    Centralises the missing-file + malformed-file handling that
    multiple host checks would otherwise duplicate.
    """
    if not path.exists():
        return None, None
    try:
        return read_mcp_config(path), None
    except MalformedConfigError as exc:
        return None, Check(
            name="host_config_present",
            outcome="fail",
            message=str(exc),
            suggested_next="inspect the file or restore from the .bak sibling, then re-run init",
        )


def check_host_config_present(path: Path) -> Check:
    """A `schemabrain` entry must exist in the host's mcpServers map."""
    if not path.exists():
        return Check(
            name="host_config_present",
            outcome="fail",
            message=f"host config not found at {path}",
            suggested_next="run `schemabrain init` to wire the host",
        )
    config, early = _safe_read_config(path)
    if early is not None:
        return early
    if schemabrain_entry_in(config) is None:
        return Check(
            name="host_config_present",
            outcome="fail",
            message="no schemabrain entry in host config",
            suggested_next="run `schemabrain init` to wire the host",
        )
    return Check(
        name="host_config_present",
        outcome="pass",
        message=f"schemabrain entry present in {path.name}",
    )


def check_host_config_uses_url_env(path: Path) -> Check:
    """The snippet's args must use --url-env (creds in env), not --source."""
    config, early = _safe_read_config(path)
    if early is not None:
        return early
    entry = schemabrain_entry_in(config)
    if entry is None:
        return Check(
            name="host_config_uses_url_env",
            outcome="warn",
            message="no schemabrain entry to evaluate (see host_config_present)",
        )
    args = entry.get("args", [])
    if isinstance(args, list) and "--source" in args:
        return Check(
            name="host_config_uses_url_env",
            outcome="fail",
            message="snippet uses --source (puts credentials in argv)",
            suggested_next="re-run `schemabrain init` to migrate to --url-env",
        )
    if isinstance(args, list) and "--url-env" in args:
        return Check(
            name="host_config_uses_url_env",
            outcome="pass",
            message="snippet uses --url-env (credentials live in env block)",
        )
    return Check(
        name="host_config_uses_url_env",
        outcome="warn",
        message="snippet has neither --url-env nor --source in args",
        suggested_next="re-run `schemabrain init` to write a canonical snippet",
    )


def check_host_config_version_pin_matches(path: Path) -> Check:
    """The snippet's `schemabrain==X.Y.Z` pin should match the installed version."""
    config, early = _safe_read_config(path)
    if early is not None:
        return early
    entry = schemabrain_entry_in(config)
    if entry is None:
        return Check(
            name="host_config_version_pin",
            outcome="warn",
            message="no schemabrain entry to evaluate",
        )
    args = entry.get("args", [])
    pin = None
    if isinstance(args, list):
        for arg in args:
            if isinstance(arg, str) and arg.startswith("schemabrain=="):
                pin = arg.split("==", 1)[1]
                break
    if pin is None:
        return Check(
            name="host_config_version_pin",
            outcome="warn",
            message="snippet has no `schemabrain==X.Y.Z` pin (absolute-path runner?)",
        )
    try:
        installed = _installed_version()
    except PackageNotFoundError:
        return Check(
            name="host_config_version_pin",
            outcome="warn",
            message=f"snippet pins {pin}; installed version unresolvable",
        )
    if pin != installed:
        return Check(
            name="host_config_version_pin",
            outcome="warn",
            message=f"snippet pins {pin}, installed is {installed}",
            suggested_next="re-run `schemabrain init` to refresh the pin",
        )
    return Check(
        name="host_config_version_pin",
        outcome="pass",
        message=f"snippet pin matches installed ({installed})",
    )


def check_host_config_store_path_matches(path: Path, *, expected: Path) -> Check:
    """The snippet's `--store-path` should match the store doctor is checking."""
    config, early = _safe_read_config(path)
    if early is not None:
        return early
    entry = schemabrain_entry_in(config)
    if entry is None:
        return Check(
            name="host_config_store_path",
            outcome="warn",
            message="no schemabrain entry to evaluate",
        )
    args = entry.get("args", [])
    snippet_store: str | None = None
    if isinstance(args, list):
        for i, arg in enumerate(args):
            if arg == "--store-path" and i + 1 < len(args):
                next_arg = args[i + 1]
                if isinstance(next_arg, str):
                    snippet_store = next_arg
                break
    if snippet_store is None:
        return Check(
            name="host_config_store_path",
            outcome="warn",
            message="snippet has no --store-path arg",
        )
    expected_str = str(expected.expanduser().resolve())
    if Path(snippet_store).expanduser().resolve() != Path(expected_str):
        return Check(
            name="host_config_store_path",
            outcome="warn",
            message=f"snippet store-path ({snippet_store}) differs from {expected_str}",
            suggested_next="re-run `schemabrain init --store-path` to align, or update --store-path in this command",
        )
    return Check(
        name="host_config_store_path",
        outcome="pass",
        message=f"snippet --store-path matches ({expected_str})",
    )


# ----- store checks ----------------------------------------------------------


def check_store_schema_version(path: Path) -> Check:
    """The store file's schema version must match what this code expects."""
    if not path.exists():
        return Check(
            name="store_schema_version",
            outcome="warn",
            message=f"store not found at {path}",
            suggested_next="run `schemabrain index` to create and populate the store",
        )
    # Defer import — SQLiteStore lifecycle is heavy; the store check
    # is the only doctor surface that needs it.
    from schemabrain.core.store import SchemaVersionMismatchError, SQLiteStore

    try:
        store = SQLiteStore(path=path)
    except SchemaVersionMismatchError as exc:
        return Check(
            name="store_schema_version",
            outcome="fail",
            message=str(exc),
            suggested_next="delete the store file and re-run `schemabrain index`, or downgrade to the matching schemabrain version",
        )
    store.close()
    return Check(
        name="store_schema_version",
        outcome="pass",
        message="store schema version matches",
    )


def check_store_entity_count(path: Path) -> Check:
    """At least one entity should be defined for the agent to query against."""
    if not path.exists():
        return Check(
            name="store_entity_count",
            outcome="warn",
            message="store not found",
            suggested_next="run `schemabrain index` to populate the store",
        )
    from schemabrain.core.store import SchemaVersionMismatchError, SQLiteStore

    try:
        store = SQLiteStore(path=path)
    except SchemaVersionMismatchError:
        return Check(
            name="store_entity_count",
            outcome="warn",
            message="cannot evaluate (schema-version mismatch — see store_schema_version)",
        )
    try:
        count = len(store.list_entities())
    finally:
        store.close()
    if count == 0:
        return Check(
            name="store_entity_count",
            outcome="warn",
            message="no entities indexed yet",
            suggested_next="run `schemabrain entities apply` or `schemabrain entities suggest`",
        )
    return Check(
        name="store_entity_count",
        outcome="pass",
        message=f"{count} entit{'y' if count == 1 else 'ies'} in store",
    )


def check_store_freshness(path: Path) -> Check:
    """The most recently indexed table should be no older than 30 days."""
    if not path.exists():
        return Check(
            name="store_freshness",
            outcome="warn",
            message="store not found",
        )
    # Read the raw indexed_at via sqlite3 — no need to instantiate the
    # full SQLiteStore for a single MAX(...) query.
    import sqlite3

    try:
        conn = sqlite3.connect(path)
        try:
            row = conn.execute("SELECT MAX(indexed_at) FROM tables").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return Check(
            name="store_freshness",
            outcome="warn",
            message=f"could not query freshness: {exc}",
        )
    if row is None or row[0] is None:
        return Check(
            name="store_freshness",
            outcome="warn",
            message="no tables indexed yet",
        )
    age_seconds = time.time() - row[0]
    if age_seconds > _STALE_AFTER_SECONDS:
        days = int(age_seconds // (24 * 60 * 60))
        return Check(
            name="store_freshness",
            outcome="warn",
            message=f"store last indexed {days} days ago",
            suggested_next="re-run `schemabrain index` to refresh schema state",
        )
    return Check(
        name="store_freshness",
        outcome="pass",
        message=f"store indexed within the last {_STALE_AFTER_DAYS} days",
    )


# ----- source checks ---------------------------------------------------------


def _is_postgres_url(url: str) -> bool:
    return url.startswith(("postgresql:", "postgresql+", "postgres:"))


def check_source_reachable(source_url: str) -> Check:
    """SELECT 1 against the source. fail on any connection or query error."""
    try:
        engine = create_engine(source_url)
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        finally:
            engine.dispose()
    except Exception as exc:
        return Check(
            name="source_reachable",
            outcome="fail",
            message=f"could not execute SELECT 1: {type(exc).__name__}: {exc}".splitlines()[0],
            suggested_next="verify the connection URL and that the server is running",
        )
    return Check(
        name="source_reachable",
        outcome="pass",
        message="connected and executed SELECT 1",
    )


def check_source_read_only(source_url: str) -> Check:
    """Postgres session-level `default_transaction_read_only` must report `on`.

    Called only for Postgres URLs (the orchestrator gates this off
    for SQLite, which has no session-level RO setting).
    """
    try:
        engine = create_engine(
            source_url,
            connect_args={"options": "-c default_transaction_read_only=on"},
        )
        try:
            with engine.connect() as conn:
                result = conn.execute(text("SHOW default_transaction_read_only"))
                value = result.scalar()
        finally:
            engine.dispose()
    except Exception as exc:
        return Check(
            name="source_session_read_only",
            outcome="fail",
            message=f"could not verify read-only session: {type(exc).__name__}: {exc}".splitlines()[
                0
            ],
            suggested_next="ensure the role has SET-able session params and the URL points at Postgres",
        )
    if value != "on":
        return Check(
            name="source_session_read_only",
            outcome="fail",
            message=f"default_transaction_read_only is {value!r}; expected 'on'",
            suggested_next="this is a security regression — re-run `schemabrain init` to refresh the snippet",
        )
    return Check(
        name="source_session_read_only",
        outcome="pass",
        message="session default_transaction_read_only=on",
    )


# ----- orchestrator ----------------------------------------------------------


def doctor(
    *,
    source_url: str | None,
    store_path: Path,
    host: HostName = "claude-desktop",
) -> CheckResult:
    """Run the doctor check matrix and return the aggregated result.

    `host="manual"` skips all host-config checks (there's nothing to
    look up). `source_url=None` skips source checks. A SQLite source
    URL adds source-reachable but skips read-only (SQLite has no
    session-level RO setting).
    """
    checks = _build_check_list(
        source_url=source_url,
        store_path=store_path,
        host=host,
    )
    return run_checks(checks)


def _build_check_list(
    *,
    source_url: str | None,
    store_path: Path,
    host: HostName,
) -> list[Callable[[], Check]]:
    """Compose the relevant checks based on inputs supplied.

    Lambdas wrap the per-check functions to capture their bound args.
    The wrapped check names live inside the returned `Check`, so the
    lambdas' opaque `<lambda>` __name__ only matters in the rare
    case where a check raises rather than returning — and even then
    the `run_checks` fallback synthesises a visible fail row.
    """
    checks: list[Callable[[], Check]] = [
        check_uvx_invocable,
        check_installed_version,
    ]
    if host == "claude-desktop":
        cfg_path = claude_desktop_config_path()
        if cfg_path is not None:
            checks.append(lambda: check_host_config_present(cfg_path))
            checks.append(lambda: check_host_config_uses_url_env(cfg_path))
            checks.append(lambda: check_host_config_version_pin_matches(cfg_path))
            checks.append(
                lambda: check_host_config_store_path_matches(cfg_path, expected=store_path)
            )
    checks.append(lambda: check_store_schema_version(store_path))
    checks.append(lambda: check_store_entity_count(store_path))
    checks.append(lambda: check_store_freshness(store_path))
    if source_url is not None:
        checks.append(lambda: check_source_reachable(source_url))
        if _is_postgres_url(source_url):
            checks.append(lambda: check_source_read_only(source_url))
    return checks


# ----- renderers -------------------------------------------------------------


_GLYPHS: dict[str, str] = {
    "pass": "[green]✓[/]",
    "warn": "[yellow]⚠[/]",
    "fail": "[red]✗[/]",
}


def render_doctor(result: CheckResult, *, console: Console) -> None:
    """Render the result as a colored summary + per-check lines.

    Output shape:

        Schema Brain doctor — 3 pass, 2 warn, 0 fail

          ✓ uvx_invocable          uvx is on PATH
          ⚠ store_entity_count     no entities indexed yet
                                   → run `schemabrain entities apply` or ...

    Plain-text consoles get the same layout without color markup.
    """
    s = result.summary
    console.print(
        f"[bold]Schema Brain doctor[/] — {s.passed} pass, {s.warned} warn, {s.failed} fail"
    )
    console.print()
    for c in result.checks:
        glyph = _GLYPHS.get(c.outcome, c.outcome)
        console.print(f"  {glyph} [bold]{c.name}[/]  {c.message}")
        if c.suggested_next:
            console.print(f"      [dim]→[/] {c.suggested_next}")


def render_doctor_json(result: CheckResult) -> str:
    """Render the result as a JSON string matching the doctor --json contract.

    Identical to `setup.checks.render_json`; aliased here so callers
    can `from schemabrain.setup.doctor_flow import render_doctor_json`
    without reaching into the lower-level module.
    """
    return render_json(result)


__all__ = [
    "check_host_config_present",
    "check_host_config_store_path_matches",
    "check_host_config_uses_url_env",
    "check_host_config_version_pin_matches",
    "check_installed_version",
    "check_source_reachable",
    "check_source_read_only",
    "check_store_entity_count",
    "check_store_freshness",
    "check_store_schema_version",
    "check_uvx_invocable",
    "doctor",
    "render_doctor",
    "render_doctor_json",
]
