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

import contextlib
import shutil
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import IO, Final, Protocol

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


# ---------------------------------------------------------------------------
# Active-spinner registry — pause Rich Status during interactive prompts.
# ---------------------------------------------------------------------------
#
# Smoke 2026-05-19 surfaced a UX bug: when the wizard reached stage 3
# (Curate entities), Rich's ``console.status(...)`` was active in the
# background while ``input()`` waited on the user's Enter. The spinner
# kept rendering on the same line as the prompt, so the user read it as
# "stage is already running" rather than "waiting on your confirmation".
#
# Pausing the spinner around ``input()`` is the right fix, but the
# ``_prompt_llm_confirmation`` callsite in ``setup.wizard`` doesn't
# import ``cli`` (would be a circular import). The shared primitive
# below lets the CLI's ``_wizard_stage_context`` register the active
# spinner, and the wizard's prompt code pause it for the duration of
# the input — both via this module they already import.
#
# Thread-local because the harness tests parallelise wizard runs and
# leaking the spinner across threads would cause sporadic
# AttributeError on ``.stop()`` from a finalised Status. The contract
# is "one wizard spinner per thread"; nesting is not supported (would
# need a stack, no consumer needs it today).


class _PausableSpinner(Protocol):
    """Subset of ``rich.status.Status`` we depend on.

    Declared explicitly so the registry doesn't pull in Rich at
    type-check time and so test stubs can satisfy the contract with
    two zero-arg methods instead of constructing a real ``Status``.
    """

    def start(self) -> None: ...
    def stop(self) -> None: ...


_active_spinner: threading.local = threading.local()


@contextlib.contextmanager
def register_active_spinner(status: _PausableSpinner) -> Iterator[None]:
    """Mark ``status`` as the currently-active wizard spinner for this thread.

    Pair with ``pause_active_spinner`` from interactive prompt code so
    the spinner stops spinning while the prompt blocks on ``input()``.
    The status object must support ``.stop()`` and ``.start()`` —
    Rich's ``Status`` matches this protocol natively.

    Nested registrations are NOT supported — the design only has one
    spinner per wizard stage. The function still snapshots the prior
    registration so a defensive caller composing this with another
    context doesn't permanently null out the registry on exit.
    """
    prev = getattr(_active_spinner, "status", None)
    _active_spinner.status = status
    try:
        yield
    finally:
        _active_spinner.status = prev


@contextlib.contextmanager
def pause_active_spinner() -> Iterator[None]:
    """If a spinner is registered for this thread, pause it for the with-block.

    No-op when no spinner is registered (every non-interactive call
    site, every non-spinner wizard stage, every test that doesn't
    register one). Used by the wizard's LLM confirmation prompt so
    the spinner doesn't visually suggest "already running" while we
    wait on the user's Enter.

    Failures in ``.stop()`` / ``.start()`` fall through to a plain
    yield rather than raising — pausing the spinner is a UX
    affordance, not a correctness requirement. A Rich-implementation
    quirk that breaks ``.stop()`` shouldn't take the prompt down
    with it.
    """
    status = getattr(_active_spinner, "status", None)
    if status is None:
        yield
        return
    try:
        status.stop()
    except Exception:  # pragma: no cover — defensive against Rich impl change
        yield
        return
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            # Defensive against Rich impl change — pausing the spinner
            # is a UX nicety, not correctness; a broken `.start()`
            # after the prompt must not raise back into the wizard.
            status.start()


# ---------------------------------------------------------------------------
# Interactive prompt primitives — day-one UX overhaul.
# ---------------------------------------------------------------------------
#
# Used by the wizard's new stage 0 (fork + setup prompts) and by 5
# post-init commands (`index`, `check`, `entities|metrics|joins
# suggest`) that today fail with a `GuidedError` when their env vars
# are missing. The prompts let those callers fall back to an
# interactive ask instead of dead-ending on a CI-shaped error.
#
# Both prompts are pure UI: they assume the caller has already gated
# on `_stderr_is_interactive_tty()`. They never decide policy (whether
# to abort vs degrade on empty input) — the caller routes the return
# value. Empty input returns `None`; the caller distinguishes
# "skipped" from "provided" by checking for None.
#
# `pause_active_spinner` wraps every `Prompt.ask` so the wizard
# spinner doesn't bleed into the prompt line — same primitive
# `_prompt_llm_confirmation` already uses (`wizard.py:392`).
#
# Cancellation: `KeyboardInterrupt` propagates verbatim. The caller
# (the wizard stage handler, the CLI subcommand) catches it and
# translates to the right outcome — abort, skip, exit non-zero —
# because that decision is policy, not UI.


