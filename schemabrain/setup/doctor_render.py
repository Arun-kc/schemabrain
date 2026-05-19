"""``schemabrain doctor`` terminal renderer — design-bundle hero surface.

Composes the ``CheckResult`` produced by ``setup.doctor_flow.doctor()``
into the design's numbered-checklist grid (handoff bundle
``schemabrain-v1/project/cli/operator.jsx`` ``Doctor`` block).

Output shape (120 cols):

    ◆ environment · ~/work/acme · my-host · darwin 24.6     8 / 11 healthy
      11 checks  ──────────────────────────────────────────  320 ms

      01  ✓  uvx_invocable          uvx is on PATH
      02  ✓  installed_version      schemabrain 0.3.0
      ...
      06  ⚠  store_entity_count     no entities indexed yet
                                    → run `schemabrain entities apply` ...
      ...

      11 checks · 8 ok · 2 warn · 1 err

Lives in its own module — not inline in ``doctor_flow.py`` — so the
check-logic file stays focused on health-check definitions, mirroring
the ``inspect/render.py`` and ``check/render.py`` boundary established
across the codebase.

Migration footgun: the doctor's outcome vocabulary
(``pass`` / ``warn`` / ``fail``) does NOT match the operator-status
vocabulary ``_ui.status_glyph`` consumes (``ok`` / ``warn`` / ``err``).
``_DOCTOR_STATUS_TO_TIER`` translates at this surface's call site,
following the per-surface translation-map pattern PR #73's wizard
established (``_WIZARD_STATUS_TO_TIER`` in ``cli.py``). The
``_ui._STATUS_TIER`` dict deliberately does NOT carry doctor aliases —
each surface owns its translation visibly in its own diff.
"""

from __future__ import annotations

import platform
import socket
from pathlib import Path
from typing import TYPE_CHECKING, Final

from rich.text import Text

from schemabrain._ui import (
    GLYPH_ARROW,
    GLYPH_BRAND,
    GLYPH_SEP,
    console_render_width,
    status_glyph,
    top_rule,
)
from schemabrain.setup.checks import CheckResult, CheckSummary

# ``rich.text.Text`` is needed at runtime in every helper, so the
# lazy-inside-function-body convention that ``cli.py`` uses for Rich
# imports is not load-bearing here — ``_ui.py`` itself imports Rich
# unconditionally, so any importer of this module already paid the
# cost. Keeping ``Text`` at module top eliminates three redundant
# inner imports without changing the import cost surface.
#
# ``Console`` stays under TYPE_CHECKING because it is only referenced
# in annotations, never instantiated here — the caller hands one in.

if TYPE_CHECKING:
    from rich.console import Console

# Doctor outcomes (``pass`` / ``warn`` / ``fail``) translated onto the
# shared ``_STATUS_TIER`` vocabulary in ``_ui``. The translation lives
# here — not in ``_ui`` — so adding a doctor-specific outcome
# (a ``skip`` tier, say) is one diff in this module rather than a
# vocabulary change rippling through every surface.
_DOCTOR_STATUS_TO_TIER: Final[dict[str, str]] = {
    "pass": "ok",
    "warn": "warn",
    "fail": "err",
}

# Column widths from the design's CSS grid
# (``2ch · 1ch · 18ch · 1fr · auto``). The design's reference mock
# used short display labels (``python``, ``primary keys``,
# ``database url``); Schema Brain check names are snake_case
# identifiers and run up to 24 chars (``source_session_read_only``,
# ``host_config_uses_url_env``). Bumped to 25 so the longest check
# fits without ellipsis truncation — the alignment hook value is
# the column's fixed width, not the exact 18 from the design mock.
_NAME_WIDTH: Final[int] = 25


def render_doctor(
    result: CheckResult,
    *,
    console: Console,
    elapsed_ms: int | None = None,
) -> None:
    """Render ``result`` as the design's numbered checklist.

    ``elapsed_ms`` is the wall-clock duration of the ``doctor()``
    orchestrator call. Optional so callers that don't measure (the
    legacy JSON-mode path, tests that bypass the orchestrator) get
    a clean omission rather than a confusing ``None ms`` cell.
    """
    # Lazy ``Table`` import — only ``render_doctor`` itself uses it,
    # so the helpers don't need it. ``Text`` lives at module top
    # (every helper consumes it at runtime).
    from rich.table import Table

    summary = result.summary
    total = len(result.checks)
    healthy = summary.passed

    console.print(_compose_brand_line(healthy=healthy, total=total))
    console.print(_compose_progress_rule(total=total, elapsed_ms=elapsed_ms, console=console))
    console.print()

    if result.checks:
        grid = Table.grid(padding=(0, 2))
        # Columns: ordinal (right) · glyph · name · detail
        # The ordinal is right-aligned so two-digit positions sit
        # flush with single-digit ones (``01`` vs ``10`` line up).
        grid.add_column(justify="right", style="bright_black", width=2)
        grid.add_column(width=1)
        grid.add_column(style="bold", width=_NAME_WIDTH, no_wrap=True)
        grid.add_column()

        for idx, check in enumerate(result.checks, start=1):
            tier = _DOCTOR_STATUS_TO_TIER.get(check.outcome)
            if tier is None:
                # Surface vocabulary drift loudly: a new outcome added to
                # ``setup.checks.CheckOutcome`` without a corresponding
                # entry here would otherwise silently render as ``✗`` red
                # (visually indistinguishable from a real failure). The
                # raise forces the translation map to stay in sync with
                # the check-outcome ``Literal`` definition.
                raise ValueError(
                    f"Unknown doctor outcome {check.outcome!r}; "
                    "add it to _DOCTOR_STATUS_TO_TIER in doctor_render.py"
                )
            glyph_char, glyph_style = status_glyph(tier)
            ordinal = f"{idx:02d}"
            glyph_cell = Text(glyph_char, style=glyph_style)
            detail = _compose_detail_cell(check.message, check.suggested_next)
            grid.add_row(ordinal, glyph_cell, check.name, detail)

        console.print(grid)
        console.print()

    console.print(_compose_footer_line(total=total, summary=summary))


