"""Design-bundle error surface renderers.

Composes the three error shapes specified by the handoff bundle
(``schemabrain-v1/project/cli/errors.jsx``):

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

* **C — LLM failure** (``render_llm_failure``): the 529 / 429 /
  network / generic-API-error advisory for the wizard stages 3-4
  and standalone ``entities|metrics suggest`` paths. Surfaced
  with a kind-specific title, a one-line cause, two recovery
  commands ("retry as-is" + "skip the LLM stage"), and a
  trailing breadcrumb. Replaces the raw Python traceback that
  used to reach the user on any Anthropic outage.

Module shape mirrors ``setup/doctor_render.py`` from PR #4:

* ``errors.py`` stays the pure DTO + translator module.
* ``errors_render.py`` (this file) is the visual chrome — pure
  layout primitives composed onto a caller-supplied ``Console``.

Lives at package root rather than under ``cli/``  so the error
shape is reachable from any subcommand without importing through
``cli.py`` (which would cycle imports during error rendering).
"""

from __future__ import annotations

import types
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

LlmFailureKind = Literal["overloaded", "rate_limited", "connection", "api_error"]
"""Classification of an Anthropic SDK failure for ``render_llm_failure``.

* ``"overloaded"`` — ``anthropic.OverloadedError`` (HTTP 529). The
  model is temporarily refusing new requests; retry in ~30-60s or
  fall back to a non-LLM path.
* ``"rate_limited"`` — ``anthropic.RateLimitError`` (HTTP 429).
  Caller is asking too fast; back off + consider lowering
  ``SCHEMABRAIN_*_CONCURRENCY``.
* ``"connection"`` — ``anthropic.APIConnectionError``. Network /
  DNS / proxy issue; the request never reached Anthropic.
* ``"api_error"`` — catch-all ``anthropic.APIError`` subclass that
  isn't one of the three above (5xx other than 529, 4xx other
  than 429, etc.). Caller should pass the SDK's response message
  through as ``cause`` so the renderer can surface it verbatim.
"""


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