def prompt_for_url(
    console: Console,
    *,
    purpose: str,
) -> str | None:
    """Interactively ask for a Postgres connection URL.

    Used by the wizard's stage 0 (own-DB path) and by post-init
    commands that need a URL but were invoked without one in env.
    The caller MUST gate on `_stderr_is_interactive_tty()` before
    calling — this helper does not check TTY itself, so a CI run
    that wires this in by mistake would block on stdin forever.

    Returns the raw URL string the user typed, or ``None`` if they
    pressed Enter without typing anything (which the caller can
    route to "abort with guided error" or to "fall back to a
    default" depending on context). ``KeyboardInterrupt`` propagates
    so Ctrl-C aborts cleanly.

    The prompt uses ``password=True`` so the URL (which may embed a
    password) is not echoed to the terminal — consistent with the
    `_redact_env_args` precedent in `cli.py` and the README's
    "credentials never in argv" posture.

    The ``purpose`` string is shown in the prompt's preamble so the
    user sees WHY the wizard is asking ("to index your database",
    "to check for schema drift"). Keep it short — one verb phrase.
    """
    from rich.prompt import Prompt

    with pause_active_spinner():
        console.print(
            f"  [bright_black]{GLYPH_PENDING} Paste your Postgres URL "
            f"{purpose}. We'll normalize the driver suffix for you.[/]"
        )
        console.print("    [bright_black]Example: postgresql://user:pass@host:5432/dbname[/]")
        answer = Prompt.ask(
            "  [cyan]DATABASE_URL[/]",
            password=True,
            console=console,
            default="",
            show_default=False,
        )
    stripped = (answer or "").strip()
    return stripped or None


def prompt_for_anthropic_key(
    console: Console,
    *,
    purpose: str,
    cost_estimate_usd: float,
    cap_usd: float,
    skip_hint: str = "press Enter to skip",
) -> str | None:
    """Interactively ask for an Anthropic API key with cost disclosure.

    Used by the wizard's stage 0 (cost-disclosure + skip-friendly
    path) and by post-init `entities|metrics|joins suggest`
    commands. The caller MUST gate on TTY before calling.

    Renders a four-line preamble before the prompt:

        ◆ Schema Brain uses Claude to {purpose}.

          ◇ Cost:  ~${cost} (capped at ${cap})
          ◇ Time:  ~30s
          ◇ {skip_hint}

    Cost numbers come from the caller because the per-stage estimate
    varies (entities ~$0.01, metrics ~$0.02, full enrichment more).
    The cap is the env-var override (`SCHEMABRAIN_*_CAP_USD`) so the
    user sees the actual ceiling they'd hit, not a hardcoded display
    value.

    The ``skip_hint`` defaults to "press Enter to skip" — appropriate
    for the wizard, where empty key = degraded mode. Post-init
    commands that require the key pass ``skip_hint="press Ctrl-C
    to abort"`` so the user understands Enter won't help them.
    Returns ``None`` for empty input regardless; the caller routes
    based on context.

    Returns the raw key string, or ``None`` if the user pressed
    Enter without typing anything. ``KeyboardInterrupt`` propagates.
    """
    from rich.prompt import Prompt

    with pause_active_spinner():
        console.print(f"  [bold]{GLYPH_BRAND} Schema Brain uses Claude to {purpose}.[/]")
        console.print()
        console.print(
            f"    [bright_black]{GLYPH_PENDING} Cost:  "
            f"~${cost_estimate_usd:.2f} (capped at ${cap_usd:.2f})[/]"
        )
        console.print(f"    [bright_black]{GLYPH_PENDING} Time:  ~30s[/]")
        console.print(f"    [bright_black]{GLYPH_PENDING} {skip_hint}[/]")
        console.print()
        answer = Prompt.ask(
            "  [cyan]Paste ANTHROPIC_API_KEY[/]",
            password=True,
            console=console,
            default="",
            show_default=False,
        )
    stripped = (answer or "").strip()
    return stripped or None


