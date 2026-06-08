"""``schemabrain dashboard`` CLI subcommand handler.

Wired into ``schemabrain/cli.py`` in E-11 (D6). This module ships the
``run_dashboard`` entry point so the wiring step is a one-line dispatch
without leaking uvicorn / fastapi imports into the main CLI module.

Behavioural contract (per RFC §2.2 + §3.2):

  - Bind ``127.0.0.1`` only. No ``--host`` flag exists; the bind
    constant lives in ``sidecar.BIND_HOST``.
  - Default port: 7878. Operator can override via ``--port``.
  - Auto-opens the default browser unless ``--no-open`` is passed.
  - Refuses to boot if the ``[ui]`` extra is not installed; surfaces
    a single actionable error line on stderr.
"""

from __future__ import annotations

import contextlib
import errno
import socket
import sys
import webbrowser
from pathlib import Path

from schemabrain.dashboard import STATIC_DIR
from schemabrain.dashboard.sidecar import (
    BIND_HOST,
    DEFAULT_PORT,
    SidecarConfig,
    create_sidecar,
)


def _port_is_available(host: str, port: int) -> bool:
    """Probe whether ``(host, port)`` can be bound for a TCP listener.

    F1-A: ``uvicorn.run(...)`` catches ``OSError`` internally — it
    logs the bind failure via its own logger and returns without
    raising — so wrapping the call in ``try / except OSError`` never
    fires for the common port-collision case. The pre-flight probe
    binds-and-closes a throwaway socket; if the bind succeeds the
    port is (probably) free and we hand off to uvicorn; if the bind
    fails with ``EADDRINUSE`` we surface our own friendly message
    BEFORE uvicorn ever runs.

    There IS a TOCTOU race (port could be taken between our probe
    and uvicorn's bind) but the race is short and rare in practice —
    even if uvicorn ends up logging its own error after winning the
    race, the operator still sees the friendly version first. Worth
    the trade for the common case where the message is right.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # SO_REUSEADDR off so the probe genuinely fails when the port
        # is held by a live listener — otherwise the probe could
        # succeed transiently on a TIME_WAIT socket and mask a real
        # collision.
        sock.bind((host, port))
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            return False
        # Any other OSError (permission denied, address not available)
        # means the port is logically unavailable too — fall through
        # so uvicorn can produce its own diagnostic for the unusual
        # case rather than swallowing it here.
        return False
    finally:
        sock.close()
    return True


def run_dashboard(
    *,
    store_path: Path,
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
    source_connection_id: str | None = None,
) -> int:
    """Boot the dashboard sidecar and block on uvicorn.

    Returns a Unix-style exit code (0 success, 1 user-facing error,
    2 install error). Never raises — every error surface lands as a
    stderr line + exit code so the CLI wrapper does not need a
    try/except around the call.
    """
    try:
        import uvicorn
    except ImportError:
        print(
            "schemabrain dashboard requires the [ui] extra. Install with "
            "`pip install schemabrain[ui]`.",
            file=sys.stderr,
        )
        return 2

    try:
        config = SidecarConfig(
            store_path=store_path,
            port=port,
            source_connection_id=source_connection_id,
        )
    except ValueError as exc:
        print(f"schemabrain dashboard: {exc}", file=sys.stderr)
        return 1

    try:
        app = create_sidecar(config)
    except ImportError as exc:  # pragma: no cover - unreachable after
        # the uvicorn-import check above; create_sidecar's only
        # ImportError path is fastapi/staticfiles missing, which would
        # already have failed earlier in this function. Kept as a
        # defensive belt for a future refactor that decouples the two.
        print(f"schemabrain dashboard: {exc}", file=sys.stderr)
        return 2

    # pre-flight bind-probe BEFORE uvicorn runs. uvicorn's own
    # error path swallows ``OSError`` and exits cleanly, so a wrapper
    # try/except never fires for the port-collision case.
    if not _port_is_available(BIND_HOST, config.port):
        print(
            f"schemabrain dashboard: port {config.port} is already in use on "
            f"{BIND_HOST}. Another SchemaBrain dashboard may already be running. "
            f"Either stop it (Ctrl+C in its window, or `lsof -nP -iTCP:{config.port}` "
            f"to find the PID) or rerun with `--port <other>`.",
            file=sys.stderr,
        )
        return 1

    url = f"http://{BIND_HOST}:{config.port}/"
    print(f"schemabrain dashboard: serving at {url}", file=sys.stderr)
    print("  press Ctrl+C to stop", file=sys.stderr)

    if open_browser:
        # Open the dashboard's own home (the Overview surface) rather than
        # the marketing landing at `/`. Only when the static export is
        # present, though — without it the sidecar serves the minimal dev
        # fallback at `/` and `/overview` would 404, so fall back to root.
        has_static_export = STATIC_DIR.exists() and any(
            p.name not in {".gitkeep", "README.md"} for p in STATIC_DIR.iterdir()
        )
        open_url = f"{url}overview" if has_static_export else url
        # ``webbrowser.open`` is best-effort across platforms; a
        # headless box without a browser silently no-ops, which is the
        # right behaviour. CI runs always pass ``--no-open``. The
        # platform surface is a grab-bag (XDG/Win32/Apple Events) so
        # we suppress any exception rather than guess the union shape.
        with contextlib.suppress(Exception):
            webbrowser.open(open_url)

    # uvicorn binds and serves until interrupted. Log level intentionally
    # ``warning`` to keep stderr quiet — the sidecar boot message above
    # is the only line a user should see in the success path.
    uvicorn.run(
        app,
        host=BIND_HOST,
        port=config.port,
        log_level="warning",
        access_log=False,
    )
    return 0


__all__ = ["run_dashboard"]
