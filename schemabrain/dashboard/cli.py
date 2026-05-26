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
import sys
import webbrowser
from pathlib import Path

from schemabrain.dashboard.sidecar import (
    BIND_HOST,
    DEFAULT_PORT,
    SidecarConfig,
    create_sidecar,
)


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
    except ImportError as exc:
        print(f"schemabrain dashboard: {exc}", file=sys.stderr)
        return 2

    url = f"http://{BIND_HOST}:{config.port}/"
    print(f"schemabrain dashboard: serving at {url}", file=sys.stderr)
    print("  press Ctrl+C to stop", file=sys.stderr)

    if open_browser:
        # ``webbrowser.open`` is best-effort across platforms; a
        # headless box without a browser silently no-ops, which is the
        # right behaviour. CI runs always pass ``--no-open``. The
        # platform surface is a grab-bag (XDG/Win32/Apple Events) so
        # we suppress any exception rather than guess the union shape.
        with contextlib.suppress(Exception):
            webbrowser.open(url)

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