def offer_persist_anthropic_key_to_env_file(
    console: Console,
    *,
    key_value: str,
    env_path: Path,
    gitignore_path: Path,
) -> bool:
    """Ask whether to save a freshly-pasted API key to ``.env`` and persist on yes.

    Called by ``_resolve_anthropic_key_source`` immediately after
    ``prompt_for_anthropic_key`` returns a non-empty key, so the
    operator who just pasted a key once doesn't have to paste it
    again every time they re-run the wizard or a suggest command.

    Three contracts (D4):

    1. **Opt-in default no.** The Confirm prompt's default is False —
       Enter without typing accepts the safe path (don't persist).
       The key never lands on disk without an explicit ``y``.
    2. **Never write silently.** Both the prompt itself AND the
       written-confirmation message print to the console. If the
       prompt would not be visible (non-TTY), the helper returns
       False without writing anything.
    3. **Gitignore warning.** When ``env_path`` is not listed in
       ``gitignore_path``, prints a one-line yellow warning above
       the prompt so the operator sees the leak risk BEFORE saying
       yes. Does not block — the operator decides.

    Returns True iff the key was actually persisted to disk. The
    caller does not need to act on the return; the helper handles
    all user-facing messaging (success line, warning, decline path).

    Template seeding (D4-fix): when ``env_path`` does NOT yet exist
    AND a sibling ``.env.example`` template is present, the helper
    copies the template to ``env_path`` BEFORE writing the key. This
    preserves the template's comments + placeholder rows for other
    keys (``DATABASE_URL=``, etc) so the new ``.env`` looks like a
    proper project config dump, not just a one-key file. The user's
    next step is filling in the other placeholders — they can see
    what to fill in instead of having to dig up the template.
    """
    from rich.prompt import Confirm

    from schemabrain.setup.env_file import (
        is_path_in_gitignore,
        persist_key_to_env_file,
    )

    with pause_active_spinner():
        if not is_path_in_gitignore(target=env_path, gitignore=gitignore_path):
            # Yellow warning so the operator can't miss the leak risk
            # before saying yes. We deliberately do NOT auto-add the
            # entry to .gitignore — that's the operator's repo to
            # manage; we don't want to mutate their git state during
            # a key-paste flow. Round-2 fold MED (silent-failure-hunter):
            # branch the wording on whether `.gitignore` exists at all.
            # The "not listed in .gitignore" copy is literally wrong
            # for a fresh repo with no `.gitignore` and reads as
            # alarmist; the "no .gitignore found" wording is honest
            # and actionable.
            if gitignore_path.exists():
                warning_body = (
                    f"{env_path.name} is NOT listed in {gitignore_path.name} — "
                    f"saving here will commit the key if you `git add` this file."
                )
            else:
                warning_body = (
                    f"no {gitignore_path.name} found — consider adding "
                    f"{env_path.name} to {gitignore_path.name} before saving "
                    f"so the key doesn't get committed."
                )
            console.print(f"  [yellow]{GLYPH_WARN} {warning_body}[/]")
        consent = Confirm.ask(
            f"  [cyan]Save ANTHROPIC_API_KEY to {env_path.name} for next time?[/]",
            default=False,
            console=console,
        )
        if not consent:
            return False
        # D4-fix: seed from .env.example when creating a fresh .env
        # so the new file inherits the template's comments + the
        # placeholder rows for other keys. Pure file-copy — the
        # subsequent persist_key_to_env_file call then in-place
        # replaces the empty `ANTHROPIC_API_KEY=` line.
        template_path = env_path.parent / (env_path.name + ".example")
        seeded_from_template = False
        if not env_path.exists() and template_path.exists():
            # Round-2 fold CRITICAL (python-reviewer): `shutil.copy`
            # preserves the source mode. `.env.example` is typically
            # `0o644` (committed to the repo, world-readable). Without
            # the explicit `chmod` below, the freshly-seeded `.env`
            # inherits `0o644`, and the subsequent
            # `persist_key_to_env_file` reads that mode back in and
            # preserves it — the API key lands group/world-readable.
            # Round-2 fold MED (silent-failure-hunter): if the copy
            # fails mid-write (quota exhausted, NFS timeout, ENOSPC),
            # clean up the partial file so the next run's loader
            # doesn't pick up garbage bytes.
            try:
                shutil.copy(template_path, env_path)
                env_path.chmod(0o600)
            except OSError:
                env_path.unlink(missing_ok=True)
                raise
            seeded_from_template = True
        persist_key_to_env_file(
            key_name="ANTHROPIC_API_KEY",
            key_value=key_value,
            env_path=env_path,
        )
        console.print(
            f"  [bright_black]{GLYPH_OK} saved to {env_path} — next run "
            f"loads it automatically; never overrides an explicit export.[/]"
        )
        if seeded_from_template:
            console.print(
                f"  [bright_black]{GLYPH_PENDING} seeded from "
                f"{template_path.name}; fill in the other placeholders "
                f"as you need them.[/]"
            )
    return True


