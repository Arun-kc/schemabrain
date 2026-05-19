"""Design-bundle error surface renderers.

Composes two of the three error shapes specified by the handoff
bundle (``schemabrain-v1/project/cli/errors.jsx``):

* **A — bad input** (``render_bad_argument_error``): a guided
  error pointing at the failing token in the user's command line
  with a caret pointer + remediation suggestions. The design's
  reference case is ``schemabrain index --since wednesday``.

* **B — missing secret** (``render_missing_secret_error``): the
  ``--url-env`` empty / unset path lifted out of the plain four-
  line ``error / why / fix / next`` block into the design's
  three-panel block. Recommends the safe ``--url-env`` form,
  documents why the password never reaches argv, and offers
  shell-level diagnostics if the env var should already be set.

The third design shape (``ErrLLMFailure``, the 529 advisory)
is deferred to a follow-up PR — it requires new exception-
catching plumbing inside the wizard / ``entities suggest`` flow
beyond a visual upgrade to an existing render call.

Module shape mirrors ``setup/doctor_render.py`` from PR #4:

* ``errors.py`` stays the pure DTO + translator module.
* ``errors_render.py`` (this file) is the visual chrome — pure
  layout primitives composed onto a caller-supplied ``Console``.

Lives at package root rather than under ``cli/``  so the error
shape is reachable from any subcommand without importing through
``cli.py`` (which would cycle imports during error rendering).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Literal

from rich.text import Text

from schemabrain._ui import (
    GLYPH_ARROW,
    GLYPH_BRAND,
    GLYPH_ERR,
    GLYPH_OK,
    GLYPH_SEP,
)

# ``Text`` is needed at runtime by every helper. ``_ui.py`` already
# imports Rich unconditionally, so any importer of this module has
# already paid the cost — keeping ``Text`` at module top removes
# four redundant lazy imports without changing the import surface
# (matches ``setup/doctor_render.py`` after PR #4's round-2 fold).
#
# ``Console`` stays under TYPE_CHECKING — we accept one from the
# caller, never instantiate one here.

if TYPE_CHECKING:
    from rich.console import Console

_MissingSecretState = Literal["unset", "empty"]


def render_bad_argument_error(
    *,
    arg_name: str,
    raw_value: str,
    reason: str,
    expected_summary: str,
    suggestions: Sequence[tuple[str, str]],
    command_prefix: str,
    console: Console,
) -> None:
    """Render the design's "bad input" error surface (shape A).

    Lays out:

    * a brand line ``◆ error · bad value for <arg_name>``
    * an invalid-argument block reproducing the command and a
      ``^^^`` caret pointer under the failing token
    * a "did you mean" sub-block listing corrected commands with
      one-line descriptions

    Parameters
    ----------
    arg_name
        The flag the user passed (e.g. ``--since``). Used in the
        title and in the caret reproduction.
    raw_value
        The actual bad value the user passed (e.g. ``wednesday``).
        Underlined with ``^`` characters of the same visual width.
    reason
        Short summary explaining why the value is wrong, shown
        directly under the caret (e.g.
        ``"not a duration · not a date"``). When the caller is
        translating from a wrapped exception, prefer a short
        compound phrase over the raw exception message so the
        caret leader stays on one line.
    expected_summary
        One-line explanation of accepted shapes (e.g.
        ``"a duration like 14d or an ISO date like 2026-05-01"``).
    suggestions
        Sequence of ``(command, description)`` pairs to render in
        the "did you mean" block. ``command`` should be a complete
        shell command line; ``description`` is the dim trailer.
        Empty sequence skips the "did you mean" block entirely.
    command_prefix
        The portion of the command line preceding ``arg_name``
        (e.g. ``"schemabrain index"``). The caret underline
        positioning depends on the exact prefix the user saw, so
        the caller passes it rather than have the renderer guess.
    console
        Rich console to write to (typically stderr).
    """
    _print_error_brand_line(f"bad value for {arg_name}", console=console)
    console.print()

    # Invalid-argument summary line.
    summary = Text()
    summary.append(f"  {GLYPH_ERR} ", style="red")
    summary.append("invalid argument ", style="bold")
    summary.append(arg_name, style="bright_black")
    summary.append(" ")
    summary.append(raw_value, style="red")
    console.print(summary)

    # One-line "got X, expected Y" sentence.
    explainer = Text()
    explainer.append("    got ")
    explainer.append(f'"{raw_value}"', style="red")
    explainer.append(", expected ")
    explainer.append(expected_summary, style="cyan")
    explainer.append(".")
    console.print(explainer)
    console.print()

    # Caret block — reproduces the command + underlines the bad token.
    # Caret offset = length of everything before raw_value in the
    # rendered line (including the leading 4-space indent).
    # Cell width — see _visible_width for CJK caveat.
    indent = "    "
    pointer_offset = len(indent) + len(command_prefix) + 1 + len(arg_name) + 1
    pointer = " " * pointer_offset + "^" * _visible_width(raw_value)
    leader = " " * pointer_offset + f"└─ {reason}"

    command_line = Text()
    command_line.append(indent)
    command_line.append(f"{command_prefix} {arg_name} ", style="bright_black")
    command_line.append(raw_value, style="red")
    console.print(command_line)

    console.print(Text(pointer, style="red"))
    console.print(Text(leader, style="red"))
    console.print()

    if suggestions:
        console.print(Text("  did you mean", style="dim"))
        for command, description in suggestions:
            line = Text("    ")
            line.append(f"{GLYPH_ARROW} ", style="green")
            line.append(command, style="cyan")
            if description:
                # Right-pad command so descriptions roughly line up
                # without a hard column constraint. 40 chars covers
                # every realistic suggestion we render today; longer
                # commands push the description right without wrapping.
                # Cell width — see _visible_width for CJK caveat.
                gap = max(2, 40 - _visible_width(command))
                line.append(" " * gap)
                line.append(f"({description})", style="bright_black")
            console.print(line)


def render_missing_secret_error(
    *,
    env_var: str,
    state: _MissingSecretState,
    console: Console,
    next_step: str | None = "see docs/setup.md for the canonical URL format",
) -> None:
    """Render the design's "missing secret" error surface (shape B).

    Lays out:

    * a brand line ``◆ error · <env_var> not set`` (or
      ``... is empty`` depending on ``state``)
    * an "env var <name> is not exported / is empty" block —
      naming the actual lookup failure rather than a downstream
      "connection string" the user never typed
    * a "use --url-env instead" recommendation block with the
      safe command form and the security rationale (no password
      in shell history / ``ps`` / audit log)
    * an "if the var really should be set" block with three
      shell-level diagnostic commands
    * an optional dim ``next:`` breadcrumb pointing at setup docs

    Parameters
    ----------
    env_var
        The env var the user referenced (e.g. ``"DATABASE_URL"``).
    state
        Either ``"unset"`` or ``"empty"``. Any other value raises
        ``ValueError`` rather than silently rendering wrong copy —
        a call-site typo (``"not_set"``, ``"missing"``) must
        surface visibly, not erode the contract.
    console
        Rich console to write to (typically stderr).
    next_step
        Optional dim trailing pointer. Defaults to the setup docs
        breadcrumb the old ``GuidedError`` path carried; pass
        ``None`` to suppress.
    """
    if state == "unset":
        title = f"{env_var} not set"
        panel_title = f"env var {env_var} is not exported"
        body_outcome = "in this shell and found nothing —"
    elif state == "empty":
        title = f"{env_var} is empty"
        panel_title = f"env var {env_var} is set but empty"
        body_outcome = "in this shell and got an empty string —"
    else:
        raise ValueError(
            f"unknown state {state!r}; expected 'unset' or 'empty'. "
            "Add the new state to `_MissingSecretState` and update the title "
            "block in `render_missing_secret_error` if introducing a third case."
        )
    _print_error_brand_line(title, console=console)
    console.print()

    # Panel 1 — name the actual lookup failure.
    summary = Text()
    summary.append(f"  {GLYPH_ERR} ", style="red")
    summary.append(panel_title, style="bold")
    console.print(summary)

    # Panel 1 body: a two-sentence explainer. First line embeds the
    # env var name in red so it stands out against the dim prose; the
    # continuation line restates the impact in plain dim.
    explainer = Text()
    explainer.append("    we looked up ", style="dim")
    explainer.append(env_var, style="red")
    explainer.append(f" {body_outcome}", style="dim")
    console.print(explainer)
    console.print(Text("    so we have no URL to connect to.", style="dim"))
    console.print()

    # Panel 2 — recommended fix.
    recommended_title = Text()
    recommended_title.append(f"  {GLYPH_OK} ", style="green")
    recommended_title.append("use --url-env once the var is exported", style="bold")
    recommended_title.append("    ")
    recommended_title.append("(recommended)", style="bright_black")
    console.print(recommended_title)

    example = Text()
    example.append("    $ ", style="bright_black")
    example.append(f"schemabrain init --url-env {env_var}", style="cyan")
    console.print(example)

    rationale_lines = [
        "    we read the var inside the process · the password never appears in:",
        "      · your shell history",
        "      · ps aux output",
        "      · the audit log",
    ]
    for raw in rationale_lines:
        console.print(Text(raw, style="bright_black"))
    console.print()

    # Panel 3 — shell-level diagnostics.
    console.print(Text("  if the var really should be set", style="dim"))
    diagnostics = [
        (f"echo ${env_var}", "check the value · prints nothing if unset"),
        (f"env | grep {env_var}", "also surfaces if it's whitespace-only"),
        ("source .env", "load from a file before retrying"),
    ]
    for command, comment in diagnostics:
        line = Text()
        line.append("    $ ", style="bright_black")
        line.append(command, style="bold")
        # Cell width — see _visible_width for CJK caveat.
        gap = max(2, 28 - _visible_width(command))
        line.append(" " * gap)
        line.append(f"# {comment}", style="bright_black")
        console.print(line)

    if next_step:
        console.print()
        breadcrumb = Text()
        breadcrumb.append(f"  {GLYPH_ARROW} next: ", style="bright_black")
        breadcrumb.append(next_step, style="dim")
        console.print(breadcrumb)


def _print_error_brand_line(title: str, *, console: Console) -> None:
    """Common brand line for every error shape: ``◆ error · <title>``.

    The brand glyph is red (severity carrier); ``error`` is bold
    foreground; the title is dim. Matches the design's
    ``ErrChrome`` header (handoff bundle ``cli/errors.jsx:6``).
    """
    text = Text()
    text.append(GLYPH_BRAND, style="red")
    text.append(" ")
    text.append("error", style="bold")
    text.append(f" {GLYPH_SEP} ", style="bright_black")
    text.append(title, style="dim")
    console.print(text)


def _visible_width(text: str) -> int:
    """Return the visual width of ``text`` in terminal cells.

    A single ASCII char is one cell; this matches what Rich
    renders for the strings we feed in. The renderer uses this to
    size the ``^^^`` underline so it sits flush with the bad
    token. Any future widening to handle East Asian double-width
    characters lives here — call sites stay unchanged. Use
    ``rich.text.Text.cell_len`` here when CJK support is needed.
    """
    return len(text)


__all__ = [
    "render_bad_argument_error",
    "render_missing_secret_error",
]
