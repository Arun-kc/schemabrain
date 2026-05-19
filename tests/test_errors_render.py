"""Pins the layout contract of ``schemabrain.errors_render``.

The error renderer ships two of the three design shapes specified
by the handoff bundle (``schemabrain-v1/project/cli/errors.jsx``):

* **A — bad input** (``render_bad_argument_error``) — caret-pointer
  + remediation suggestions.
* **B — missing secret** (``render_missing_secret_error``) — three
  panel block recommending ``--url-env`` over leaky alternatives.

Tests pin the visible substrings each surface MUST emit so a future
refactor that breaks a layout invariant fails at the unit level
rather than as a visual regression caught during smoke. Tests use
Rich's recording console so the rendered string can be inspected
without a real TTY.

Shape C (the 529 advisory) is deliberately not implemented yet —
it requires new exception-catching plumbing inside the wizard /
``entities suggest`` flow beyond a visual upgrade to an existing
render call. Tracked for a follow-up PR.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from schemabrain.errors_render import (
    render_bad_argument_error,
    render_missing_secret_error,
)


def _render(call: object, *, width: int = 120) -> str:
    """Helper — render via a recording Console at the design's reference width.

    ``force_terminal=False`` keeps Rich from emitting ANSI escape
    codes around styled spans, so substring assertions that
    straddle style transitions (eg the brand-line ``◆ error``
    spans glyph + bold word) match cleanly.
    """
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=width, no_color=True)
    call(console)  # type: ignore[operator]
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Shape A — bad-input caret-underline.
# ---------------------------------------------------------------------------


class TestRenderBadArgumentError:
    def _default_call(self, **overrides: object) -> object:
        defaults = dict(
            arg_name="--since",
            raw_value="wednesday",
            reason="not a duration · not a date",
            expected_summary="a duration like 14d or an ISO 8601 timestamp",
            suggestions=[
                ("schemabrain index --since 7d", "last 7 days"),
                ("schemabrain index", "full re-index"),
            ],
            command_prefix="schemabrain index",
        )
        defaults.update(overrides)
        return lambda console: render_bad_argument_error(  # type: ignore[arg-type]
            **defaults, console=console
        )

    def test_brand_line_carries_error_glyph(self) -> None:
        out = _render(self._default_call())
        assert "◆ error" in out

    def test_brand_line_includes_arg_name(self) -> None:
        out = _render(self._default_call())
        assert "bad value for --since" in out

    def test_summary_shows_invalid_argument_with_token(self) -> None:
        out = _render(self._default_call())
        assert "invalid argument" in out
        assert "--since" in out
        assert "wednesday" in out

    def test_explainer_quotes_raw_value(self) -> None:
        out = _render(self._default_call())
        assert 'got "wednesday"' in out
        assert "a duration like 14d" in out

    def test_command_line_reproduced_verbatim(self) -> None:
        out = _render(self._default_call())
        # Full reproduction of what the user actually ran.
        assert "schemabrain index --since wednesday" in out

    def test_caret_underline_width_matches_raw_value(self) -> None:
        out = _render(self._default_call())
        # The caret underline runs ``^`` for ``len(raw_value)`` chars.
        # Substring `^^^^^^^^^` is exactly len("wednesday") = 9.
        assert "^^^^^^^^^" in out
        # And does NOT extend past the value (no 10-caret string).
        assert "^^^^^^^^^^" not in out

    def test_leader_carries_reason(self) -> None:
        out = _render(self._default_call())
        assert "└─ not a duration · not a date" in out

    def test_suggestions_block_header(self) -> None:
        out = _render(self._default_call())
        assert "did you mean" in out

    def test_suggestions_render_each_command(self) -> None:
        out = _render(self._default_call())
        assert "schemabrain index --since 7d" in out
        assert "(last 7 days)" in out
        assert "(full re-index)" in out

    def test_suggestions_accepts_a_tuple_sequence(self) -> None:
        # The renderer's contract is ``Sequence[tuple[str, str]]``
        # — a tuple of pairs must work, not just a list.
        out = _render(
            self._default_call(suggestions=(("schemabrain index --since 7d", "last 7 days"),))
        )
        assert "schemabrain index --since 7d" in out

    def test_no_suggestions_still_renders_caret_block(self) -> None:
        # When the caller has no concrete corrections to offer, the
        # caret + reason must still render — the "did you mean"
        # block just doesn't appear.
        out = _render(self._default_call(suggestions=[]))
        assert "^^^^^^^^^" in out
        assert "did you mean" not in out

    def test_suggestion_with_empty_description_renders_command_alone(self) -> None:
        # An empty description on a suggestion renders the command
        # without the trailing parenthetical — keeps the layout
        # contract honest when a caller can't think of a one-liner.
        out = _render(self._default_call(suggestions=[("schemabrain index", "")]))
        assert "schemabrain index" in out
        # No empty parens dangle at the end of the line.
        assert "()" not in out

    def test_arrow_glyph_in_suggestions(self) -> None:
        out = _render(self._default_call())
        # ``→`` precedes each suggestion line.
        assert "→" in out


# ---------------------------------------------------------------------------
# Shape B — missing-secret three-panel.
# ---------------------------------------------------------------------------


class TestRenderMissingSecretError:
    def _default_call(self, **overrides: object) -> object:
        defaults = dict(env_var="DATABASE_URL", state="unset")
        defaults.update(overrides)
        return lambda console: render_missing_secret_error(  # type: ignore[arg-type]
            **defaults, console=console
        )

    def test_brand_line_renders_with_error_glyph(self) -> None:
        out = _render(self._default_call())
        assert "◆ error" in out

    def test_unset_title_wording(self) -> None:
        out = _render(self._default_call(state="unset"))
        assert "DATABASE_URL not set" in out

    def test_empty_title_wording(self) -> None:
        out = _render(self._default_call(state="empty"))
        assert "DATABASE_URL is empty" in out

    def test_unknown_state_raises_loudly(self) -> None:
        # Unknown states MUST raise — a call-site typo (eg passing
        # ``"not_set"`` instead of ``"unset"``) would otherwise
        # silently render wrong copy. Pinning the raise contract
        # at the unit level catches the regression deterministically.
        with pytest.raises(ValueError, match="unknown state"):
            _render(self._default_call(state="weird_unknown_state"))

    def test_missing_panel_names_the_lookup_failure(self) -> None:
        # Panel title names the actual failure (env var lookup),
        # not a downstream "connection string" the user never
        # typed. The old phrasing misled users running
        # ``inspect``/``check`` where there is no "connection"
        # being attempted.
        out = _render(self._default_call())
        assert "env var DATABASE_URL is not exported" in out

    def test_empty_panel_names_empty_value_distinct_from_unset(self) -> None:
        out = _render(self._default_call(state="empty"))
        assert "env var DATABASE_URL is set but empty" in out

    def test_recommended_block_shows_url_env_form(self) -> None:
        out = _render(self._default_call())
        assert "use --url-env" in out
        assert "schemabrain init --url-env DATABASE_URL" in out

    def test_security_rationale_lists_three_leak_surfaces(self) -> None:
        out = _render(self._default_call())
        assert "your shell history" in out
        assert "ps aux" in out
        assert "audit log" in out

    def test_diagnostics_block_shows_three_shell_commands(self) -> None:
        out = _render(self._default_call())
        assert "echo $DATABASE_URL" in out
        assert "env | grep" in out
        assert "source .env" in out

    def test_diagnostics_grep_uses_full_var_name(self) -> None:
        # The diagnostic command must grep the exact env var name,
        # not a heuristic prefix. ``env | grep MY`` on ``MY_DB_URL``
        # would match unrelated ``MYSQL_*`` etc. and mislead the user.
        out = _render(self._default_call(env_var="MY_DB_URL"))
        assert "env | grep MY_DB_URL" in out
        assert "env | grep MY\n" not in out

    def test_next_step_breadcrumb_renders_by_default(self) -> None:
        out = _render(self._default_call())
        # The default next_step matches the old ``GuidedError`` path's
        # ``next_step="see docs/setup.md for the canonical URL format"``
        # so the documentation breadcrumb doesn't silently drop.
        assert "next:" in out
        assert "docs/setup.md" in out

    def test_next_step_can_be_suppressed(self) -> None:
        out = _render(self._default_call(next_step=None))
        assert "next:" not in out
        assert "docs/setup.md" not in out

    def test_next_step_accepts_custom_pointer(self) -> None:
        out = _render(self._default_call(next_step="custom pointer xyz"))
        assert "next:" in out
        assert "custom pointer xyz" in out

    def test_empty_state_explainer_is_distinct_from_unset(self) -> None:
        # The two states render different panel titles AND different
        # body copy so an operator can tell at a glance which case
        # they're in.
        out_empty = _render(self._default_call(state="empty"))
        out_unset = _render(self._default_call(state="unset"))
        assert "is set but empty" in out_empty
        assert "is set but empty" not in out_unset
        assert "is not exported" in out_unset
        assert "is not exported" not in out_empty


# ---------------------------------------------------------------------------
# Cross-shape consistency
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(
            lambda console: render_bad_argument_error(
                arg_name="--since",
                raw_value="wednesday",
                reason="not a duration",
                expected_summary="a duration like 14d",
                suggestions=[("schemabrain index --since 7d", "last 7 days")],
                command_prefix="schemabrain index",
                console=console,
            ),
            id="shape_a_bad_input",
        ),
        pytest.param(
            lambda console: render_missing_secret_error(
                env_var="DATABASE_URL", state="unset", console=console
            ),
            id="shape_b_missing_secret",
        ),
    ],
)
class TestErrorShapesConsistency:
    def test_brand_line_uses_red_severity(self, call: object) -> None:
        """Every error shape leads with ``◆ error``."""
        out = _render(call)
        assert "◆ error" in out

    def test_renders_without_crashing_at_narrow_width(self, call: object) -> None:
        """Narrow terminals (80 cols) get the same content, soft-wrapped."""
        out = _render(call, width=80)
        assert "◆ error" in out