# ---------------------------------------------------------------------------
# LLM cost-preview line — day-one UX overhaul "what's happening" sub-step.
# ---------------------------------------------------------------------------


def stderr_is_interactive_tty() -> bool:
    """True iff init/wizard can safely prompt — both stdin AND stderr are TTYs.

    Single source of truth shared by ``cli`` and ``wizard`` so tests
    can monkeypatch one function and reach both code paths. The CLI
    keeps its private ``_stderr_is_interactive_tty`` shim for
    backwards compatibility with existing tests that monkeypatch it
    directly.
    """
    return sys.stdin.isatty() and sys.stderr.isatty()


def prompt_yes_no(
    console: Console,
    question: str,
    *,
    default: bool,
) -> bool:
    """Ask a yes/no question via ``rich.prompt.Confirm``, pausing any spinner.

    Caller MUST gate on ``_stderr_is_interactive_tty()`` before
    calling — this helper does not check TTY itself, so a non-
    interactive run would block on stdin forever.

    Wrapped in ``pause_active_spinner()`` so when called from inside
    a wizard stage that has registered a Status spinner, the spinner
    stops while the prompt blocks on stdin. Without the pause, the
    spinner overwrites the prompt's response line as the user
    types ([y/N]:).

    Lifted out of ``cli._prompt_yes_no`` (F3) so the wizard's
    stage-6 inline overwrite prompt can use the same primitive
    without importing from ``cli``. ``cli._prompt_yes_no`` remains
    as a thin shim for the residual CLI callsites that don't have
    a Console handy.
    """
    from rich.prompt import Confirm

    with pause_active_spinner():
        return Confirm.ask(question, default=default, console=console)


def print_llm_stage_preamble(
    console: Console,
    *,
    action: str,
    model: str,
    cost_estimate_usd: float,
    cap_usd: float,
) -> None:
    """Print a one-line cost preview before an LLM call.

    Used inside the wizard's entity / metric suggestion stages so the
    user sees what's about to be spent BEFORE the LLM call goes out
    (~30s of waiting otherwise reads as "stuck"). Pairs with the
    ``prompt_for_anthropic_key`` cost disclosure — same numbers, same
    framing, so the user who pasted a key 30 seconds ago has the
    bounds re-stated before the spend.

    Wrapped in ``pause_active_spinner()`` because Rich's spinner
    interleaving only fires when the print and the Status share the
    same Console object. Callers (wizard stage 3/4 handlers) build
    a fresh ``make_console(stderr=True)``, which is a DIFFERENT
    Console than the one ``_wizard_stage_context`` registered as
    the active spinner. Without the explicit pause, the spinner
    overwrites this line on its next animation frame and the
    operator never reads the cost preview — undermining the whole
    point of the line. The spinner-pause registry uses a thread-
    local handle, not a Console reference, so it pauses regardless
    of which Console the spinner is attached to. Round-2 reviewer
    fold (silent-failure-hunter MEDIUM-1).

    ``cost_estimate_usd`` is the per-call estimate (typically
    $0.01 entities, $0.02 metrics). ``cap_usd`` is the actual
    ceiling that will trip the ``CostCeilingGuard``, threaded down
    from the wizard's ``max_cost_usd`` config — operator sees the
    real ceiling, not a hardcoded display value.

    Future PR-2 work: replace this static line with a Rich ``Live``
    display showing elapsed time + ticking spinner. That refactor
    needs ``_wizard_stage_context`` to coexist cleanly with an
    inner ``Live``, which is too much surface for PR-1. The static
    line ships the bulk of the UX benefit (cost transparency
    before the call) without the refactor.
    """
    with pause_active_spinner():
        console.print(
            f"  [bright_black]{GLYPH_PENDING} Asking Claude to {action} · "
            f"~${cost_estimate_usd:.2f} · capped at ${cap_usd:.2f} · {model}[/]"
        )