def render_llm_failure(
    *,
    kind: LlmFailureKind,
    retry_command: str,
    fallback_command: str | None,
    cause: str,
    console: Console,
    next_step: str | None = "see https://status.anthropic.com if it persists",
) -> None:
    """Render the design's "LLM failure" advisory surface (shape C).

    Lays out:

    * a brand line ``◆ error · <kind-specific title>``
    * a one-panel "what happened" block naming the SDK exception
      kind in plain language + a short cause line passed by the
      caller (typically the SDK's response message)
    * a "what to do" block listing a retry command and an optional
      fallback (e.g. ``--no-enrich``); when no fallback is
      meaningful, the caller passes ``fallback_command=None`` and
      only the retry line renders
    * an optional dim ``next:`` breadcrumb pointing at the Anthropic
      status page

    Replaces the raw Python traceback that used to reach the user
    when ``anthropic.OverloadedError`` / ``RateLimitError`` /
    ``APIConnectionError`` / ``APIError`` bubbled out of the LLM
    pipeline. Five callsites today: ``_cmd_index`` (with enrich),
    ``_cmd_entities_suggest``, ``_cmd_metrics_suggest`` in
    ``cli.py``; ``_run_entity_suggestion``, ``_run_metric_suggestion``
    in ``setup/wizard.py``.

    Parameters
    ----------
    kind
        One of the four ``LlmFailureKind`` values. Any other value
        raises ``ValueError`` — a call-site typo must surface
        visibly rather than render wrong copy (matches the contract
        of ``render_missing_secret_error``'s state validation).
    retry_command
        The exact shell command to suggest as the primary recovery.
        Should be the command the user actually ran (with secrets
        elided) so the recovery is a literal copy-paste.
    fallback_command
        Optional secondary recovery — typically the ``--no-enrich``
        variant of ``retry_command``. Pass ``None`` for surfaces
        where structure-only is not a meaningful fallback (e.g.
        ``entities suggest`` whose entire job is the LLM call).
    cause
        Short one-line cause string surfaced verbatim under the
        ✗ glyph. For ``"api_error"``, prefer the SDK's
        ``error.message`` over ``str(exc)`` so the user sees the
        server-side detail (rate-limit window, quota name, etc.).
    console
        Rich console to write to (typically stderr).
    next_step
        Optional dim trailing pointer. Defaults to the Anthropic
        status page; pass ``None`` to suppress.
    """
    title, panel_title = _llm_failure_titles(kind)
    _print_error_brand_line(title, console=console)
    console.print()

    # Panel 1 — name the failure in plain language + caller-supplied cause.
    summary = Text()
    summary.append(f"  {GLYPH_ERR} ", style="red")
    summary.append(panel_title, style="bold")
    console.print(summary)

    cause_line = Text()
    cause_line.append("    ")
    cause_line.append(cause, style="dim")
    console.print(cause_line)
    console.print()

    # Panel 2 — recovery commands.
    recommended_title = Text()
    recommended_title.append(f"  {GLYPH_OK} ", style="green")
    recommended_title.append("what to do", style="bold")
    console.print(recommended_title)

    retry_line = Text()
    retry_line.append("    $ ", style="bright_black")
    retry_line.append(retry_command, style="cyan")
    # Hint comment — the action being recommended.
    retry_hint = _llm_failure_retry_hint(kind)
    gap = max(2, 48 - _visible_width(retry_command))
    retry_line.append(" " * gap)
    retry_line.append(f"# {retry_hint}", style="bright_black")
    console.print(retry_line)

    if fallback_command is not None:
        fallback_line = Text()
        fallback_line.append("    $ ", style="bright_black")
        fallback_line.append(fallback_command, style="cyan")
        gap = max(2, 48 - _visible_width(fallback_command))
        fallback_line.append(" " * gap)
        fallback_line.append("# skip the LLM stage", style="bright_black")
        console.print(fallback_line)

    if next_step:
        console.print()
        breadcrumb = Text()
        breadcrumb.append(f"  {GLYPH_ARROW} next: ", style="bright_black")
        breadcrumb.append(next_step, style="dim")
        console.print(breadcrumb)


def _llm_failure_titles(kind: LlmFailureKind) -> tuple[str, str]:
    """Return ``(brand_title, panel_title)`` for an LLM failure kind.

    Centralizes the copy so the brand line and the panel-1 title
    stay consistent. Raises ``ValueError`` on unknown kinds — same
    posture as ``render_missing_secret_error``'s state check: a
    typo must surface, not silently render an empty title.
    """
    if kind == "overloaded":
        return ("Anthropic is overloaded", "the model is temporarily refusing new requests")
    if kind == "rate_limited":
        return (
            "rate-limited by Anthropic",
            "you're sending requests faster than the account quota allows",
        )
    if kind == "connection":
        return (
            "couldn't reach Anthropic",
            "the request never left this machine — network, DNS, or proxy issue",
        )
    if kind == "api_error":
        return (
            "Anthropic returned an error",
            "the API call reached Anthropic but came back with an error",
        )
    raise ValueError(
        f"unknown kind {kind!r}; expected one of "
        "'overloaded' | 'rate_limited' | 'connection' | 'api_error'. "
        "Add the new kind to `LlmFailureKind` and update both "
        "`_llm_failure_titles` and `_llm_failure_retry_hint` if "
        "introducing a new shape."
    )


def _llm_failure_retry_hint(kind: LlmFailureKind) -> str:
    """Per-kind retry hint rendered as the inline ``# ...`` comment.

    Kept beside ``_llm_failure_titles`` so the two per-kind copy
    tables stay aligned. Same unknown-kind posture: surfaces loud,
    not silent.
    """
    if kind == "overloaded":
        return "retry in 30-60s"
    if kind == "rate_limited":
        return "wait, then retry · or lower SCHEMABRAIN_*_CONCURRENCY"
    if kind == "connection":
        return "check network / proxy, then retry"
    if kind == "api_error":
        return "retry; if it persists, check the message above"
    raise ValueError(
        f"unknown kind {kind!r}; expected one of "
        "'overloaded' | 'rate_limited' | 'connection' | 'api_error'."
    )


