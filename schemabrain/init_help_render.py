"""``schemabrain init --help`` design-bundle help surface renderer.

The stage chain rendered in the ``stages`` preamble line is read
live from ``schemabrain.setup.wizard.DEFAULT_STAGES`` so a future
wizard reshape (stage rename, additional stage, reordering) auto-
propagates into the help screen — there is no second source of
truth to drift against. The ``7-stage`` count in the brand line
is derived from the same tuple.

Composes the grouped help screen from the handoff bundle
(``schemabrain-v1/project/cli/operator.jsx`` ``InitHelp`` block):

* a cyan brand line ``◆ schemabrain init — 7-stage activation wizard``
* a three-line preamble (``usage`` / ``stages`` / ``runtime``)
* five grouped flag blocks (``SOURCE`` / ``STAGES`` / ``HOST`` / ``COST``
  / ``BEHAVIOR``), each headed by a dim label + one-line purpose and
  separated by a dashed rule
* an ``examples`` panel with three representative invocations

Decouples the wire-up (``argparse.add_argument_group``) from the
visual layout. ``cli.py`` organises the flags into argument groups
and tags each with a ``description`` carrying the design's
one-line purpose; this module walks the groups and renders them
into the design's grid.

Lives at package root rather than under ``cli/`` so a future
``check --help`` or ``index --help`` migration can reuse the
same primitives without cycling imports through ``cli.py``.
"""

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING

from rich.text import Text

from schemabrain._ui import (
    GLYPH_ARROW,
    GLYPH_BRAND,
    GLYPH_RULE,
    GLYPH_SEP,
    console_render_width,
)

if TYPE_CHECKING:
    import argparse

    from rich.console import Console

# Column widths from the design's CSS grid
# (``32ch | 1fr``). 32 chars covers the longest current init flag
# spec (``--entities-max-cost-usd <N>`` at 28 chars + 4 chars
# breathing room); a future flag exceeding that pushes its help
# text to the next line via Rich's soft-wrap.
_FLAG_COL_WIDTH = 32
_RULE_INDENT = 2

# Runtime estimate shown in the preamble. Kept as a named module-
# level constant so a reviewer searching for "0.07" or "45s" can
# find and update it in one place when model pricing, token
# counts, or the wizard's typical workload shifts. Measured
# against the bundled ecommerce fixture (7 tables / 30 cols).
_RUNTIME_SUMMARY = "~45s · ~$0.07 · 2 LLM calls (Sonnet) · 1 optional (Haiku, with --enrich)"


def render_init_help(parser: argparse.ArgumentParser, *, console: Console) -> None:
    """Render ``parser``'s grouped help surface onto ``console``.

    Walks ``parser._action_groups`` to discover the groups
    organised by ``cli.py`` (``Source`` / ``Stages`` / ``Host``
    / ``Cost`` / ``Behavior``); each group's ``description``
    carries the one-line purpose the design prints next to the
    group label.

    Hidden / built-in argparse groups (positional, optional,
    helper) are skipped — only groups with at least one
    user-facing flag AND a description string render.
    """
    _print_brand_line(console)
    _print_preamble(console)
    console.print()

    groups = _user_facing_groups(parser)
    _warn_on_dropped_groups(parser, kept=groups)
    for index, group in enumerate(groups):
        if index > 0:
            console.print()
        _print_group(group, console=console)

    console.print()
    _print_examples(console)
    console.print()
    _print_footer(console)


def _print_brand_line(console: Console) -> None:
    """``◆ schemabrain init — N-stage activation wizard`` (one row).

    Cyan brand glyph + bold subcommand name + dim tagline. Matches
    the design's surface header (``cli/operator.jsx:509``). The
    stage count is derived from ``DEFAULT_STAGES`` so a wizard
    reshape auto-propagates into the brand line.
    """
    stage_count = _stage_count()
    text = Text()
    text.append(GLYPH_BRAND, style="cyan")
    text.append(" ")
    text.append("schemabrain init", style="bold")
    text.append(f" — {stage_count}-stage activation wizard", style="dim")
    console.print(text)
    console.print()


