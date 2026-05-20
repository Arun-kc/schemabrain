"""Pins the layout contract of ``schemabrain.errors_render``.

The error renderer ships the three design shapes specified by
the handoff bundle (``schemabrain-v1/project/cli/errors.jsx``):

* **A — bad input** (``render_bad_argument_error``) — caret-pointer
  + remediation suggestions.
* **B — missing secret** (``render_missing_secret_error``) — three
  panel block recommending ``--url-env`` over leaky alternatives.
* **C — LLM failure** (``render_llm_failure``) — kind-specific
  advisory + two recovery commands. Replaces the raw Python
  traceback when the Anthropic SDK throws.

Tests pin the visible substrings each surface MUST emit so a future
refactor that breaks a layout invariant fails at the unit level
rather than as a visual regression caught during smoke. Tests use
Rich's recording console so the rendered string can be inspected
without a real TTY.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from schemabrain.errors_render import (
    cause_from_llm_error,
    classify_llm_failure,
    render_bad_argument_error,
    render_llm_failure,
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
# Shape C — LLM failure advisory.
# ---------------------------------------------------------------------------


class TestRenderLlmFailure:
    def _default_call(self, **overrides: object) -> object:
        defaults: dict[str, object] = dict(
            kind="overloaded",
            retry_command="schemabrain index",
            fallback_command="schemabrain index --no-enrich",
            cause="HTTP 529 from anthropic.com",
        )
        defaults.update(overrides)
        return lambda console: render_llm_failure(  # type: ignore[arg-type]
            **defaults, console=console
        )

    def test_brand_line_carries_error_glyph(self) -> None:
        out = _render(self._default_call())
        assert "◆ error" in out

    def test_overloaded_title_mentions_anthropic(self) -> None:
        out = _render(self._default_call(kind="overloaded"))
        assert "Anthropic is overloaded" in out

    def test_rate_limited_title_distinct_from_overloaded(self) -> None:
        out_rate = _render(self._default_call(kind="rate_limited"))
        out_over = _render(self._default_call(kind="overloaded"))
        assert "rate-limited" in out_rate
        assert "rate-limited" not in out_over

    def test_connection_title_signals_network(self) -> None:
        out = _render(self._default_call(kind="connection"))
        assert "couldn't reach Anthropic" in out

    def test_api_error_title_is_generic(self) -> None:
        out = _render(self._default_call(kind="api_error"))
        assert "Anthropic returned an error" in out

    def test_unknown_kind_raises_assertion_error(self) -> None:
        # Round-2 fold MED (python-reviewer): contract moved from
        # `raise ValueError("unknown kind")` to `typing.assert_never`
        # so static type-checkers (mypy / pyright) flag a missing
        # branch BEFORE the test runs. Runtime guard is now an
        # `AssertionError` (what `assert_never` raises when called)
        # — same loud-failure posture as before, just with a
        # type-system anchor.
        with pytest.raises(AssertionError):
            _render(self._default_call(kind="not_a_kind"))

    def test_cause_string_rendered_under_glyph(self) -> None:
        out = _render(self._default_call(cause="upstream connect error · 5s timeout"))
        assert "upstream connect error · 5s timeout" in out

    def test_retry_command_included(self) -> None:
        out = _render(self._default_call(retry_command="schemabrain entities suggest"))
        assert "schemabrain entities suggest" in out

    def test_fallback_command_renders_when_provided(self) -> None:
        out = _render(
            self._default_call(
                retry_command="schemabrain index",
                fallback_command="schemabrain index --no-enrich",
            )
        )
        assert "--no-enrich" in out
        assert "skip the LLM stage" in out

    def test_fallback_command_suppressed_when_none(self) -> None:
        # Standalone `entities suggest` has no structure-only
        # fallback (the LLM call IS the job).
        out = _render(
            self._default_call(
                retry_command="schemabrain entities suggest",
                fallback_command=None,
            )
        )
        assert "skip the LLM stage" not in out
        assert "--no-enrich" not in out

    def test_retry_hint_differs_by_kind(self) -> None:
        # Kind-specific hint is rendered as the inline `# ...` comment
        # next to the retry command.
        out_over = _render(self._default_call(kind="overloaded"))
        out_conn = _render(self._default_call(kind="connection"))
        assert "30-60s" in out_over
        assert "30-60s" not in out_conn
        assert "network" in out_conn

    def test_next_step_breadcrumb_defaults_to_status_page(self) -> None:
        out = _render(self._default_call())
        assert "status.anthropic.com" in out

    def test_next_step_breadcrumb_suppressed_when_none(self) -> None:
        out = _render(self._default_call(next_step=None))
        assert "status.anthropic.com" not in out

    def test_renders_at_narrow_width_without_crashing(self) -> None:
        out = _render(self._default_call(), width=80)
        assert "◆ error" in out
        assert "Anthropic is overloaded" in out


class TestClassifyLlmFailure:
    """`classify_llm_failure` maps SDK exception classes to kind tokens."""

    def test_overloaded_error_classifies_as_overloaded(self) -> None:
        import anthropic

        # SDK 0.30+ ships `OverloadedError`. Construct via the base
        # `APIStatusError` signature when the dedicated class is
        # available; fall back to the attribute-based 529 path.
        overloaded_cls = getattr(anthropic, "OverloadedError", None)
        if overloaded_cls is None:
            pytest.skip("anthropic SDK does not expose OverloadedError")
        exc = overloaded_cls.__new__(overloaded_cls)
        exc.status_code = 529  # type: ignore[attr-defined]
        assert classify_llm_failure(exc) == "overloaded"

    def test_rate_limit_error_classifies_as_rate_limited(self) -> None:
        import anthropic

        exc = anthropic.RateLimitError.__new__(anthropic.RateLimitError)
        exc.status_code = 429  # type: ignore[attr-defined]
        assert classify_llm_failure(exc) == "rate_limited"

    def test_api_connection_error_classifies_as_connection(self) -> None:
        import anthropic

        exc = anthropic.APIConnectionError.__new__(anthropic.APIConnectionError)
        assert classify_llm_failure(exc) == "connection"

    def test_bare_api_error_classifies_as_api_error(self) -> None:
        import anthropic

        # Use APIStatusError with a non-529 / non-429 status — the
        # branch that returns "api_error" from the APIStatusError
        # arm of the classifier.
        exc = anthropic.APIStatusError.__new__(anthropic.APIStatusError)
        exc.status_code = 500  # type: ignore[attr-defined]
        assert classify_llm_failure(exc) == "api_error"

    def test_non_sdk_exception_returns_none(self) -> None:
        # Local programming bugs / RuntimeError from `_extract_text`
        # must NOT classify — the caller propagates them as-is so
        # the user sees the traceback for a real bug.
        assert classify_llm_failure(RuntimeError("max_tokens reached")) is None
        assert classify_llm_failure(ValueError("bad input")) is None

    def test_overloaded_detected_via_status_when_class_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # SDK-version compat path: when `OverloadedError` isn't on
        # the anthropic module, the classifier should still flag 529s
        # via `status_code` inspection. Simulate by hiding the
        # attribute and constructing a base `APIStatusError`.
        import anthropic

        monkeypatch.delattr(anthropic, "OverloadedError", raising=False)
        exc = anthropic.APIStatusError.__new__(anthropic.APIStatusError)
        exc.status_code = 529  # type: ignore[attr-defined]
        assert classify_llm_failure(exc) == "overloaded"


class TestCauseFromLlmError:
    """Round-1 fold M3: `cause_from_llm_error` extracts a one-line
    cause string from an Anthropic SDK exception. Lifted from
    `cli._try_render_llm_failure` so the `getattr(exc, "message", ...)`
    untyped access lives next to `classify_llm_failure` — single
    owner, single fallback chain.
    """

    def test_prefers_anthropic_message_field(self) -> None:
        # When the SDK exception carries a `.message` attribute,
        # use it verbatim (it's the server-side detail).
        import anthropic

        exc = anthropic.APIStatusError.__new__(anthropic.APIStatusError)
        exc.message = "Overloaded by model traffic"
        assert cause_from_llm_error(exc) == "Overloaded by model traffic"

    def test_falls_back_to_str_when_message_absent(self) -> None:
        # Custom subclasses / non-SDK Python exceptions may not have
        # a `.message` attribute. `str(exc)` is the next-best signal.
        exc = RuntimeError("connection reset")
        assert cause_from_llm_error(exc) == "connection reset"

    def test_falls_back_to_type_name_when_str_is_empty(self) -> None:
        # Bare `Exception()` has empty str. The type name is the
        # last fallback so the operator still sees a signal.
        exc = RuntimeError()
        assert cause_from_llm_error(exc) == "RuntimeError"

    def test_skips_empty_message_attribute(self) -> None:
        # `getattr(exc, "message", None) or ...` — falsy message
        # falls through to the next step in the chain.
        import anthropic

        exc = anthropic.APIStatusError.__new__(anthropic.APIStatusError)
        exc.message = ""
        exc.args = ("server temporarily unavailable",)
        assert cause_from_llm_error(exc) == "server temporarily unavailable"


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
        pytest.param(
            lambda console: render_llm_failure(
                kind="overloaded",
                retry_command="schemabrain index",
                fallback_command="schemabrain index --no-enrich",
                cause="HTTP 529",
                console=console,
            ),
            id="shape_c_llm_failure",
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
