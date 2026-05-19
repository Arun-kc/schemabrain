"""Tests for `schemabrain._ui` — the shared CLI shell vocabulary.

These pin the *contract* the three threaded callers (`cli_ui.py`,
`check/render.py`, `inspect/render.py`) rely on. The visual surfaces
have their own snapshot-style tests in `test_cli_ui.py`,
`test_check_render.py`, and `test_inspect_render.py`; this module
focuses on the primitives themselves and on the zero-behavior-change
guarantee:

* `severity_glyph(def_kind)` returns the same tuples the previous
  hand-rolled `_DRIFT_GLYPH` dict did, including the unknown-kind
  fall-through to the entity-tier hard break.
* `pii_marker(sensitivity)` returns the same Rich-markup strings
  the previous hand-rolled `_PII_GLYPH` dict did, including the
  unknown-tier verbatim pass-through.
* `make_console(...)` returns a Console with the same default shape
  every threaded caller previously got from `Console(stderr=True)`.
* `NO_COLOR=1` is honoured by the factory's product (delegated to
  Rich's built-in env handling).
"""

from __future__ import annotations

import io
import sys

import pytest
from rich.console import Console

from schemabrain._ui import (
    GLYPH_ERR,
    GLYPH_OK,
    GLYPH_WARN,
    make_console,
    pii_marker,
    severity_glyph,
)


class TestSeverityGlyph:
    """Pins the severity tier map for `schemabrain check` (and any
    future caller, e.g. `doctor`, that wants the same vocabulary).

    The previous hand-rolled `_DRIFT_GLYPH` in `check/render.py`
    returned the same three tuples. These tests are the contract
    snapshot — flipping any tuple here is a deliberate, visible
    change to operator output.
    """

    def test_entity_drift_is_hard_break(self) -> None:
        # Entity drift takes the whole entity offline — operator
        # must see the hard ✗ red tier, not the softer ⚠ yellow.
        assert severity_glyph("entity") == ("✗", "red")

    def test_metric_drift_is_advisory(self) -> None:
        # Metric drift degrades one definition without blocking the
        # rest of the semantic layer — yellow ⚠.
        assert severity_glyph("metric") == ("⚠", "yellow")

    def test_canonical_join_drift_is_advisory(self) -> None:
        assert severity_glyph("canonical_join") == ("⚠", "yellow")

    def test_unknown_def_kind_falls_back_to_hard_break(self) -> None:
        # An unclassified `def_kind` slipping through indicates a
        # routing gap — surfacing it as ✗ red rather than silently
        # painting a benign warning is intentional.
        assert severity_glyph("brand_new_kind_2027") == ("✗", "red")
        assert severity_glyph("") == ("✗", "red")


class TestGlyphConstants:
    """The exported glyph constants are part of the public contract.

    Callers that compose multi-line output (e.g. `check/render.py`'s
    healthy summary line, which hardcodes `✓`) should be able to
    reference these without redefining the character at the call
    site.
    """

    def test_severity_glyph_constants(self) -> None:
        assert GLYPH_OK == "✓"
        assert GLYPH_WARN == "⚠"
        assert GLYPH_ERR == "✗"


class TestPIIMarker:
    """Pins the PII sensitivity label vocabulary for
    `schemabrain inspect` columns.

    The previous hand-rolled `_PII_GLYPH` dict in `inspect/render.py`
    returned identical Rich-markup strings; these tests fence that
    contract so a future caller (`doctor`'s host-config readout,
    `audit`'s PII column flag) gets the same labels.
    """

    @pytest.mark.parametrize(
        ("sensitivity", "expected"),
        [
            ("public", "[dim]public[/]"),
            ("internal", "[yellow]internal[/]"),
            ("confidential", "[red]confidential[/]"),
            ("pii", "[red]pii[/]"),
        ],
    )
    def test_known_tiers_render_with_severity_markup(self, sensitivity: str, expected: str) -> None:
        assert pii_marker(sensitivity) == expected

    def test_unknown_sensitivity_passes_through_verbatim(self) -> None:
        # An indexer change that introduces a new sensitivity tier
        # MUST surface in operator output rather than disappearing.
        # The renderer trusts the indexer's vocabulary — see
        # `_ui.pii_marker` docstring.
        assert pii_marker("ultra_secret_2027") == "ultra_secret_2027"

    def test_empty_string_passes_through(self) -> None:
        # The indexer occasionally writes empty when a tier hasn't
        # been classified yet. Verbatim pass-through keeps that
        # signal visible in inspect output.
        assert pii_marker("") == ""


class TestMakeConsole:
    """Pins the Console factory contract.

    The factory is the single hook every CLI surface should resolve
    through. These tests fence the keyword-only API and the
    pass-through behaviour for the kwargs threaded callers use today.
    """

    def test_returns_a_rich_console(self) -> None:
        assert isinstance(make_console(), Console)

    def test_default_targets_stdout(self) -> None:
        # `make_console()` with no kwargs preserves the default Rich
        # behaviour: writes to sys.stdout. The `stderr=True` opt-in
        # is required at every caller, matching the previous
        # `Console(stderr=True)` shape used in `cli_ui.py`.
        console = make_console()
        assert console.file is sys.stdout

    def test_stderr_keyword_directs_to_stderr(self) -> None:
        console = make_console(stderr=True)
        assert console.file is sys.stderr

    def test_record_keyword_enables_capture(self) -> None:
        # Tests rely on `record=True` to assert against rendered
        # output. Fence the pass-through so a future signature
        # change can't quietly drop the kwarg.
        console = make_console(record=True)
        assert console.record is True

    def test_file_keyword_overrides_stdout(self) -> None:
        # The Rich test idiom — `Console(file=StringIO(), ...)` —
        # is preserved through the factory.
        buf = io.StringIO()
        console = make_console(file=buf, force_terminal=True, width=120)
        console.print("hello")
        assert "hello" in buf.getvalue()

    def test_no_color_env_var_disables_color(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Industry-standard NO_COLOR (no-color.org) MUST disable
        # colour without losing structure or glyphs. Rich reads the
        # env var on Console construction; the factory must not
        # subvert that contract.
        monkeypatch.setenv("NO_COLOR", "1")
        console = make_console()
        assert console.no_color is True

    def test_no_color_unset_keeps_color_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Symmetric counter-test: unset NO_COLOR must not flip the
        # factory's product into no-color mode by accident.
        monkeypatch.delenv("NO_COLOR", raising=False)
        console = make_console()
        assert console.no_color is False