def _print_preamble(console: Console) -> None:
    """Three-line ``usage`` / ``stages`` / ``runtime`` block.

    All three labels render in dim left-aligned column; the
    bodies use a slightly brighter style so the eye reads the
    information before the chrome. The stages line uses the
    design's arrow-separated chain so a reader sees the wizard's
    shape before diving into flags.
    """
    label_width = len("runtime") + 4  # 4-space gutter

    usage_label = Text("usage".ljust(label_width), style="dim")
    usage_body = Text(
        "schemabrain init [--source URL | --url-env VAR] [flags]",
        style="default",
    )
    console.print(usage_label + usage_body)

    stages_label = Text("stages".ljust(label_width), style="dim")
    stages_body = Text()
    for index, stage_name in enumerate(_stage_names()):
        if index > 0:
            stages_body.append(f" {GLYPH_ARROW} ", style="bright_black")
        stages_body.append(stage_name, style="default")
    console.print(stages_label + stages_body)

    runtime_label = Text("runtime".ljust(label_width), style="dim")
    runtime_body = Text(_RUNTIME_SUMMARY, style="default")
    console.print(runtime_label + runtime_body)


def _print_group(group: argparse._ArgumentGroup, *, console: Console) -> None:
    """Render one group: ``LABEL · purpose`` rule + flag table.

    The group title becomes the design's uppercased label; the
    description becomes the one-line purpose. Each flag emits a
    spec column (``--flag <metavar>``) and an indented help body
    column.

    The flag table renders via ``Table.grid`` so long help text
    soft-wraps under the help column rather than flushing back
    to column 0 — Rich computes the indent from the column
    definition rather than from any explicit padding in the
    composed ``Text``.
    """
    # Lazy import: ``Table`` is only needed when rendering a
    # group, which happens only on ``init --help``.
    from rich.table import Table

    label = (group.title or "").upper()
    purpose = group.description or ""

    rule = Text(" " * _RULE_INDENT, style="bright_black")
    rule.append(label, style="dim")
    rule.append(f"  {GLYPH_SEP}  ", style="bright_black")
    rule.append(purpose, style="bright_black")
    console.print(rule)

    # Dashed separator under the label. Width sized to the
    # console so the rule sinks into chrome rather than breaks
    # the eye-flow above the flag table.
    width = console_render_width(console)
    separator = Text(
        " " * _RULE_INDENT + GLYPH_RULE * max(4, width - _RULE_INDENT * 2),
        style="bright_black",
    )
    console.print(separator)

    grid = Table.grid(padding=(0, 2))
    # Leading empty column carries the design's 2-char indent
    # without relying on row-level prefixes (which would break
    # Rich's column-aware soft-wrap on long help text).
    grid.add_column(width=_RULE_INDENT)
    grid.add_column(width=_FLAG_COL_WIDTH, no_wrap=True)
    grid.add_column(overflow="fold")
    for action in group._group_actions:
        if _is_help_action(action):
            continue
        spec = Text(_format_flag_spec(action))
        help_text = Text(action.help or "", style="dim")
        grid.add_row("", spec, help_text)
    console.print(grid)


def _print_examples(console: Console) -> None:
    """Three-row examples block with a dim ``examples`` header.

    The examples are hardcoded — they document the wizard's three
    most common shapes: full wire-up to Claude Desktop, fast
    reuse-store re-run, and dbt-anchored manual print. Anchored
    in the design's mock at ``cli/operator.jsx:541``.
    """
    header = Text("  examples", style="dim")
    console.print(header)

    width = console_render_width(console)
    separator = Text(
        " " * _RULE_INDENT + GLYPH_RULE * max(4, width - _RULE_INDENT * 2),
        style="bright_black",
    )
    console.print(separator)

    examples = [
        "schemabrain init --url-env DATABASE_URL --host claude-desktop",
        "schemabrain init --url-env DATABASE_URL --skip-index --no-entities",
        "schemabrain init --from-dbt ./target/manifest.json --host manual --print-only",
    ]
    for example in examples:
        line = Text("  $ ", style="bright_black")
        line.append(example, style="default")
        console.print(line)


def _print_footer(console: Console) -> None:
    """``see also`` breadcrumb at the bottom (one line).

    Dim by design — readers who know what they want already left
    the screen; the footer is a quiet pointer for those who
    didn't find their answer above.
    """
    footer = Text(
        f"  {GLYPH_ARROW} see also: schemabrain --help · schemabrain doctor",
        style="bright_black",
    )
    console.print(footer)


def _user_facing_groups(parser: argparse.ArgumentParser) -> list[argparse._ArgumentGroup]:
    """Return the argument groups created by ``cli.py``'s ``init`` wire-up.

    argparse seeds every parser with two default groups
    (``positional arguments`` + ``optional arguments`` /
    ``options`` depending on version). Both have no description
    and contain only built-in actions; we skip them.

    A user-facing group is identified by having BOTH a non-empty
    title AND a non-empty description — the convention ``cli.py``
    follows when registering ``Source`` / ``Stages`` / etc.
    """
    return [
        group
        for group in parser._action_groups
        if (group.title or "").strip() and (group.description or "").strip()
    ]


