"""Pins the layout contract of ``schemabrain.init_help_render``.

The grouped help surface is the fourth operator-visible win from
the design-system migration arc (after wizard hero, doctor
checklist, and error renderers). These tests pin:

* The cyan brand line ``◆ schemabrain init`` heads the surface.
* The three-line preamble (``usage`` / ``stages`` / ``runtime``)
  renders with the design's labels.
* All five argument groups (``SOURCE`` / ``STAGES`` / ``HOST``
  / ``COST`` / ``BEHAVIOR``) render with their dim labels and
  one-line purposes.
* Every ``init`` flag appears in exactly one group's flag table.
* The examples block renders three representative commands.
* The renderer is wired to ``-h`` and ``--help`` via the
  ``_GroupedInitHelpAction`` custom argparse Action.

Tests use the real parser via ``schemabrain.cli._build_parser``
so a future flag rename, group move, or wire-up regression
fails at the unit level.
"""

from __future__ import annotations

import argparse
import io

import pytest
from rich.console import Console

from schemabrain.cli import _build_parser
from schemabrain.init_help_render import (
    _format_flag_spec,
    _is_help_action,
    _user_facing_groups,
    render_init_help,
)


@pytest.fixture
def init_parser() -> argparse.ArgumentParser:
    """The real ``init`` subparser, post-build.

    Uses ``_build_parser`` so the test stays honest about what
    actually ships in production — a flag that gets renamed in
    ``cli.py`` but stayed in the test fixture would mask the
    regression.
    """
    parser = _build_parser()
    # ``argparse``'s subparser action is the second positional
    # action on the top-level parser. Walk its choices to find
    # the ``init`` subparser.
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices["init"]
    raise AssertionError("init subparser not found on built parser")


def _render(parser: argparse.ArgumentParser, *, width: int = 120) -> str:
    """Helper — render to an in-memory buffer at the design's reference width."""
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=width, no_color=True)
    render_init_help(parser, console=console)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Brand line + preamble
# ---------------------------------------------------------------------------


class TestBrandLine:
    def test_brand_glyph_present(self, init_parser: argparse.ArgumentParser) -> None:
        assert "◆" in _render(init_parser)

    def test_schemabrain_init_words_present(self, init_parser: argparse.ArgumentParser) -> None:
        assert "schemabrain init" in _render(init_parser)

    def test_tagline_mentions_7_stage_wizard(self, init_parser: argparse.ArgumentParser) -> None:
        assert "7-stage activation wizard" in _render(init_parser)


class TestPreamble:
    def test_usage_line_renders(self, init_parser: argparse.ArgumentParser) -> None:
        out = _render(init_parser)
        assert "usage" in out
        assert "schemabrain init [--source URL | --url-env VAR] [flags]" in out

    def test_stages_chain_renders(self, init_parser: argparse.ArgumentParser) -> None:
        out = _render(init_parser)
        # The 7-stage chain reads top-to-bottom — operators see the
        # wizard's shape before scanning flags.
        for stage in (
            "source_check",
            "index",
            "entities",
            "metrics",
            "joins",
            "wire_host",
            "next_step",
        ):
            assert stage in out

    def test_runtime_summary_renders(self, init_parser: argparse.ArgumentParser) -> None:
        out = _render(init_parser)
        assert "runtime" in out
        assert "Sonnet" in out
        assert "Haiku" in out
        assert "--enrich" in out


# ---------------------------------------------------------------------------
# Group blocks
# ---------------------------------------------------------------------------


class TestGroupBlocks:
    def test_five_user_facing_groups(self, init_parser: argparse.ArgumentParser) -> None:
        # ``cli.py`` registers exactly five user-facing groups
        # (source, stages, host, cost, behavior); argparse's
        # default ``positional`` / ``optional`` groups are
        # filtered out by ``_user_facing_groups``.
        groups = _user_facing_groups(init_parser)
        titles = [g.title for g in groups]
        assert titles == ["Source", "Stages", "Host", "Cost", "Behavior"]

    @pytest.mark.parametrize(
        "label,purpose_substring",
        [
            ("SOURCE", "where does the schema come from"),
            ("STAGES", "turn individual wizard stages"),
            ("HOST", "AI agent to wire up"),
            ("COST", "spend ceilings"),
            ("BEHAVIOR", "how the wizard runs"),
        ],
    )
    def test_each_group_renders_label_and_purpose(
        self,
        init_parser: argparse.ArgumentParser,
        label: str,
        purpose_substring: str,
    ) -> None:
        out = _render(init_parser)
        assert label in out
        assert purpose_substring in out

    @pytest.mark.parametrize(
        "flag,expected_group_title",
        [
            ("--source", "Source"),
            ("--url-env", "Source"),
            ("--from-dbt", "Source"),
            ("--skip-index", "Stages"),
            ("--enrich", "Stages"),
            ("--no-entities", "Stages"),
            ("--no-metrics", "Stages"),
            ("--no-joins", "Stages"),
            ("--host", "Host"),
            ("--store-path", "Host"),
            ("--env-var", "Host"),
            ("--entities-max-cost-usd", "Cost"),
            ("--metrics-max-cost-usd", "Cost"),
            ("--yes", "Behavior"),
            ("--skip-llm-confirm", "Behavior"),
            ("--print-only", "Behavior"),
        ],
    )
    def test_flag_lives_in_expected_group(
        self,
        init_parser: argparse.ArgumentParser,
        flag: str,
        expected_group_title: str,
    ) -> None:
        """Pin which group each flag lives in.

        A future ``cli.py`` refactor that moves a flag between
        groups (eg ``--enrich`` from ``Stages`` to ``Behavior``)
        breaks this test loudly rather than the user noticing
        on the help screen.
        """
        for group in init_parser._action_groups:
            if group.title == expected_group_title:
                option_strings = [
                    opt for action in group._group_actions for opt in action.option_strings
                ]
                assert flag in option_strings, (
                    f"Expected {flag} in group {expected_group_title!r}; saw {option_strings}"
                )
                return
        pytest.fail(f"Group {expected_group_title!r} not present on init parser")