@contextlib.contextmanager
def live_llm_stage_progress(
    console: Console,
    *,
    action: str,
    model: str,
    cost_estimate_usd: float,
    cap_usd: float,
) -> Iterator[None]:
    """Show a Rich ``Live`` cost preview + auto-updating elapsed timer.

    D1 (post-PR-#79 polish): upgrades the static one-line preamble
    that ``print_llm_stage_preamble`` ships to a live two-line
    display that re-renders the elapsed seconds twice a second so
    the operator can see the ~20-30s LLM round-trip is still in
    flight (not hung).

    Display shape (TTY path):

    ::

        ◇ Asking Claude to identify business entities (7 tables) ·
            ~$0.01 · capped at $1.00 · claude-sonnet-4-6
          elapsed 12s

    Non-TTY (CI logs, redirected stderr): falls back to the static
    ``print_llm_stage_preamble`` line — Live frames would just be
    carriage-return noise in a log file. Pre-checked so callers
    don't need to gate on ``console.is_terminal``.

    Composes with ``register_active_spinner`` / ``pause_active_spinner``
    correctly: the outer wizard stage spinner (registered by
    ``_wizard_stage_context``) is paused via ``pause_active_spinner()``
    BEFORE the Live starts (Rich rejects nested live displays). The
    Live is not itself registered as the active spinner because the
    LLM round-trip is non-interactive — no prompt fires during the
    body that would need to pause the Live mid-call.

    Critical exception-flow contract: the ``try`` around
    ``Live.__enter__`` is narrow (defensive against Rich init
    failures only). The ``yield`` is NOT wrapped — F1 had a bug
    where a wide ``try``/``except Exception`` caught body-raised
    exceptions via the yield re-raise and double-yielded into
    contextlib's "generator didn't stop after throw()" guard.
    Keep the yield outside any general except clause.
    """
    preamble_line = (
        f"  [bright_black]{GLYPH_PENDING} Asking Claude to {action} · "
        f"~${cost_estimate_usd:.2f} · capped at ${cap_usd:.2f} · {model}[/]"
    )

    if not console.is_terminal:
        # Non-TTY: static preamble only. The operator (or scraper)
        # gets one durable line in the log; no Live frames.
        with pause_active_spinner():
            console.print(preamble_line)
        yield
        return

    # Lazy import: ``rich.live`` is heavier than ``rich.console``
    # and only the TTY path needs it. Importing at module top would
    # tax non-interactive runs that never reach this helper.
    from rich.live import Live

    started = time.monotonic()

    class _PreambleWithTimer:
        """Renderable that re-computes elapsed time on every Live refresh."""

        def __rich__(self) -> str:
            elapsed = int(time.monotonic() - started)
            # Two-line render: preamble unchanged, timer ticks.
            # The dim styling keeps the timer visually subordinate to
            # the preamble (which carries the load-bearing facts:
            # cost, cap, model).
            return f"{preamble_line}\n  [dim]  elapsed {elapsed}s[/]"

    # Pause any outer spinner BEFORE Live starts — Rich rejects
    # nested live displays with a `LiveError`. The pause is a no-op
    # when no spinner is registered (CLI standalone path).
    with pause_active_spinner():
        try:
            live = Live(
                _PreambleWithTimer(),
                console=console,
                refresh_per_second=2,
                transient=False,
            )
        except Exception:  # pragma: no cover — defensive against Rich init failures
            # Fall back to the static preamble — better than crashing
            # the wizard for a UX nicety.
            console.print(preamble_line)
            yield
            return
        with live:
            yield


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
    "live_llm_stage_progress",
    "make_console",
    "offer_persist_anthropic_key_to_env_file",
    "pause_active_spinner",
    "pii_marker",
    "print_llm_stage_preamble",
    "prompt_for_anthropic_key",
    "prompt_for_url",
    "prompt_yes_no",
    "register_active_spinner",
    "short_path",
    "status_glyph",
    "stderr_is_interactive_tty",
    "top_rule",
]
