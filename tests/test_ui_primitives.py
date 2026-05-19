"""Tests for `schemabrain._ui` — the shared CLI shell vocabulary.

These pin the *contract* every threaded caller (`cli_ui.py`,
`check/render.py`, `inspect/render.py`, and the wizard / doctor
surfaces that will migrate next) relies on. The visual surfaces
have their own snapshot-style tests in `test_cli_ui.py`,
`test_check_render.py`, and `test_inspect_render.py`; this module
focuses on the primitives themselves:

* `drift_glyph(def_kind)` routes a definition-kind noun (`entity` /
  `metric` / `canonical_join`) to its `(glyph, rich_style)` tuple,
  with an unknown-kind fall-through to the entity-tier hard break.
* `status_glyph(status_name)` routes the general operator-status
  tier vocabulary (`ok` / `warn` / `err` / `active` / `pending` /
  `skipped`) to its tuple — the wizard's per-stage outcomes,
  `doctor`'s per-check outcomes, and `tail`'s per-event severity
  all resolve through this single helper.
* `pii_marker(sensitivity)` returns Rich-markup-tagged labels for
  the four PII tiers, with verbatim pass-through for unknown tiers.
* `make_console(...)` returns a Console with the same default shape
  every threaded caller previously got from `Console(stderr=True)`,
  honouring `NO_COLOR=1` via Rich's built-in env contract.
* The exported glyph constants (`GLYPH_OK` through `GLYPH_SEP`) are
  the single source of truth — callers reach for the constant
  rather than redefining the character at the call site.
"""

from __future__ import annotations

import io
import sys

import pytest
from rich.console import Console

from schemabrain._ui import (
    GLYPH_ACTIVE,
    GLYPH_ARROW,
    GLYPH_BRAND,
    GLYPH_BULLET,
    GLYPH_ERR,
    GLYPH_OK,
    GLYPH_PENDING,
    GLYPH_SEP,
    GLYPH_SKIPPED,
    GLYPH_WARN,
    drift_glyph,
    make_console,
    pii_marker,
    status_glyph,
)


class TestDriftGlyph:
    """Pins the drift-severity tier map for `schemabrain check`.

    Input is a `def_kind` noun (`entity` / `metric` /
    `canonical_join`). These tests are the contract snapshot —
    flipping any tuple here is a deliberate, visible change to
    operator output.
    """

    def test_entity_drift_is_hard_break(self) -> None:
        # Entity drift takes the whole entity offline — operator
        # must see the hard ✗ red tier, not the softer ⚠ yellow.
        assert drift_glyph("entity") == ("✗", "red")

    def test_metric_drift_is_advisory(self) -> None:
        # Metric drift degrades one definition without blocking the
        # rest of the semantic layer — yellow ⚠.
        assert drift_glyph("metric") == ("⚠", "yellow")

    def test_canonical_join_drift_is_advisory(self) -> None:
        assert drift_glyph("canonical_join") == ("⚠", "yellow")

    def test_unknown_def_kind_falls_back_to_hard_break(self) -> None:
        # An unclassified `def_kind` slipping through indicates a
        # routing gap — surfacing it as ✗ red rather than silently
        # painting a benign warning is intentional.
        assert drift_glyph("brand_new_kind_2027") == ("✗", "red")
        assert drift_glyph("") == ("✗", "red")


class TestStatusGlyph:
    """Pins the general operator-status tier map.

    Input is a tier name (`ok` / `warn` / `err` / `active` /
    `pending` / `skipped`). The wizard, `doctor`, and `tail` will
    all migrate from local glyph dicts onto this single helper —
    these tests fence the vocabulary so a follow-up surface
    migration can't silently flip a glyph or colour.

    Note: PR #2 ships the primitive only; the local dicts in
    `schemabrain/setup/doctor_flow.py:_GLYPHS` and
    `schemabrain/cli.py:_STAGE_GLYPHS` are migrated when the
    wizard / doctor surfaces are re-rendered (visible glyph flip
    for `skipped`: current `↷` → design-spec `⊘`).
    """

    @pytest.mark.parametrize(
        ("status_name", "expected_glyph", "expected_style"),
        [
            ("ok", "✓", "green"),
            ("warn", "⚠", "yellow"),
            ("err", "✗", "red"),
            # `active` uses cyan rather than green so an in-progress
            # row stays visually distinct from a completed one — the
            # design specifies lime, Rich's named-palette floor is
            # cyan until truecolor.
            ("active", "▸", "cyan"),
            ("pending", "◇", "bright_black"),
            ("skipped", "⊘", "yellow"),
        ],
    )
    def test_known_tiers_route_through_status_glyph(
        self,
        status_name: str,
        expected_glyph: str,
        expected_style: str,
    ) -> None:
        assert status_glyph(status_name) == (expected_glyph, expected_style)

    def test_unknown_status_falls_back_to_hard_break(self) -> None:
        # Mirrors `drift_glyph`'s contract: a renderer reaching for
        # a tier that isn't routed yet should surface visibly.
        assert status_glyph("brand_new_tier_2027") == ("✗", "red")
        assert status_glyph("") == ("✗", "red")


class TestGlyphConstants:
    """The exported glyph constants are the single source of truth.

    Callers that compose multi-line output (e.g. `check/render.py`'s
    healthy summary line, which references `GLYPH_OK`) should be
    able to import the constant rather than embedding the character
    at the call site. The wizard's stage rows, `doctor`'s severity
    column, and the error renderers will all consume from this
    list.
    """

    def test_drift_glyph_constants(self) -> None:
        assert GLYPH_OK == "✓"
        assert GLYPH_WARN == "⚠"
        assert GLYPH_ERR == "✗"

    def test_status_glyph_constants(self) -> None:
        # The wizard's per-stage outcomes and `doctor`'s active
        # check indicator both consume these.
        assert GLYPH_ACTIVE == "▸"
        assert GLYPH_PENDING == "◇"
        assert GLYPH_SKIPPED == "⊘"

    def test_anchor_glyph_constants(self) -> None:
        # Surface-anchoring glyphs the design uses across the
        # brand line (◆), arrow hints (→), bullet lists (•), and
        # the dot separator on metadata strips (·).
        assert GLYPH_BRAND == "◆"
        assert GLYPH_ARROW == "→"
        assert GLYPH_BULLET == "•"
        assert GLYPH_SEP == "·"


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