# ---------------------------------------------------------------------------
# Flag spec formatting
# ---------------------------------------------------------------------------


class TestFormatFlagSpec:
    def test_store_true_flag_has_no_metavar(self, init_parser: argparse.ArgumentParser) -> None:
        action = _find_action(init_parser, "--enrich")
        spec = _format_flag_spec(action)
        assert spec == "--enrich"

    def test_value_flag_renders_angle_metavar(self, init_parser: argparse.ArgumentParser) -> None:
        action = _find_action(init_parser, "--source")
        spec = _format_flag_spec(action)
        # ``--source URL`` (argparse upper-case) becomes ``--source <url>``
        # — lowercased + angle-wrapped per the design.
        assert spec == "--source <url>"

    def test_choice_flag_uses_angle_name(self, init_parser: argparse.ArgumentParser) -> None:
        action = _find_action(init_parser, "--host")
        spec = _format_flag_spec(action)
        # ``--host`` has ``choices=(...)`` and no explicit metavar
        # → fallback collapses to ``<name>`` rather than spelling
        # every choice inline.
        assert spec == "--host <name>"

    def test_short_and_long_options_render_together(
        self, init_parser: argparse.ArgumentParser
    ) -> None:
        action = _find_action(init_parser, "--yes")
        spec = _format_flag_spec(action)
        # Short option first, long option second — matches the
        # design's convention (``-y, --yes``).
        assert spec == "-y, --yes"

    def test_dest_fallback_when_no_metavar_or_choices(self) -> None:
        # Defensive fallback: an action with neither ``metavar=``
        # nor ``choices=`` derives its angle-bracket label from
        # ``action.dest`` (lowercased). Today every init flag
        # sets an explicit ``metavar``; this pin keeps the
        # fallback honest for future flags that forget.
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("--my-flag", dest="my_flag", default=None)
        action = _find_action(parser, "--my-flag")
        assert _format_flag_spec(action) == "--my-flag <my_flag>"

    def test_numeric_type_fallback_when_no_metavar(self) -> None:
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("--cap", type=float, default=None)
        action = _find_action(parser, "--cap")
        # ``<n>`` for numeric flags — terser than ``<cap>`` and
        # matches the design's ``<N>`` style.
        assert _format_flag_spec(action) == "--cap <n>"


# ---------------------------------------------------------------------------
# Examples + footer
# ---------------------------------------------------------------------------


class TestExamples:
    def test_three_example_commands_present(self, init_parser: argparse.ArgumentParser) -> None:
        out = _render(init_parser)
        assert "schemabrain init --url-env DATABASE_URL --host claude-desktop" in out
        assert "schemabrain init --url-env DATABASE_URL --skip-index --no-entities" in out
        assert (
            "schemabrain init --from-dbt ./target/manifest.json --host manual --print-only" in out
        )

    def test_examples_header_dim(self, init_parser: argparse.ArgumentParser) -> None:
        out = _render(init_parser)
        assert "examples" in out

    def test_see_also_footer_present(self, init_parser: argparse.ArgumentParser) -> None:
        out = _render(init_parser)
        assert "see also" in out
        assert "schemabrain --help" in out
        assert "schemabrain doctor" in out


# ---------------------------------------------------------------------------
# Help-action wire-up
# ---------------------------------------------------------------------------