def _compose_brand_line(*, healthy: int, total: int) -> Text:
    """Build the design's brand line: ``◆ environment · cwd · host · os    N / M healthy``.

    Returns a Rich ``Text`` so the cyan brand glyph + dim chrome can
    co-exist on one line. Right-aligned ``healthy`` ratio reads as
    a quick top-level signal — operators see the count before they
    scan the per-check rows.
    """
    cwd = _short_cwd()
    host = _short_hostname()
    os_label = _short_os_label()

    text = Text()
    text.append(GLYPH_BRAND, style="cyan")
    text.append(" ")
    text.append("environment", style="bold")
    text.append(f" {GLYPH_SEP} ", style="bright_black")
    text.append(cwd, style="dim")
    text.append(f" {GLYPH_SEP} ", style="bright_black")
    text.append(host, style="dim")
    text.append(f" {GLYPH_SEP} ", style="bright_black")
    text.append(os_label, style="dim")
    text.append("    ")
    text.append(f"{healthy} / {total} healthy", style="bright_black")
    return text


def _compose_progress_rule(*, total: int, elapsed_ms: int | None, console: Console) -> Text:
    """Build the section rule: ``  N checks  ─────  <elapsed>``.

    Uses the shared ``_ui.top_rule`` primitive so the visual chrome
    stays in sync with the wizard's progress rule.
    """
    label = f"{total} {'check' if total == 1 else 'checks'}"
    right = f"{elapsed_ms} ms" if elapsed_ms is not None else None
    width = console_render_width(console)
    return top_rule(label, right, width=width)


def _compose_detail_cell(message: str, suggested_next: str | None) -> Text:
    """Build the detail cell for a single check row.

    Layout when ``suggested_next`` is set:

        <message>
        → fix: <suggested_next>

    The fix sub-line uses the design's ``→ fix:`` prefix (handoff
    bundle ``cli/operator.jsx:325`` — ``{SBG.arrow} fix: <cmd>``).
    Without a suggested_next, only the message renders.
    """
    text = Text()
    text.append(message)
    if suggested_next:
        text.append("\n")
        text.append(f"{GLYPH_ARROW} fix: ", style="bright_black")
        text.append(suggested_next, style="cyan")
    return text


def _compose_footer_line(*, total: int, summary: CheckSummary) -> Text:
    """Build the design's footer: ``N checks · A ok · B warn · C err``.

    Mirrors the per-outcome counts the JSON output exposes, so a
    user grepping CI logs for ``"3 fail"`` finds the same count
    they'd see on screen.

    Typed against ``CheckSummary`` (not ``object``) so a rename in
    the summary dataclass — say ``warned`` → ``warning_count`` —
    surfaces statically at mypy/pyright time rather than silently
    rendering ``0 warn`` across every doctor run.
    """
    text = Text(style="bright_black")
    text.append(f"  {total} {'check' if total == 1 else 'checks'}")
    text.append(f" {GLYPH_SEP} {summary.passed} ok")
    text.append(f" {GLYPH_SEP} {summary.warned} warn")
    text.append(f" {GLYPH_SEP} {summary.failed} err")
    return text


def _short_cwd() -> str:
    """Working directory with ``$HOME`` collapsed to ``~``.

    Operators identify projects by the trailing path segment, not by
    the absolute prefix — the home substitution keeps the brand line
    readable without losing identifying detail.
    """
    cwd = Path.cwd()
    home = Path.home()
    try:
        relative = cwd.relative_to(home)
        return f"~/{relative}" if str(relative) != "." else "~"
    except ValueError:
        # cwd is outside home (e.g. /tmp during smoke runs) — show
        # the absolute path rather than a misleading ~ prefix.
        return str(cwd)


def _short_hostname() -> str:
    """Hostname with ``.local`` / domain suffix stripped.

    macOS hostnames frequently carry a ``.local`` mDNS suffix; the
    brand line drops it so the visible host matches what a user
    reads in their shell prompt.
    """
    name = socket.gethostname()
    return name.split(".", 1)[0] if "." in name else name


def _short_os_label() -> str:
    """Compact OS label: ``darwin 24.6`` / ``linux 6.5`` / ``windows 10``.

    Mirrors ``uname -srm`` shape without the architecture (the brand
    line is a quick identity strip, not a full env dump — the JSON
    output carries the full ``platform.platform()`` string for ops
    that grep for arch).
    """
    system = platform.system().lower()
    release = platform.release()
    # Take only the ``major.minor`` of release so the label stays
    # narrow even when a kernel reports a five-segment version.
    parts = release.split(".")
    short_release = ".".join(parts[:2]) if len(parts) >= 2 else release
    return f"{system} {short_release}" if short_release else system


__all__ = [
    "render_doctor",
]
