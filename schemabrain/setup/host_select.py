"""Interactive host-selection prompt for ``schemabrain init``.

Renders the 5-option menu when ``--host`` was not passed explicitly
and stderr is an interactive TTY. The menu lists all 4 supported
first-party hosts plus ``manual``; rows whose presence we detect on
this machine get a ✓ chip so the operator sees what the wizard would
have auto-picked. Default cursor points at the priority-winner from
``detect_host`` (or ``manual`` when nothing is detected) so pressing
Enter does what auto-detect would have done.

Why a separate module rather than inlining in ``cli.py`` or
``setup_stage.py``:

- ``cli.py`` is 7000+ LOC; another inline prompt would noise up the
  ``_cmd_init`` block past the point of readability.
- ``setup_stage.py`` owns the *source* fork prompt (own DB vs demo).
  Host selection is conceptually a different question (where to
  write the wiring) — separating modules keeps each focused on one
  axis of the wizard's input space.

The non-TTY / ``--yes`` path collapses to ``detect_host`` (single
priority-winner) without prompting — that's handled by the caller
in ``_cmd_init``, not here. This helper assumes interactive mode and
blocks on stdin.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from schemabrain._ui import (
    GLYPH_ACTIVE,
    GLYPH_BRAND,
    GLYPH_OK,
    pause_active_spinner,
)
from schemabrain.setup.hosts import (
    HostName,
    claude_desktop_config_path,
    cursor_config_path,
    detect_available_hosts,
    detect_host,
    windsurf_config_path,
)

if TYPE_CHECKING:
    from rich.console import Console

__all__ = ["prompt_for_host_selection"]


# Menu rows in priority order. Each tuple is (host name, label,
# config-path description). The path description is rendered dim
# next to the label so the operator confirms what would get written.
# Tuple is module-level so tests can assert against the exact set
# without re-deriving it.
# Per-row description is provided by `_resolved_path_for()` (it
# substitutes ~ into resolved Path values), so each row stores
# only (host_name, label). Pinned for stable iteration order.
_MENU_ROWS: tuple[tuple[HostName, str], ...] = (
    ("claude-desktop", "Claude Desktop"),
    ("claude-code", "Claude Code"),
    ("cursor", "Cursor"),
    ("windsurf", "Windsurf"),
    ("manual", "Manual"),
)


def _resolved_path_for(host: HostName) -> str:
    """Render the actual config path for `host` (best-effort fallback to label).

    For claude-desktop on Linux the resolver returns ``None`` (no
    official build); the menu shows the platform-neutral description
    in that case. Other hosts always resolve to a path; we render
    the home-relative form via ``~`` substitution for readability.
    """
    if host == "claude-desktop":
        path = claude_desktop_config_path()
        if path is None:
            return "no Claude Desktop build on this OS"
        return _home_relative(str(path))
    if host == "cursor":
        return _home_relative(str(cursor_config_path()))
    if host == "windsurf":
        return _home_relative(str(windsurf_config_path()))
    if host == "claude-code":
        return "shells out to `claude mcp add`"
    return "print snippet to stdout"


def _home_relative(path: str) -> str:
    """Replace the user's home directory with ``~`` for display."""
    from pathlib import Path

    home = str(Path.home())
    if path.startswith(home):
        return "~" + path[len(home) :]
    return path


def prompt_for_host_selection(*, console: Console) -> HostName:
    """Show the host-selection menu and return the chosen host.

    Top-level entry point used by ``_cmd_init`` when ``--host`` was
    not passed AND stderr is an interactive TTY. The caller is
    responsible for gating on both conditions — this helper assumes
    interactive mode is appropriate and blocks on stdin.

    The default cursor points at the priority winner from
    ``detect_host``, so pressing Enter does what auto-detect would
    have done. Operators with a different installed host (or who
    want manual mode) type a number to override.

    ``KeyboardInterrupt`` propagates verbatim so Ctrl-C aborts the
    wizard cleanly; the caller in ``cli.py`` already translates that
    into exit-130.
    """
    from rich.prompt import Prompt

    detected = set(detect_available_hosts())
    default_host = detect_host()
    default_idx = next(
        (str(i + 1) for i, (name, _label) in enumerate(_MENU_ROWS) if name == default_host),
        "5",  # falls back to "manual" — the last row
    )

    with pause_active_spinner():
        console.print()
        console.print(
            f"  [bold]{GLYPH_BRAND} SchemaBrain — wire SchemaBrain into which MCP host?[/]"
        )
        console.print()
        for idx, (name, label) in enumerate(_MENU_ROWS, start=1):
            chip = (
                f"[green]{GLYPH_OK} detected[/]" if name in detected else "[bright_black]      [/]"
            )
            path = _resolved_path_for(name)
            console.print(
                f"    [bright_black]{GLYPH_ACTIVE} {idx}. {label:<14}[/] {chip}  "
                f"[bright_black]· {path}[/]"
            )
        console.print()
        choice = Prompt.ask(
            "  [cyan]Choice[/]",
            console=console,
            choices=[str(i) for i in range(1, len(_MENU_ROWS) + 1)],
            default=default_idx,
            show_default=True,
            show_choices=False,
        )
    return _MENU_ROWS[int(choice) - 1][0]
