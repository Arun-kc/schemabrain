"""Schema Brain CLI shell vocabulary — palette tokens, glyph constants,
severity routing, and the single ``rich.console.Console`` factory.

This is the foundation layer for the design-system migration. It does
NOT re-render any surface on its own — it provides the typed primitives
that every renderer should resolve against, so a future palette or
glyph change happens in one place.

Design anchor (handoff bundle ``schemabrain-v1`` · ``cli/shell.css``):

* Terminal-dark visual language, lime ``#c1ff72`` accent, Geist Mono.
* **Glyph-first severity** — colour is decoration, never load-bearing.
  Every severity carrier is a glyph plus colour, so colour-blind users
  and ``NO_COLOR=1`` readers see the same information.
* Five severity tiers map onto a fixed glyph set:
  ``✓ ok · ⚠ warn · ✗ err · ▸ active · ◇ pending``.

Scope discipline for this module's first ship:

* Only the constants + helpers consumed by ``cli_ui.py``,
  ``check/render.py``, and ``inspect/render.py`` are exported. New
  callers add their constants here rather than redefining locally.
* The richer primitives the design specifies (run-signature line, brand
  ``◆`` surface header, top-rule, footer-hint band, grouped help
  formatter) land in follow-up PRs when their consumer surfaces are
  actually re-rendered. Shipping them empty would be speculative.
* ``NO_COLOR`` handling is delegated to ``rich.console.Console``'s
  built-in environment check — ``make_console`` is the single hook
  every surface should resolve through so a future ``--no-color`` CLI
  flag, ``--json`` mode, or palette swap flips one place.

Future migration note: when truecolor support is the floor, the named
Rich colours below can flip to hex literals (``#c1ff72`` etc.). Today
they stay as named tiers so output reads cleanly on basic terminals
and inside CI captures.
"""

from __future__ import annotations

from typing import Final

from rich.console import Console

# ---------------------------------------------------------------------------
# Glyph vocabulary — design bundle ``schemabrain-v1/project/cli/shell.jsx``.
# ---------------------------------------------------------------------------
#
# Severity carriers (consumed in PR #1):
#   ✓ ok      ⚠ warn     ✗ err
#
# Status carriers (consumed in later PRs):
#   ▸ active  ◇ pending  ⊘ skipped
#
# Anchors (consumed in later PRs):
#   ◆ brand   → arrow    · sep    • bullet    ─ rule
#
# The full set lives in a single comment block so future callers can
# scan it in one place. PR #1 exports only the three severity glyphs
# its consumers reference today — when a renderer in a later PR needs
# ``▸`` or ``◆``, it adds the constant here next to its kin rather
# than scattering glyph literals across the codebase.

GLYPH_OK: Final[str] = "✓"
GLYPH_WARN: Final[str] = "⚠"
GLYPH_ERR: Final[str] = "✗"


# ---------------------------------------------------------------------------
# Severity routing — definition-kind → (glyph, rich-style) tuple.
# ---------------------------------------------------------------------------
#
# Entity drift is a hard break (the agent loses access to the whole
# entity when its binding goes away). Metric / join drift degrades a
# single definition without taking the surrounding semantic layer
# offline. The same tiering reads in ``schemabrain check`` and (later)
# in ``schemabrain doctor``, so the mapping lives in one place: extend
# this dict to route a new ``def_kind`` to the right severity tier,
# don't duplicate glyph + colour decisions at call sites.

_DRIFT_TIER: Final[dict[str, tuple[str, str]]] = {
    "entity": (GLYPH_ERR, "red"),
    "metric": (GLYPH_WARN, "yellow"),
    "canonical_join": (GLYPH_WARN, "yellow"),
}


def severity_glyph(def_kind: str) -> tuple[str, str]:
    """Return ``(glyph, rich_style)`` for a drift on ``def_kind``.

    Unknown kinds collapse to the hard-break tier (``✗`` red) rather
    than silently rendering as a benign warning — a new ``def_kind``
    that hasn't been classified yet is a code smell the renderer
    should surface, not hide. Add the new kind to ``_DRIFT_TIER`` to
    route it correctly.
    """
    return _DRIFT_TIER.get(def_kind, (GLYPH_ERR, "red"))


# ---------------------------------------------------------------------------
# PII sensitivity markers — consumed by ``schemabrain inspect`` columns.
# ---------------------------------------------------------------------------
#
# Four tiers carrying both a label and Rich markup. Unknown sensitivity
# strings render verbatim so an indexer change that introduces a new
# tier shows up in output rather than disappearing — the renderer's
# job is to surface what the indexer wrote, not paper over drift.

_PII_MARKERS: Final[dict[str, str]] = {
    "public": "[dim]public[/]",
    "internal": "[yellow]internal[/]",
    "confidential": "[red]confidential[/]",
    "pii": "[red]pii[/]",
}


def pii_marker(sensitivity: str) -> str:
    """Rich-markup-tagged label for a PII sensitivity tier.

    Returns the raw ``sensitivity`` string unchanged when the tier is
    unknown — the renderer composes a verbatim cell rather than
    falling back to a misleading neutral tier. The indexer is the
    source of truth for the sensitivity vocabulary; this map mirrors
    it for presentation only.
    """
    return _PII_MARKERS.get(sensitivity, sensitivity)


# ---------------------------------------------------------------------------
# Console factory — the single entry point every renderer should use.
# ---------------------------------------------------------------------------


def make_console(
    *,
    stderr: bool = False,
    file=None,
    force_terminal: bool | None = None,
    width: int | None = None,
    record: bool = False,
) -> Console:
    """Build a ``rich.console.Console`` configured for Schema Brain.

    Single hook so a future ``--no-color`` flag, ``--json`` quiet
    mode, palette swap, or test-mode override flips one place rather
    than every ``Console(...)`` call site.

    ``NO_COLOR=1`` is honoured automatically — ``rich.console.Console``
    reads the env var on construction. Confirmed via Rich's
    documented `NO_COLOR <https://no-color.org>`_ contract; the
    factory does not need to set ``no_color=True`` manually.

    Args mirror ``rich.console.Console`` for the parameters call sites
    actually pass today. The keyword-only surface keeps the call
    explicit at every site — there's no positional ``Console(True)``
    that quietly toggles ``stderr``.

    ``stderr=True`` directs output to standard error rather than
    standard output. The Schema Brain CLI writes progress, status,
    and guided-error chrome to stderr so stdout stays clean for
    pipe consumers (JSON, SQL, audit rows).

    ``record=True`` enables Rich's in-memory render capture, useful
    in tests that want to assert against the rendered string without
    spinning a real terminal. ``force_terminal`` and ``width`` are
    the standard test-fixture pair: ``force_terminal=True, width=120``
    makes Rich emit the same widget layout into a ``StringIO`` that
    a real 120-col TTY would see.
    """
    return Console(
        stderr=stderr,
        file=file,
        force_terminal=force_terminal,
        width=width,
        record=record,
    )


__all__ = [
    "GLYPH_ERR",
    "GLYPH_OK",
    "GLYPH_WARN",
    "make_console",
    "pii_marker",
    "severity_glyph",
]
