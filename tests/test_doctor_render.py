"""Pins the layout contract of ``schemabrain.setup.doctor_render``.

The doctor surface is the first operator-visible surface migrated
after the wizard (PR #73). These tests pin:

* The ordinal column zero-pads to two digits and right-aligns.
* The ``_DOCTOR_STATUS_TO_TIER`` translation contract
  (``pass → ok``, ``warn → warn``, ``fail → err``).
* The brand line carries ``◆`` + ``environment`` + ``N / M healthy``.
* The progress rule renders with the ``checks`` label + optional
  elapsed metadata.
* The footer line renders four counts in design vocabulary
  (``N checks · A ok · B warn · C err``).
* The ``→ fix:`` sub-line renders only when ``suggested_next`` is set.

Tests use Rich's recording console so the rendered string can be
inspected without a real TTY. ``force_terminal=True`` + ``width=120``
makes Rich emit the same widget shape it would in production.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from schemabrain.setup.checks import Check, CheckResult
from schemabrain.setup.doctor_render import (
    _DOCTOR_STATUS_TO_TIER,
    _short_cwd,
    _short_hostname,
    _short_os_label,
    render_doctor,
)


def _render(result: CheckResult, *, elapsed_ms: int | None = None, width: int = 120) -> str:
    """Helper — render to an in-memory buffer at the design's width."""
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, width=width, no_color=True)
    render_doctor(result, console=console, elapsed_ms=elapsed_ms)
    return buf.getvalue()


class TestDoctorStatusToTier:
    """Pins the per-surface translation map's exact contract.

    Pinning at the dict level — not just an assertion against rendered
    output — makes a regression visible at the unit level even if a
    downstream renderer change masks the misroute on screen.
    """

    def test_pass_routes_to_ok(self) -> None:
        assert _DOCTOR_STATUS_TO_TIER["pass"] == "ok"

    def test_warn_routes_to_warn(self) -> None:
        assert _DOCTOR_STATUS_TO_TIER["warn"] == "warn"

    def test_fail_routes_to_err(self) -> None:
        assert _DOCTOR_STATUS_TO_TIER["fail"] == "err"

    def test_only_doctor_outcomes_are_translated(self) -> None:
        # Doctor outcomes are a closed set in ``setup.checks``; this
        # map intentionally does not carry aliases for other surfaces.
        assert set(_DOCTOR_STATUS_TO_TIER.keys()) == {"pass", "warn", "fail"}

    def test_unknown_outcome_raises_loudly(self) -> None:
        # Vocabulary drift between ``CheckOutcome`` and
        # ``_DOCTOR_STATUS_TO_TIER`` must surface visibly, not get
        # silently routed to ``err``. Construct a Check directly with
        # ``object.__setattr__`` to bypass ``Check.__post_init__``'s
        # outcome validation — the renderer's defensive raise is the
        # last line of defence against vocabulary drift introduced by
        # a refactor that loosens the upstream validation.
        check = Check(name="x", outcome="pass", message="ok")
        object.__setattr__(check, "outcome", "unknown_tier")
        result = CheckResult(checks=(check,))
        with pytest.raises(ValueError, match="Unknown doctor outcome"):
            _render(result)


class TestOrdinalColumn:
    def test_first_check_renders_with_zero_padded_ordinal(self) -> None:
        result = CheckResult(checks=(Check(name="alpha", outcome="pass", message="ok"),))
        out = _render(result)
        assert "01" in out

    def test_double_digit_ordinal_when_more_than_nine_checks(self) -> None:
        checks = tuple(Check(name=f"check_{i}", outcome="pass", message="ok") for i in range(12))
        result = CheckResult(checks=checks)
        out = _render(result)
        # 10, 11, 12 all render zero-padded already (i.e. as 10 / 11 / 12).
        assert "10" in out
        assert "12" in out


class TestBrandLine:
    def test_brand_glyph_present(self) -> None:
        result = CheckResult(checks=(Check(name="a", outcome="pass", message="ok"),))
        out = _render(result)
        assert "◆" in out

    def test_environment_word_present(self) -> None:
        result = CheckResult(checks=(Check(name="a", outcome="pass", message="ok"),))
        out = _render(result)
        assert "environment" in out

    def test_healthy_ratio_present(self) -> None:
        result = CheckResult(
            checks=(
                Check(name="a", outcome="pass", message="ok"),
                Check(name="b", outcome="pass", message="ok"),
                Check(name="c", outcome="warn", message="watch"),
            )
        )
        out = _render(result)
        # 2 of 3 checks pass — the healthy ratio reads ``2 / 3 healthy``.
        assert "2 / 3 healthy" in out

    def test_healthy_ratio_zero_over_zero_for_empty_result(self) -> None:
        out = _render(CheckResult(checks=()))
        assert "0 / 0 healthy" in out