class TestHelpActionWireUp:
    def test_dash_h_invokes_grouped_renderer(
        self,
        init_parser: argparse.ArgumentParser,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # The init parser must short-circuit on ``-h`` via the
        # design renderer. SystemExit confirms argparse exited
        # cleanly after the help action fired.
        with pytest.raises(SystemExit) as excinfo:
            init_parser.parse_args(["-h"])
        assert excinfo.value.code == 0
        captured = capsys.readouterr()
        # The renderer writes to the configured ``_stderr_console``;
        # the brand glyph in either stream confirms the design
        # surface fired (not argparse's plaintext fallback).
        combined = captured.err + captured.out
        assert "◆" in combined
        assert "schemabrain init" in combined

    def test_long_help_invokes_grouped_renderer(
        self,
        init_parser: argparse.ArgumentParser,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        with pytest.raises(SystemExit) as excinfo:
            init_parser.parse_args(["--help"])
        assert excinfo.value.code == 0
        # ``capsys.readouterr()`` drains the buffer on the first
        # call — re-reading via ``.err`` and ``.out`` from a
        # SINGLE captured result keeps both streams accessible.
        captured = capsys.readouterr()
        combined = captured.err + captured.out
        assert "◆" in combined
        assert "schemabrain init" in combined

    def test_help_action_takes_no_arguments(self, init_parser: argparse.ArgumentParser) -> None:
        # The replacement help action must be ``nargs=0`` — passing
        # a value should not be expected.
        for action in init_parser._actions:
            if "--help" in action.option_strings:
                assert action.nargs == 0
                return
        pytest.fail("no help action found on init parser")

    def test_help_action_is_identified_by_is_help_action(
        self, init_parser: argparse.ArgumentParser
    ) -> None:
        # The renderer's ``_is_help_action`` filter must catch our
        # custom action — otherwise the help flag would surface in
        # the design's grid as a regular flag.
        for action in init_parser._actions:
            if "--help" in action.option_strings:
                assert _is_help_action(action)
                return
        pytest.fail("no help action found on init parser")

    def test_warns_when_titled_group_has_no_description(self) -> None:
        # Contract guard: a future ``cli.py`` author who registers
        # a new group with a title but forgets the ``description=``
        # kwarg must see a runtime ``UserWarning`` so the missing
        # group surfaces in development. The flags would otherwise
        # silently vanish from the rendered help.
        parser = argparse.ArgumentParser(add_help=False)
        # Group with both title AND description — surfaces correctly.
        ok_group = parser.add_argument_group("Real", description="renders in the help screen")
        ok_group.add_argument("--real-flag", action="store_true")
        # Group with a title but NO description — must warn.
        silent_group = parser.add_argument_group("Silent")
        silent_group.add_argument("--hidden-flag", action="store_true")

        buf = io.StringIO()
        console = Console(file=buf, force_terminal=False, width=120, no_color=True)
        with pytest.warns(UserWarning, match="Silent"):
            render_init_help(parser, console=console)

        out = buf.getvalue()
        # The Silent group's flag is dropped from the rendered grid.
        assert "--hidden-flag" not in out
        # But the Real group's flag still renders normally.
        assert "--real-flag" in out

    def test_help_action_filtered_when_inside_user_facing_group(self) -> None:
        # Defensive contract: even if a future refactor moves the
        # help action into one of the user-facing groups (eg
        # Behavior), the renderer must NOT surface it as a regular
        # flag row. Exercise the filter by constructing a synthetic
        # parser that puts the help action inside a described
        # group.
        parser = argparse.ArgumentParser(add_help=False)
        group = parser.add_argument_group("Behavior", description="how things run")
        group.add_argument("-h", "--help", action="help")
        group.add_argument("--real-flag", action="store_true")

        buf = io.StringIO()
        console = Console(file=buf, force_terminal=False, width=120, no_color=True)
        render_init_help(parser, console=console)
        out = buf.getvalue()

        assert "--real-flag" in out
        # The help action does NOT surface in the design grid as
        # a flag row. The renderer's spec format would emit
        # ``-h, --help`` for a row; the see-also footer mentions
        # ``schemabrain --help`` legitimately (different shape),
        # so we pin the row shape specifically.
        assert "-h, --help" not in out


# ---------------------------------------------------------------------------
# Cross-width robustness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("width", [80, 100, 120, 160])
class TestWidthAdaptivity:
    def test_render_does_not_crash_at_various_widths(
        self, init_parser: argparse.ArgumentParser, width: int
    ) -> None:
        out = _render(init_parser, width=width)
        # Soft-cap at 120; at narrow widths the grid still emits
        # the brand line, group blocks, and examples without
        # raising.
        assert "◆" in out
        assert "SOURCE" in out
        assert "BEHAVIOR" in out
        assert "examples" in out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_action(parser: argparse.ArgumentParser, option_string: str) -> argparse.Action:
    """Look up the Action backing a given option-string on ``parser``."""
    for action in parser._actions:
        if option_string in action.option_strings:
            return action
    raise AssertionError(f"action {option_string} not found on parser")
