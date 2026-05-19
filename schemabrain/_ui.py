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
* Six status tiers map onto a fixed glyph set:
  ``✓ ok · ⚠ warn · ✗ err · ▸ active · ◇ pending · ⊘ skipped``.

Scope discipline:

* Only the primitives that have at least one consumer (today or in
  the immediate next PR) ship here. Speculative additions get
  deferred until their renderer materialises.
* This module exposes two parallel severity helpers:
  ``drift_glyph(def_kind)`` for ``schemabrain check`` drift rows
  (input is a definition-kind noun: ``entity`` / ``metric`` /
  ``canonical_join``) and ``status_glyph(status_name)`` for the
  general operator-status vocabulary the wizard and ``doctor``
  consume (input is a tier name: ``ok`` / ``warn`` / ``err`` /
  ``active`` / ``pending`` / ``skipped``). The split keeps each
  function's input domain honest — ``drift_glyph`` routes by what
  drifted, ``status_glyph`` routes by how the work went.
* ``NO_COLOR`` handling is delegated to ``rich.console.Console``'s
  built-in environment check — ``make_console`` is the single hook
  every surface should resolve through so a future ``--no-color``
  CLI flag, ``--json`` mode, or palette swap flips one place.

Future migration note: when truecolor support is the floor, the named
Rich colours below can flip to hex literals (``#c1ff72`` etc.). Today
they stay as named tiers so output reads cleanly on basic terminals
and inside CI captures.
"""

from __future__ import annotations

from typing import IO, Final

from rich.console import Console
from rich.text import Text

# ---------------------------------------------------------------------------
# Glyph vocabulary — design bundle ``schemabrain-v1/project/cli/shell.jsx``.
# ---------------------------------------------------------------------------
#
# Severity carriers:
#   ✓ ok       ⚠ warn      ✗ err
#
# Status carriers:
#   ▸ active   ◇ pending   ⊘ skipped
#
# Anchors:
#   ◆ brand    → arrow     · sep     • bullet
#
# Every glyph the design's surfaces consume has a named constant here,
# so a future renderer never reaches for a character literal. Constants
# without a current consumer document the design vocabulary for
# follow-up PRs (wizard stage rows, doctor headers, error renderers).

GLYPH_OK: Final[str] = "✓"
GLYPH_WARN: Final[str] = "⚠"
GLYPH_ERR: Final[str] = "✗"
GLYPH_ACTIVE: Final[str] = "▸"
GLYPH_PENDING: Final[str] = "◇"
GLYPH_SKIPPED: Final[str] = "⊘"
GLYPH_BRAND: Final[str] = "◆"
GLYPH_ARROW: Final[str] = "→"
GLYPH_BULLET: Final[str] = "•"
GLYPH_SEP: Final[str] = "·"
GLYPH_RULE: Final[str] = "─"


# ---------------------------------------------------------------------------
# Drift severity routing — definition-kind → (glyph, rich-style) tuple.
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


def drift_glyph(def_kind: str) -> tuple[str, str]:
    """Return ``(glyph, rich_style)`` for a drift on ``def_kind``.

    Input is a definition-kind noun (``entity`` / ``metric`` /
    ``canonical_join``) — not a tier name. Use ``status_glyph`` for
    the general ``ok`` / ``warn`` / ``err`` / ``active`` /
    ``pending`` / ``skipped`` operator-status vocabulary.

    Unknown kinds collapse to the hard-break tier (``✗`` red) rather
    than silently rendering as a benign warning — a new ``def_kind``
    that hasn't been classified yet is a code smell the renderer
    should surface, not hide. Add the new kind to ``_DRIFT_TIER`` to
    route it correctly.
    """
    return _DRIFT_TIER.get(def_kind, (GLYPH_ERR, "red"))


# ---------------------------------------------------------------------------
# Status routing — operator-status tier name → (glyph, rich-style).
# ---------------------------------------------------------------------------
#
# Six tiers — three severity + three lifecycle — the wizard's per-
# stage outcomes, ``doctor``'s per-check outcomes, and ``tail``'s
# per-event severity all map onto this vocabulary. The wizard
# already migrated onto this helper in PR #3 (collapsing the local
# ``cli._STAGE_GLYPHS`` dict and flipping the previous skipped
# glyph ``↷`` to the design-spec ``⊘``). Renderers still keeping
# local glyph dicts (``setup/doctor_flow._GLYPHS``) collapse onto
# ``status_glyph`` when their surface is migrated — the visible
# glyph flip lands with the surface re-render, not in this
# primitive's commit.
#
# **Migration footgun — read this before threading a legacy dict
# through ``status_glyph``.** The local dicts use different tier
# vocabulary than ``_STATUS_TIER`` keys:
#
#   wizard ``_STAGE_GLYPHS``:     ``done`` / ``skipped`` / ``failed``
#   doctor ``_GLYPHS``:           ``pass`` / ``warn``    / ``fail``
#   _STATUS_TIER (this dict):     ``ok``   / ``warn``    / ``err``  / ``active`` / ``pending`` / ``skipped``
#
# Only ``warn`` and ``skipped`` overlap by name. A naïve replacement
# of ``_STAGE_GLYPHS.get(outcome.status, outcome.status)`` with
# ``status_glyph(outcome.status)`` would silently route ``done`` and
# ``failed`` through the unknown-tier fallback ``(✗, red)`` — a
# hard-error visual on a successful wizard run. Migration MUST
# translate at the call site: ``done → ok``, ``failed → err``,
# ``pass → ok``, ``fail → err``. The translation belongs in the
# surface re-render commit so it's auditable in the diff, not
# hidden as an alias in this dict.

_STATUS_TIER: Final[dict[str, tuple[str, str]]] = {
    "ok": (GLYPH_OK, "green"),
    "warn": (GLYPH_WARN, "yellow"),
    "err": (GLYPH_ERR, "red"),
    # `active` uses cyan rather than the lime the design specifies —
    # Rich's named-colour palette doesn't have lime, and `green`
    # collides with `ok` so an in-progress row reads identical to a
    # completed one (only the glyph `▸` vs `✓` differentiates).
    # Cyan keeps the in-progress / done distinction visible until
    # the truecolor migration provides lime (`#c1ff72` per design).
    "active": (GLYPH_ACTIVE, "cyan"),
    "pending": (GLYPH_PENDING, "bright_black"),
    "skipped": (GLYPH_SKIPPED, "yellow"),
}


def status_glyph(status_name: str) -> tuple[str, str]:
    """Return ``(glyph, rich_style)`` for an operator-status tier.

    Input is a tier name (``ok`` / ``warn`` / ``err`` / ``active``
    / ``pending`` / ``skipped``) — not a domain noun. Use
    ``drift_glyph`` for ``schemabrain check`` drift rows where the
    input is a ``def_kind``.

    Unknown tier names collapse to the hard-break tier (``✗`` red)
    rather than silently rendering as a benign warning, matching
    ``drift_glyph``'s contract. A renderer reaching for a tier that
    isn't routed yet should surface visibly. Extend ``_STATUS_TIER``
    to add a new tier rather than hand-rolling a glyph at the call
    site.
    """
    return _STATUS_TIER.get(status_name, (GLYPH_ERR, "red"))


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
    file: IO[str] | None = None,
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


# ---------------------------------------------------------------------------
# Top-rule text builder — design's section separator.
# ---------------------------------------------------------------------------
#
# The design's ``TopRule`` (handoff bundle ``cli/shell.jsx`` line 76)
# is a left-aligned label followed by a dashed run and an optional
# right-aligned metadata string. Used above the wizard's stage list
# and (in follow-up PRs) above the doctor's check list.
#
# Shape (120 cols, label="progress", right="2 / 7 done · ~38s remaining"):
#
#   ┌─ 2 cells gap
#   │       ┌─ label (dim)
#   │       │              ┌─ dashed run (dim)
#   │       │              │                                       ┌─ right meta (dim)
#   ▾       ▾              ▾                                       ▾
#     progress  ──────────────────────────────  2 / 7 done · ~38s remaining
#
# ``width`` is the total cell width the line should occupy.
# ``label`` and ``right`` are rendered in the same dim style; the
# dashed run uses the same style so the whole line reads as one
# muted band. A renderer wanting bolder hierarchy should compose
# the rule out of a plain ``Text`` rather than calling this helper.

_TOP_RULE_GAP: Final[int] = 2
_TOP_RULE_MIN_DASHES: Final[int] = 4


def top_rule(
    label: str,
    right: str | None = None,
    *,
    width: int = 120,
    style: str = "bright_black",
) -> Text:
    """Build a Rich ``Text`` for the design's top-rule shape.

    Returns a renderable ``Text`` instance — call sites print it via
    ``console.print(top_rule(...))``. Returning ``Text`` (not a
    pre-rendered string) lets the caller compose it with other Rich
    primitives if needed; printing it directly is the common path.

    Args mirror the design's section-header conventions:

    * ``label`` — the left-aligned tag (``"progress"``,
      ``"7 stages"``, etc.). Rendered in ``style``.
    * ``right`` — optional right-aligned metadata
      (``"2 / 7 done · ~38s remaining"``, ``"21.0s · $0.075"``).
      ``None`` renders just the label + dashes.
    * ``width`` — total cell width. Defaults to 120 (matches the
      design's reference width). Callers reading
      ``console.width`` should pass it explicitly.
    * ``style`` — Rich style applied to every component. The design
      uses a single muted tone for the whole line so the rule sinks
      into chrome rather than competing with the stage list below.

    When ``label`` + ``right`` are too wide to fit, the dashed run
    collapses to ``_TOP_RULE_MIN_DASHES`` (4 dashes) — the rule
    stays visible but no longer fills the width. Operators on
    narrow terminals see a short rule rather than a wrapped one.
    """
    gap = " " * _TOP_RULE_GAP
    right_with_gap = (gap + right) if right else ""

    label_segment_len = _TOP_RULE_GAP + len(label) + _TOP_RULE_GAP
    right_segment_len = len(right_with_gap)
    dash_count = max(_TOP_RULE_MIN_DASHES, width - label_segment_len - right_segment_len)

    text = Text(style=style)
    text.append(gap + label + gap)
    text.append(GLYPH_RULE * dash_count)
    if right:
        text.append(right_with_gap)
    return text


# ---------------------------------------------------------------------------
# Shared width helper — soft-cap console width at the design reference.
# ---------------------------------------------------------------------------
#
# Every design surface (wizard hero, doctor checklist,
# ``init --help`` grouped formatter, future renderers) needs the
# same "use the detected terminal width but cap at the design's
# 120-col reference" calculation. The defensive ``getattr``
# fallback handles a Console stub without ``.width`` so a test
# fixture that hands in a mock object doesn't crash the
# renderer — Rich's real ``Console`` always has the attribute,
# so production paths never trigger the fallback.

_DEFAULT_DESIGN_WIDTH: Final[int] = 120


def short_path(path: str | None) -> str:
    """Collapse ``$HOME`` to ``~/`` in a filesystem path for display.

    Returns the input unchanged when:

    * ``path`` is ``None`` → returns the empty string (caller can
      still concatenate without a guard);
    * ``path`` is outside the user's home directory → renders
      verbatim (a ``/tmp/...`` smoke path stays readable);
    * ``path`` IS the home directory → renders as ``~``;
    * ``Path.home()`` raises (rare — sandboxed env without a
      resolvable home) → falls through to the raw path.

    Used across the design's surfaces (doctor brand line, inspect
    store brand line, dry-run cost-estimate panel ``store`` row)
    so terminal recordings + CI logs + support screenshots never
    surface the operator's OS username on the visible path —
    consistent with what every other path-display surface in the
    codebase already does.
    """
    if path is None:
        return ""
    from pathlib import Path

    try:
        home = Path.home()
    except (OSError, RuntimeError):
        return path
    p = Path(path)
    try:
        relative = p.relative_to(home)
    except ValueError:
        return path
    if str(relative) == ".":
        return "~"
    return f"~/{relative}"


def console_render_width(console: Console, *, cap: int = _DEFAULT_DESIGN_WIDTH) -> int:
    """Return ``min(console.width, cap)`` with a defensive fallback.

    The ``cap`` defaults to ``120`` (the design's reference
    width). Callers wanting a tighter or wider soft-cap pass
    ``cap=`` explicitly. The fallback to ``cap`` when
    ``.width`` is missing keeps test stubs honest without
    crashing the renderer.
    """
    detected = getattr(console, "width", cap)
    return min(detected, cap)


__all__ = [
    "GLYPH_ACTIVE",
    "GLYPH_ARROW",
    "GLYPH_BRAND",
    "GLYPH_BULLET",
    "GLYPH_ERR",
    "GLYPH_OK",
    "GLYPH_PENDING",
    "GLYPH_RULE",
    "GLYPH_SEP",
    "GLYPH_SKIPPED",
    "GLYPH_WARN",
    "console_render_width",
    "drift_glyph",
    "make_console",
    "pii_marker",
    "short_path",
    "status_glyph",
    "top_rule",
]