class TestProgressRule:
    def test_singular_check_label(self) -> None:
        result = CheckResult(checks=(Check(name="a", outcome="pass", message="ok"),))
        out = _render(result)
        assert "1 check" in out
        # Make sure we didn't accidentally write ``1 checks``.
        assert "1 checks" not in out

    def test_plural_checks_label(self) -> None:
        result = CheckResult(
            checks=(
                Check(name="a", outcome="pass", message="ok"),
                Check(name="b", outcome="pass", message="ok"),
            )
        )
        out = _render(result)
        assert "2 checks" in out

    def test_elapsed_ms_renders_when_provided(self) -> None:
        result = CheckResult(checks=(Check(name="a", outcome="pass", message="ok"),))
        out = _render(result, elapsed_ms=320)
        assert "320 ms" in out

    def test_elapsed_ms_omitted_when_none(self) -> None:
        result = CheckResult(checks=(Check(name="a", outcome="pass", message="ok"),))
        out = _render(result, elapsed_ms=None)
        # No bogus ``None ms`` cell when the caller doesn't measure.
        assert "None ms" not in out


class TestFooterLine:
    def test_footer_renders_four_counts(self) -> None:
        result = CheckResult(
            checks=(
                Check(name="a", outcome="pass", message="ok"),
                Check(name="b", outcome="pass", message="ok"),
                Check(name="c", outcome="warn", message="watch"),
                Check(name="d", outcome="fail", message="bad"),
            )
        )
        out = _render(result)
        assert "4 checks" in out
        assert "2 ok" in out
        assert "1 warn" in out
        assert "1 err" in out


class TestFixSubline:
    def test_fix_prefix_only_when_suggested_next(self) -> None:
        # Pass check carries no suggested_next — no fix line.
        result_pass = CheckResult(checks=(Check(name="a", outcome="pass", message="ok"),))
        out_pass = _render(result_pass)
        assert "fix:" not in out_pass

        # Warn check WITH suggested_next renders the fix sub-line.
        result_warn = CheckResult(
            checks=(
                Check(
                    name="b",
                    outcome="warn",
                    message="watch",
                    suggested_next="run `schemabrain init`",
                ),
            )
        )
        out_warn = _render(result_warn)
        assert "→ fix:" in out_warn
        assert "schemabrain init" in out_warn

    def test_arrow_glyph_in_fix_line(self) -> None:
        result = CheckResult(
            checks=(
                Check(
                    name="b",
                    outcome="warn",
                    message="watch",
                    suggested_next="re-run init",
                ),
            )
        )
        out = _render(result)
        assert "→" in out


class TestEnvironmentHelpers:
    """The brand-line composer reads cwd / host / OS through small
    helpers. Pinning these in isolation keeps the brand-line tests
    above robust to per-machine variation (each test machine has a
    different cwd / hostname / OS).
    """

    def test_short_cwd_returns_a_string(self) -> None:
        cwd = _short_cwd()
        assert isinstance(cwd, str)
        assert cwd  # non-empty

    def test_short_hostname_strips_dot_suffix(self) -> None:
        # Pin the contract — first dot onward is stripped — rather
        # than asserting "no dot in result". An IPv4 hostname
        # (``10.0.0.1``) on a Docker-based CI runner would falsely
        # fail the looser assertion even though the helper is doing
        # exactly what it claims.
        host = _short_hostname()
        import socket as _socket

        raw = _socket.gethostname()
        assert host == raw.split(".")[0]

    def test_short_os_label_contains_system_name(self) -> None:
        import platform

        label = _short_os_label()
        assert platform.system().lower() in label

    def test_short_os_label_degrades_when_release_is_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Some Linux container images return ``platform.release() == ""``.
        # The helper should still return a non-empty label (just the
        # system name) rather than a trailing-space oddity.
        import platform as _platform

        monkeypatch.setattr(_platform, "release", lambda: "")
        label = _short_os_label()
        assert label  # non-empty
        assert label == _platform.system().lower()


class TestEmptyResultRender:
    def test_renders_brand_line_with_zero_total(self) -> None:
        out = _render(CheckResult(checks=()))
        assert "◆" in out
        assert "0 / 0 healthy" in out

    def test_renders_footer_with_zero_counts(self) -> None:
        out = _render(CheckResult(checks=()))
        assert "0 checks" in out
        assert "0 ok" in out
        assert "0 warn" in out
        assert "0 err" in out


@pytest.mark.parametrize("width", [80, 100, 120, 160])
class TestWidthAdaptivity:
    def test_render_does_not_crash_at_various_widths(self, width: int) -> None:
        result = CheckResult(
            checks=(
                Check(name="a", outcome="pass", message="ok"),
                Check(name="b", outcome="warn", message="watch", suggested_next="do x"),
            )
        )
        out = _render(result, elapsed_ms=42, width=width)
        # Hard-soft cap at 120; at narrow widths the grid still emits
        # the brand line and footer without raising.
        assert "◆" in out
        assert "42 ms" in out