def _is_help_action(action: argparse.Action) -> bool:
    """Return True if ``action`` is the auto-added ``-h/--help`` action.

    The help action gets folded into one of our user-facing
    groups when we register the custom action; we don't want to
    re-render the help flag in the design's grid.
    """
    return any(opt in {"-h", "--help"} for opt in action.option_strings)


def _format_flag_spec(action: argparse.Action) -> str:
    """Build the design's ``--flag <metavar>`` spec column.

    Mirrors the design's mock format (``--source <url>``,
    ``--entities-max-cost-usd <N>``, ``-y, --yes``, ``--enrich``).
    Boolean flags (``action='store_true'``) omit the metavar;
    choice flags collapse to ``<name>`` rather than rendering
    every alternative inline (the help text already lists them).
    """
    option_strings = list(action.option_strings)
    # Sort short option first, then long — argparse stores them
    # in registration order which varies. Long flags
    # (``--yes``) sort AFTER short ones (``-y``) so the rendered
    # spec reads ``-y, --yes`` per the design's convention.
    option_strings.sort(key=lambda opt: (opt.startswith("--"), opt))
    flags = ", ".join(option_strings)

    if _action_takes_value(action):
        metavar = _angle_metavar(action)
        return f"{flags} {metavar}"
    return flags


def _action_takes_value(action: argparse.Action) -> bool:
    """``True`` when the action expects a value after the flag.

    ``store_true`` / ``store_false`` / ``count`` / ``help`` /
    custom no-arg actions all take no value. Everything else
    (``store`` / ``append`` / ``store_const`` with a value)
    gets a metavar in the design's spec column.
    """
    # nargs == 0 covers most no-value actions including custom
    # ones built by subclassing ``argparse.Action``.
    return action.nargs != 0


def _angle_metavar(action: argparse.Action) -> str:
    """Lowercase angle-bracketed metavar, matching the design's mock.

    argparse uppercases the dest (``ENTITIES_MAX_COST_USD``);
    the design uses ``<N>`` for numeric ceilings, ``<url>``,
    ``<VAR>``, ``<PATH>``, ``<name>``. We honour an explicit
    ``metavar=`` argument verbatim (lowercased + wrapped) so
    cli.py can override the auto-derived name where the design
    differs.
    """
    if action.metavar:
        return f"<{str(action.metavar).lower()}>"
    if action.choices:
        return "<name>"
    if action.type is float or action.type is int:
        return "<n>"
    # Default: lowercase the dest token. ``store_path`` → ``<path>``.
    return f"<{str(action.dest).lower()}>"


def _stage_names() -> list[str]:
    """Return the wizard stage names in render order.

    Lazy import on ``setup.wizard`` so importing ``init_help_render``
    doesn't pull the entire wizard machinery (anthropic SDK,
    pipeline, profilers) into memory unless the help screen
    actually renders.
    """
    from schemabrain.setup.wizard import DEFAULT_STAGES

    return [stage.name for stage in DEFAULT_STAGES]


def _stage_count() -> int:
    """Return the number of wizard stages in ``DEFAULT_STAGES``."""
    from schemabrain.setup.wizard import DEFAULT_STAGES

    return len(DEFAULT_STAGES)


def _warn_on_dropped_groups(
    parser: argparse.ArgumentParser,
    *,
    kept: list[argparse._ArgumentGroup],
) -> None:
    """Warn at render time when a titled group has no description.

    ``_user_facing_groups`` requires BOTH a non-empty title AND a
    non-empty description to surface a group. If ``cli.py`` ever
    registers a group with a title but forgets the
    ``description=`` kwarg, the flags inside silently vanish from
    the rendered help. The user-facing parser still parses those
    flags correctly — so the contract violation is invisible
    without this guard.

    Emits a ``UserWarning`` (not a hard error) so test sandboxes
    and intentional silent-flag-groups stay possible; CI
    treating warnings as errors will catch the regression.
    """
    dropped = [
        group
        for group in parser._action_groups
        if (group.title or "").strip()
        and not (group.description or "").strip()
        and group not in kept
        and any(not _is_help_action(a) for a in group._group_actions)
    ]
    for group in dropped:
        warnings.warn(
            f"argument group {group.title!r} has no description and will not "
            f"render in `init --help`. Add a description= keyword to "
            f"parser.add_argument_group() so its flags surface to users.",
            UserWarning,
            stacklevel=2,
        )


__all__ = [
    "render_init_help",
]