def classify_llm_failure(exc: BaseException) -> LlmFailureKind | None:
    """Map an exception to a ``LlmFailureKind`` if it's an Anthropic SDK error.

    Returns the kind when ``exc`` is one of the recognized
    ``anthropic.*`` exception classes; returns ``None`` otherwise
    (so the caller can let an unrelated exception propagate to a
    different handler or to the top-level traceback). The match
    uses ``isinstance`` on each candidate class so SDK subclasses
    inherit the right kind without needing per-version updates
    here.

    Lives next to ``render_llm_failure`` because every callsite
    that wants to render the shape needs the classification; keeps
    the two pieces synchronized when a new SDK exception type is
    added. The anthropic import is lazy so the module stays
    importable when the SDK is not installed (tests, stub paths).
    """
    try:
        import anthropic
    except ImportError:  # pragma: no cover — anthropic is a hard runtime dep today
        return None

    # Order matters: ``OverloadedError`` and ``RateLimitError`` are
    # both ``APIStatusError`` subclasses, so the more specific
    # checks must come first. ``APIConnectionError`` and the bare
    # ``APIError`` are siblings, not in the ``APIStatusError``
    # hierarchy.
    if isinstance(exc, anthropic.APIStatusError):
        if isinstance(exc, anthropic.RateLimitError):
            return "rate_limited"
        if _is_overloaded_error(exc, anthropic_module=anthropic):
            return "overloaded"
        return "api_error"
    if isinstance(exc, anthropic.APIConnectionError):
        return "connection"
    if isinstance(exc, anthropic.APIError):
        return "api_error"
    return None


def cause_from_llm_error(exc: BaseException) -> str:
    """Extract a one-line cause string from an Anthropic SDK exception.

    Round-1 fold M3: lifted from ``cli._try_render_llm_failure`` so
    the ``getattr(exc, "message", ...)`` access lives next to the
    ``classify_llm_failure`` boundary that establishes the
    ``exc is Anthropic SDK type`` invariant — single owner, single
    untyped-access site, one fallback chain.

    Callers should invoke this only AFTER ``classify_llm_failure``
    has returned non-None, so ``exc`` is known to be an
    ``anthropic.APIError`` subclass. The ``getattr`` fallback
    chain (``error.message`` → ``str(exc)`` → ``type(exc).__name__``)
    handles every SDK subclass that ships today plus any new ones
    that may omit the ``message`` field.
    """
    return getattr(exc, "message", None) or str(exc) or type(exc).__name__


def _is_overloaded_error(exc: BaseException, *, anthropic_module: types.ModuleType) -> bool:
    """Return True when ``exc`` is the SDK's 529-overloaded error.

    Round-1 fold H2: ``anthropic_module`` is typed as
    ``types.ModuleType`` rather than ``object``. Same anti-pattern
    that python-reviewer flagged convergently in PRs #4 and #7
    (``_compose_footer_line(summary: object)``,
    ``_compose_entity_brand_line(entity: object)``) — the
    ``getattr`` for SDK-version tolerance is correct, but the type
    annotation should reflect what the parameter actually is.

    Wrapped to tolerate SDK versions that ship the class under a
    different attribute name. ``InternalServerError`` is the 5xx
    catch-all; some SDK versions surface 529 through a dedicated
    ``OverloadedError``. We check by attribute name to avoid an
    AttributeError on older versions, and fall back to HTTP-status
    inspection so we still classify correctly when the dedicated
    class is absent.
    """
    overloaded_cls = getattr(anthropic_module, "OverloadedError", None)
    if overloaded_cls is not None and isinstance(exc, overloaded_cls):
        return True
    status = getattr(exc, "status_code", None)
    return status == 529


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
    "LlmFailureKind",
    "cause_from_llm_error",
    "classify_llm_failure",
    "render_bad_argument_error",
    "render_llm_failure",
    "render_missing_secret_error",
]
