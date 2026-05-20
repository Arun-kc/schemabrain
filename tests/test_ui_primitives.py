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
import os
import sys
from pathlib import Path

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
    GLYPH_RULE,
    GLYPH_SEP,
    GLYPH_SKIPPED,
    GLYPH_WARN,
    drift_glyph,
    make_console,
    pii_marker,
    status_glyph,
    top_rule,
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
        # brand line (◆), arrow hints (→), bullet lists (•),
        # the dot separator on metadata strips (·), and the
        # horizontal rule character used by ``top_rule`` (─).
        assert GLYPH_BRAND == "◆"
        assert GLYPH_ARROW == "→"
        assert GLYPH_BULLET == "•"
        assert GLYPH_SEP == "·"
        assert GLYPH_RULE == "─"


class TestTopRule:
    """Pins ``top_rule(...)`` — the design's section-header builder.

    The wizard's progress rule and (in follow-ups) the doctor's
    check-list rule both render through this primitive, so the
    contract has to fence:

    * The label is followed by spacing and a dashed run that fills
      the remaining width.
    * An optional right-aligned metadata string lands at the end of
      the line.
    * When ``label`` + ``right`` exceed the requested ``width``, the
      dashed run collapses to a 4-dash floor rather than wrapping —
      a narrow-terminal rule should stay on one line.
    """

    def test_label_plus_right_with_dashed_fill(self) -> None:
        rendered = top_rule("progress", "2 / 7 done", width=60).plain
        # Label appears with the leading 2-cell gap.
        assert rendered.startswith("  progress")
        # Right metadata appears at the end.
        assert rendered.endswith("2 / 7 done")
        # Dashed run sits between them — characters are the design's
        # box-drawing ``─`` (U+2500), not the ASCII ``-``.
        assert GLYPH_RULE in rendered
        assert "-" * 10 not in rendered

    def test_label_only_renders_dashed_tail(self) -> None:
        rendered = top_rule("7 stages", width=40).plain
        assert rendered.startswith("  7 stages")
        # Without a `right` argument, the dashed run runs to the
        # end of the requested width.
        assert rendered.rstrip().endswith(GLYPH_RULE)

    def test_total_width_matches_requested_when_content_fits(self) -> None:
        # The label + dashes + right meta should consume exactly
        # `width` cells when there's space to honour it. This pins
        # the rule's width contract so wizard / doctor surfaces
        # render their progress lines flush with the stage list
        # below.
        text = top_rule("progress", "done", width=80)
        assert len(text.plain) == 80

    def test_narrow_width_collapses_to_dash_floor_not_wrap(self) -> None:
        # 8-cell width can't fit "progress" + 2 gaps + dashes +
        # "extremely long metadata string here". The implementation
        # must collapse to the 4-dash floor rather than wrap or
        # return a negative-length string.
        rendered = top_rule(
            "progress",
            "extremely long metadata string here",
            width=8,
        ).plain
        assert "─" * 4 in rendered
        # And the line stays on one row.
        assert "\n" not in rendered

    def test_default_style_is_dim(self) -> None:
        # The whole rule reads as muted chrome so it sinks below the
        # stage list. ``bright_black`` is Rich's name for the muted
        # tone the design uses for section dividers.
        text = top_rule("progress", "done", width=60)
        # The Text constructor stores the style on the instance for
        # all subsequent appends — fence the contract so a future
        # signature change doesn't quietly flip the default.
        assert str(text.style) == "bright_black"

    def test_custom_style_overrides_default(self) -> None:
        # Caller-provided ``style`` must propagate to the whole
        # rendered band. A wizard re-render that wants a bolder
        # rule (e.g. green on success) reaches for this argument.
        text = top_rule("ok", "done", width=40, style="green")
        assert str(text.style) == "green"


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


class TestShortPath:
    """Pins the ``$HOME → ~/`` collapse contract used across design surfaces."""

    def test_collapses_home_prefix(self, tmp_path: pytest.TempPathFactory) -> None:
        from pathlib import Path

        from schemabrain._ui import short_path

        home = str(Path.home())
        result = short_path(f"{home}/.schemabrain/store.db")
        assert result == "~/.schemabrain/store.db"

    def test_returns_raw_when_outside_home(self) -> None:
        from schemabrain._ui import short_path

        # ``/tmp/...`` is outside ``$HOME`` — render verbatim.
        result = short_path("/tmp/sb_smoke.db")
        assert result == "/tmp/sb_smoke.db"

    def test_returns_empty_string_when_none(self) -> None:
        from schemabrain._ui import short_path

        # ``None`` collapses to empty so callers can concatenate
        # without a guard.
        assert short_path(None) == ""

    def test_collapses_exact_home_to_tilde(self) -> None:
        from pathlib import Path

        from schemabrain._ui import short_path

        result = short_path(str(Path.home()))
        assert result == "~"

    def test_relative_path_renders_verbatim(self) -> None:
        from schemabrain._ui import short_path

        # ``./schemabrain.db`` is relative — not inside ``$HOME``
        # via ``relative_to`` (raises ValueError) — render as-is.
        assert short_path("./schemabrain.db") == "./schemabrain.db"


class TestActiveSpinnerRegistry:
    """Tests for ``register_active_spinner`` / ``pause_active_spinner``
    — the registry the wizard uses to pause Rich's status spinner
    while ``_prompt_llm_confirmation`` blocks on ``input()``. Smoke
    2026-05-19 surfaced the spinner-during-prompt bug this fixes;
    the tests here pin the contract so a future Rich version or
    threading-model change can't silently regress it.
    """

    def test_pause_is_noop_when_no_spinner_registered(self) -> None:
        from schemabrain._ui import pause_active_spinner

        # No prior register_active_spinner; the with-block runs
        # without raising and without trying to call .stop()/.start()
        # on a non-existent status.
        with pause_active_spinner():
            pass

    def test_pause_stops_and_restarts_registered_spinner(self) -> None:
        from schemabrain._ui import pause_active_spinner, register_active_spinner

        events: list[str] = []

        class _RecordingStatus:
            def start(self) -> None:
                events.append("start")

            def stop(self) -> None:
                events.append("stop")

        status = _RecordingStatus()
        with register_active_spinner(status), pause_active_spinner():
            events.append("input")
        # The exact ordering matters — stop() must precede the
        # blocking work, start() must restore the spinner after.
        assert events == ["stop", "input", "start"]

    def test_register_restores_previous_status_on_exit(self) -> None:
        from schemabrain._ui import _active_spinner, register_active_spinner

        class _S:
            def start(self) -> None: ...
            def stop(self) -> None: ...

        outer = _S()
        inner = _S()
        with register_active_spinner(outer):
            assert _active_spinner.status is outer
            with register_active_spinner(inner):
                assert _active_spinner.status is inner
            assert _active_spinner.status is outer
        # Top-level exit clears the registry back to its prior state
        # (``None`` here because nothing was registered before outer).
        assert getattr(_active_spinner, "status", None) is None

    def test_pause_swallows_stop_exceptions_to_protect_prompt(self) -> None:
        from schemabrain._ui import pause_active_spinner, register_active_spinner

        # A Rich-impl quirk that makes .stop() raise must NOT take
        # the surrounding input() prompt down. The pause helper
        # falls through to a plain yield in that case.
        class _BrokenStatus:
            def start(self) -> None:
                raise RuntimeError("start should not run when stop failed")

            def stop(self) -> None:
                raise RuntimeError("simulated rich quirk")

        with register_active_spinner(_BrokenStatus()), pause_active_spinner():
            # If pause raised, this line wouldn't execute.
            pass

    def test_pause_swallows_start_exceptions_after_input(self) -> None:
        from schemabrain._ui import pause_active_spinner, register_active_spinner

        # Symmetric to the stop case: a broken ``.start()`` after the
        # prompt must not raise back into the wizard. The user's
        # confirm answer has already been read at that point.
        events: list[str] = []

        class _BrokenStart:
            def start(self) -> None:
                raise RuntimeError("simulated rich quirk")

            def stop(self) -> None:
                events.append("stop")

        with register_active_spinner(_BrokenStart()), pause_active_spinner():
            events.append("input")
        assert events == ["stop", "input"]


class TestPromptForUrl:
    """Tests for ``prompt_for_url`` — the interactive URL prompt the
    wizard's stage 0 and post-init commands use when ``DATABASE_URL``
    is missing in env. Verifies the helper:

    * Returns the entered URL stripped of whitespace.
    * Returns ``None`` for empty input (caller routes to abort or
      default — the helper doesn't decide policy).
    * Calls ``Prompt.ask`` with ``password=True`` so the URL never
      appears in terminal scrollback (URLs often embed passwords).
    * Pauses an active wizard spinner around the input so the user
      doesn't see "stage running" while typing.
    * Propagates ``KeyboardInterrupt`` for clean Ctrl-C abort.
    """

    def test_returns_stripped_url_on_input(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from schemabrain._ui import make_console, prompt_for_url

        captured: dict[str, object] = {}

        def fake_ask(*args: object, **kwargs: object) -> str:
            captured.update(kwargs)
            return "  postgresql://user:pw@host:5432/db  "

        monkeypatch.setattr("rich.prompt.Prompt.ask", fake_ask)
        out = prompt_for_url(make_console(file=io.StringIO()), purpose="to index")
        assert out == "postgresql://user:pw@host:5432/db"
        # ``password=True`` is load-bearing — the URL may contain
        # credentials and must not echo to the terminal.
        assert captured.get("password") is True

    def test_returns_none_on_empty_input(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from schemabrain._ui import make_console, prompt_for_url

        monkeypatch.setattr("rich.prompt.Prompt.ask", lambda *a, **kw: "")
        assert prompt_for_url(make_console(file=io.StringIO()), purpose="x") is None

    def test_returns_none_on_whitespace_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from schemabrain._ui import make_console, prompt_for_url

        monkeypatch.setattr("rich.prompt.Prompt.ask", lambda *a, **kw: "   ")
        assert prompt_for_url(make_console(file=io.StringIO()), purpose="x") is None

    def test_pauses_active_spinner(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from schemabrain._ui import (
            make_console,
            prompt_for_url,
            register_active_spinner,
        )

        events: list[str] = []

        class _RecordingStatus:
            def start(self) -> None:
                events.append("start")

            def stop(self) -> None:
                events.append("stop")

        def fake_ask(*args: object, **kwargs: object) -> str:
            events.append("ask")
            return ""

        monkeypatch.setattr("rich.prompt.Prompt.ask", fake_ask)
        with register_active_spinner(_RecordingStatus()):
            prompt_for_url(make_console(file=io.StringIO()), purpose="x")
        # Spinner must stop BEFORE Prompt.ask blocks on stdin, then
        # restart AFTER — otherwise the user sees "stage running"
        # on the same line as the prompt and reads the wizard as
        # already done, missing that they're blocking it.
        assert events == ["stop", "ask", "start"]

    def test_propagates_keyboard_interrupt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from schemabrain._ui import make_console, prompt_for_url

        def raising_ask(*args: object, **kwargs: object) -> str:
            raise KeyboardInterrupt

        monkeypatch.setattr("rich.prompt.Prompt.ask", raising_ask)
        with pytest.raises(KeyboardInterrupt):
            prompt_for_url(make_console(file=io.StringIO()), purpose="x")


class TestPromptForAnthropicKey:
    """Tests for ``prompt_for_anthropic_key`` — the API key prompt
    with cost disclosure. Verifies the helper:

    * Returns the entered key stripped, or ``None`` for empty input.
    * Renders the cost / cap / skip_hint disclosure before prompting.
    * Calls ``Prompt.ask`` with ``password=True``.
    * Pauses the active wizard spinner around the input.
    * Propagates ``KeyboardInterrupt``.
    """

    def test_returns_stripped_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from schemabrain._ui import make_console, prompt_for_anthropic_key

        monkeypatch.setattr("rich.prompt.Prompt.ask", lambda *a, **kw: "  sk-ant-xyz  ")
        out = prompt_for_anthropic_key(
            make_console(file=io.StringIO()),
            purpose="suggest entities",
            cost_estimate_usd=0.01,
            cap_usd=0.5,
        )
        assert out == "sk-ant-xyz"

    def test_returns_none_on_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from schemabrain._ui import make_console, prompt_for_anthropic_key

        monkeypatch.setattr("rich.prompt.Prompt.ask", lambda *a, **kw: "")
        assert (
            prompt_for_anthropic_key(
                make_console(file=io.StringIO()),
                purpose="x",
                cost_estimate_usd=0.01,
                cap_usd=0.5,
            )
            is None
        )

    def test_disclosure_includes_cost_cap_and_skip_hint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from schemabrain._ui import make_console, prompt_for_anthropic_key

        buf = io.StringIO()
        monkeypatch.setattr("rich.prompt.Prompt.ask", lambda *a, **kw: "")
        prompt_for_anthropic_key(
            make_console(file=buf, force_terminal=False, width=120),
            purpose="suggest entities",
            cost_estimate_usd=0.0123,
            cap_usd=0.50,
            skip_hint="press Enter to skip (degraded mode)",
        )
        out = buf.getvalue()
        # All three numbers must appear so the user understands the
        # bounds before pasting a key. Cost rounds to 2dp; cap is the
        # actual ceiling (the env-var override), not a stub.
        assert "$0.01" in out
        assert "$0.50" in out
        assert "press Enter to skip (degraded mode)" in out
        assert "suggest entities" in out

    def test_password_true_load_bearing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from schemabrain._ui import make_console, prompt_for_anthropic_key

        captured: dict[str, object] = {}

        def fake_ask(*args: object, **kwargs: object) -> str:
            captured.update(kwargs)
            return ""

        monkeypatch.setattr("rich.prompt.Prompt.ask", fake_ask)
        prompt_for_anthropic_key(
            make_console(file=io.StringIO()),
            purpose="x",
            cost_estimate_usd=0.01,
            cap_usd=0.5,
        )
        # API keys must not echo — they're as sensitive as passwords.
        assert captured.get("password") is True

    def test_pauses_active_spinner(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from schemabrain._ui import (
            make_console,
            prompt_for_anthropic_key,
            register_active_spinner,
        )

        events: list[str] = []

        class _RecordingStatus:
            def start(self) -> None:
                events.append("start")

            def stop(self) -> None:
                events.append("stop")

        def fake_ask(*args: object, **kwargs: object) -> str:
            events.append("ask")
            return ""

        monkeypatch.setattr("rich.prompt.Prompt.ask", fake_ask)
        with register_active_spinner(_RecordingStatus()):
            prompt_for_anthropic_key(
                make_console(file=io.StringIO()),
                purpose="x",
                cost_estimate_usd=0.01,
                cap_usd=0.5,
            )
        assert events == ["stop", "ask", "start"]

    def test_propagates_keyboard_interrupt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from schemabrain._ui import make_console, prompt_for_anthropic_key

        def raising_ask(*args: object, **kwargs: object) -> str:
            raise KeyboardInterrupt

        monkeypatch.setattr("rich.prompt.Prompt.ask", raising_ask)
        with pytest.raises(KeyboardInterrupt):
            prompt_for_anthropic_key(
                make_console(file=io.StringIO()),
                purpose="x",
                cost_estimate_usd=0.01,
                cap_usd=0.5,
            )


class TestOfferPersistAnthropicKeyToEnvFile:
    """D4: opt-in consent prompt + .env persistence + gitignore warning.

    Verifies the three hard contracts from the docstring:

    1. Opt-in default no — Enter without typing accepts the safe path.
    2. Never writes silently — every write produces a visible message.
    3. Gitignore warning fires when ``.env`` is not listed.
    """

    def test_writes_to_env_when_user_consents(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from schemabrain._ui import (
            make_console,
            offer_persist_anthropic_key_to_env_file,
        )

        env_path = tmp_path / ".env"
        gi = tmp_path / ".gitignore"
        gi.write_text(".env\n", encoding="utf-8")
        monkeypatch.setattr("rich.prompt.Confirm.ask", lambda *a, **kw: True)

        result = offer_persist_anthropic_key_to_env_file(
            make_console(file=io.StringIO()),
            key_value="sk-ant-secret",
            env_path=env_path,
            gitignore_path=gi,
        )

        assert result is True
        assert env_path.exists()
        assert "ANTHROPIC_API_KEY=sk-ant-secret" in env_path.read_text()

    def test_skips_write_when_user_declines(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The opt-in default no contract: if the user presses Enter
        # (or types n), the key MUST NOT land on disk. The whole
        # point of D4 is to never write a secret without explicit
        # consent.
        from schemabrain._ui import (
            make_console,
            offer_persist_anthropic_key_to_env_file,
        )

        env_path = tmp_path / ".env"
        gi = tmp_path / ".gitignore"
        gi.write_text(".env\n", encoding="utf-8")
        monkeypatch.setattr("rich.prompt.Confirm.ask", lambda *a, **kw: False)

        result = offer_persist_anthropic_key_to_env_file(
            make_console(file=io.StringIO()),
            key_value="sk-ant-secret",
            env_path=env_path,
            gitignore_path=gi,
        )

        assert result is False
        assert not env_path.exists()

    def test_confirm_default_is_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The Confirm.ask call MUST pass `default=False` so a
        # blank-Enter declines the save. A `default=True` would mean
        # an operator who just wanted to dismiss the prompt
        # accidentally writes the secret to disk.
        from schemabrain._ui import (
            make_console,
            offer_persist_anthropic_key_to_env_file,
        )

        captured: dict[str, object] = {}

        def fake_confirm(*args: object, **kwargs: object) -> bool:
            captured.update(kwargs)
            return False

        gi = tmp_path / ".gitignore"
        gi.write_text(".env\n", encoding="utf-8")
        monkeypatch.setattr("rich.prompt.Confirm.ask", fake_confirm)

        offer_persist_anthropic_key_to_env_file(
            make_console(file=io.StringIO()),
            key_value="sk-ant-secret",
            env_path=tmp_path / ".env",
            gitignore_path=gi,
        )

        assert captured.get("default") is False

    def test_warns_when_env_not_in_gitignore(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from schemabrain._ui import (
            make_console,
            offer_persist_anthropic_key_to_env_file,
        )

        buf = io.StringIO()
        # No .gitignore at all — warning must surface.
        monkeypatch.setattr("rich.prompt.Confirm.ask", lambda *a, **kw: False)

        offer_persist_anthropic_key_to_env_file(
            make_console(file=buf, force_terminal=False, width=120),
            key_value="sk-ant-secret",
            env_path=tmp_path / ".env",
            gitignore_path=tmp_path / ".gitignore",
        )

        out = buf.getvalue()
        # Operator must see the leak-risk line before the prompt fires.
        # Round-2 fold MED: when .gitignore is missing entirely, the
        # warning wording changed from "NOT listed in .gitignore"
        # (literally wrong) to "no .gitignore found" (honest).
        assert ".env" in out
        assert ".gitignore" in out
        assert "no .gitignore found" in out

    def test_no_warning_when_env_is_in_gitignore(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from schemabrain._ui import (
            make_console,
            offer_persist_anthropic_key_to_env_file,
        )

        buf = io.StringIO()
        gi = tmp_path / ".gitignore"
        gi.write_text(".env\n", encoding="utf-8")
        monkeypatch.setattr("rich.prompt.Confirm.ask", lambda *a, **kw: False)

        offer_persist_anthropic_key_to_env_file(
            make_console(file=buf, force_terminal=False, width=120),
            key_value="sk-ant-secret",
            env_path=tmp_path / ".env",
            gitignore_path=gi,
        )

        out = buf.getvalue()
        # No leak warning — the operator's repo is already configured
        # to ignore .env. (The prompt itself still renders.)
        assert "NOT" not in out

    def test_seeds_env_from_template_when_env_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # D4-fix: when `.env` doesn't exist but `.env.example` does,
        # the persist flow seeds .env from the template FIRST so
        # the new file inherits the template's comments + placeholder
        # rows for other keys. Without the seed, the new .env had
        # only the one key — surprising for operators who expect
        # .env to be the full project config dump.
        from schemabrain._ui import (
            make_console,
            offer_persist_anthropic_key_to_env_file,
        )

        env_path = tmp_path / ".env"
        template = tmp_path / ".env.example"
        template.write_text(
            "# Postgres URL\nDATABASE_URL=\n\n# API key\nANTHROPIC_API_KEY=\n",
            encoding="utf-8",
        )
        gi = tmp_path / ".gitignore"
        gi.write_text(".env\n", encoding="utf-8")
        monkeypatch.setattr("rich.prompt.Confirm.ask", lambda *a, **kw: True)

        offer_persist_anthropic_key_to_env_file(
            make_console(file=io.StringIO()),
            key_value="sk-ant-secret",
            env_path=env_path,
            gitignore_path=gi,
        )

        body = env_path.read_text(encoding="utf-8")
        # Template comments + other placeholder rows survived.
        assert "# Postgres URL" in body
        assert "DATABASE_URL=" in body
        assert "# API key" in body
        # The API key line was replaced in place — not appended at end.
        assert "ANTHROPIC_API_KEY=sk-ant-secret" in body
        # Exactly one ANTHROPIC_API_KEY entry (in-place replace, no duplicate).
        assert body.count("ANTHROPIC_API_KEY=") == 1

    def test_no_template_seed_when_env_already_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # If .env already exists, the persist flow MUST NOT clobber
        # it with the template — the operator's existing entries
        # are load-bearing.
        from schemabrain._ui import (
            make_console,
            offer_persist_anthropic_key_to_env_file,
        )

        env_path = tmp_path / ".env"
        env_path.write_text("CUSTOM_VAR=keep_me\n", encoding="utf-8")
        template = tmp_path / ".env.example"
        template.write_text("TEMPLATE_VAR=should_not_appear\n", encoding="utf-8")
        gi = tmp_path / ".gitignore"
        gi.write_text(".env\n", encoding="utf-8")
        monkeypatch.setattr("rich.prompt.Confirm.ask", lambda *a, **kw: True)

        offer_persist_anthropic_key_to_env_file(
            make_console(file=io.StringIO()),
            key_value="sk-ant-secret",
            env_path=env_path,
            gitignore_path=gi,
        )

        body = env_path.read_text(encoding="utf-8")
        # Existing entry preserved.
        assert "CUSTOM_VAR=keep_me" in body
        # Template's vars NEVER pulled in (would overwrite operator's work).
        assert "TEMPLATE_VAR" not in body
        # Key was appended, not seeded from template.
        assert "ANTHROPIC_API_KEY=sk-ant-secret" in body

    def test_template_seed_writes_at_0o600_not_0o644(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Round-2 fold CRITICAL (python-reviewer): pre-fold,
        # `shutil.copy(template, env)` preserved the template's
        # `0o644` mode bits; the subsequent `persist_key_to_env_file`
        # read that mode back in and preserved it — the API key
        # landed group/world-readable. The fix `env_path.chmod(0o600)`
        # immediately after the copy now enforces owner-only.
        if os.name != "posix":
            pytest.skip("chmod bits not honored the same way on Windows")
        from schemabrain._ui import (
            make_console,
            offer_persist_anthropic_key_to_env_file,
        )

        env_path = tmp_path / ".env"
        template = tmp_path / ".env.example"
        template.write_text(
            "DATABASE_URL=\nANTHROPIC_API_KEY=\n",
            encoding="utf-8",
        )
        template.chmod(0o644)  # the world-readable mode this test guards against
        gi = tmp_path / ".gitignore"
        gi.write_text(".env\n", encoding="utf-8")
        monkeypatch.setattr("rich.prompt.Confirm.ask", lambda *a, **kw: True)

        offer_persist_anthropic_key_to_env_file(
            make_console(file=io.StringIO()),
            key_value="sk-ant-secret",
            env_path=env_path,
            gitignore_path=gi,
        )

        mode = env_path.stat().st_mode & 0o777
        # No group/world bits. Owner-only.
        assert mode & 0o077 == 0, (
            f"API key written at mode {oct(mode)} — group/world can read the secret"
        )

    def test_warning_distinguishes_missing_gitignore_from_unlisted_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Round-2 fold MED (silent-failure-hunter): pre-fold, the
        # warning text said ".env is NOT listed in .gitignore" even
        # when no .gitignore existed at all — alarmist and literally
        # wrong. Fix branches the wording.
        from schemabrain._ui import (
            make_console,
            offer_persist_anthropic_key_to_env_file,
        )

        buf = io.StringIO()
        gi_missing = tmp_path / ".gitignore"  # does NOT exist
        monkeypatch.setattr("rich.prompt.Confirm.ask", lambda *a, **kw: False)

        offer_persist_anthropic_key_to_env_file(
            make_console(file=buf, force_terminal=False, width=120),
            key_value="sk-ant-secret",
            env_path=tmp_path / ".env",
            gitignore_path=gi_missing,
        )

        out = buf.getvalue()
        # Honest copy when no .gitignore exists.
        assert "no .gitignore found" in out
        # Make sure the alarmist "NOT listed in" wording is NOT used here.
        assert "NOT listed in" not in out

    def test_no_template_seed_when_template_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No .env.example present → no seed message, just the
        # one-key file. The seed is an enhancement, not a precondition.
        from schemabrain._ui import (
            make_console,
            offer_persist_anthropic_key_to_env_file,
        )

        env_path = tmp_path / ".env"
        gi = tmp_path / ".gitignore"
        gi.write_text(".env\n", encoding="utf-8")
        monkeypatch.setattr("rich.prompt.Confirm.ask", lambda *a, **kw: True)
        buf = io.StringIO()

        offer_persist_anthropic_key_to_env_file(
            make_console(file=buf, force_terminal=False, width=120),
            key_value="sk-ant-secret",
            env_path=env_path,
            gitignore_path=gi,
        )

        body = env_path.read_text(encoding="utf-8")
        assert body == "ANTHROPIC_API_KEY=sk-ant-secret\n"
        # No "seeded from" line in the user-facing output.
        assert "seeded from" not in buf.getvalue()

    def test_success_message_printed_on_persist(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The "never write silently" contract: every successful
        # write produces a visible confirmation so the operator
        # knows their key is on disk now.
        from schemabrain._ui import (
            make_console,
            offer_persist_anthropic_key_to_env_file,
        )

        buf = io.StringIO()
        env_path = tmp_path / ".env"
        gi = tmp_path / ".gitignore"
        gi.write_text(".env\n", encoding="utf-8")
        monkeypatch.setattr("rich.prompt.Confirm.ask", lambda *a, **kw: True)

        offer_persist_anthropic_key_to_env_file(
            make_console(file=buf, force_terminal=False, width=120),
            key_value="sk-ant-secret",
            env_path=env_path,
            gitignore_path=gi,
        )

        out = buf.getvalue()
        assert "saved to" in out
        # The path may soft-wrap inside Rich's width budget — assert
        # on the path after collapsing whitespace so the basename
        # match survives mid-word wraps.
        collapsed = "".join(out.split())
        assert env_path.name in collapsed


class TestPrintLlmStagePreamble:
    """Tests for ``print_llm_stage_preamble`` — the cost-preview
    line that prints inside the wizard's entity / metric suggestion
    stages BEFORE the ~30s LLM call goes out. Without this line, the
    wizard's spinner reads as "stuck" because the user has no
    visibility into what's about to happen or how much it'll cost.

    The line is informational only — the actual cost gating is done
    by ``CostCeilingGuard``. The cap_usd shown here must match the
    real ceiling so the user isn't misled.
    """

    def test_renders_action_model_cost_and_cap(self) -> None:
        from schemabrain._ui import make_console, print_llm_stage_preamble

        buf = io.StringIO()
        print_llm_stage_preamble(
            make_console(file=buf, force_terminal=False, width=120),
            action="identify business entities (14 tables)",
            model="claude-sonnet-4",
            cost_estimate_usd=0.01,
            cap_usd=0.50,
        )
        out = buf.getvalue()
        # All four numbers/strings must appear so the user can
        # connect the spend to the action.
        assert "identify business entities (14 tables)" in out
        assert "claude-sonnet-4" in out
        assert "$0.01" in out
        assert "$0.50" in out

    def test_cost_renders_at_two_decimal_places(self) -> None:
        # Floats like 0.0123 must render as $0.01, not $0.0123 — the
        # user wants a quick visual bound, not a billing-precision
        # number. Two decimal places matches the disclosure in
        # prompt_for_anthropic_key.
        from schemabrain._ui import make_console, print_llm_stage_preamble

        buf = io.StringIO()
        print_llm_stage_preamble(
            make_console(file=buf, force_terminal=False, width=120),
            action="x",
            model="m",
            cost_estimate_usd=0.0123,
            cap_usd=0.5078,
        )
        out = buf.getvalue()
        assert "$0.01" in out
        assert "$0.51" in out
        # Don't leak excess precision.
        assert "$0.0123" not in out
        assert "$0.5078" not in out

    def test_uses_pending_glyph_for_in_flight_signal(self) -> None:
        # The pending glyph (◇) is the design's "in progress / about
        # to happen" marker — distinct from the active glyph (▸) used
        # for the stage-row header. The visual hierarchy is:
        # ▸ Stage row (running)
        #   ◇ Sub-line (about to happen)
        from schemabrain._ui import GLYPH_PENDING, make_console, print_llm_stage_preamble

        buf = io.StringIO()
        print_llm_stage_preamble(
            make_console(file=buf, force_terminal=False, width=120),
            action="x",
            model="m",
            cost_estimate_usd=0.01,
            cap_usd=0.5,
        )
        assert GLYPH_PENDING in buf.getvalue()


class TestLiveLlmStageProgress:
    """D1: ``live_llm_stage_progress`` upgrades the static preamble
    to a Rich ``Live`` display with auto-updating elapsed timer.

    The Live re-renders every 0.5s during the body's blocking LLM
    call so operators on a ~30s round-trip get continuous
    "still working" feedback instead of an opaque spinner.

    Non-TTY callers (CI / piped stderr) degrade to the static
    preamble line — no Live frames, but the cost framing still
    reaches the log so artifact scrapers see what the call was.
    """

    def test_non_tty_renders_static_preamble_only(self) -> None:
        # Non-TTY: print the preamble line + yield, no Live, no
        # timer. The preamble carries the load-bearing facts
        # (action + model + cost + cap) so a log scraper sees what
        # the call was even without per-frame updates.
        from schemabrain._ui import live_llm_stage_progress, make_console

        buf = io.StringIO()
        console = make_console(file=buf, force_terminal=False, width=120)
        with live_llm_stage_progress(
            console,
            action="identify business entities (7 tables)",
            model="claude-sonnet-4-6",
            cost_estimate_usd=0.01,
            cap_usd=1.0,
        ):
            pass

        out = buf.getvalue()
        assert "claude-sonnet-4-6" in out
        assert "identify business entities (7 tables)" in out
        assert "$0.01" in out
        assert "$1.00" in out
        # No "elapsed" line on non-TTY — that's the TTY-only timer.
        assert "elapsed" not in out

    def test_tty_renders_elapsed_timer_line(self) -> None:
        # TTY path: Live's `_PreambleWithTimer.__rich__` renders the
        # preamble PLUS the "elapsed Xs" line. First frame fires on
        # `Live.__enter__` before the yield, so we always see at
        # least "elapsed 0s" even with an instant body.
        from schemabrain._ui import live_llm_stage_progress

        buf = io.StringIO()
        # `color_system=None` strips ANSI so substring assertions
        # match the visible characters.
        console = Console(
            file=buf,
            force_terminal=True,
            width=120,
            color_system=None,
            legacy_windows=False,
        )
        with live_llm_stage_progress(
            console,
            action="identify business entities (7 tables)",
            model="claude-sonnet-4-6",
            cost_estimate_usd=0.01,
            cap_usd=1.0,
        ):
            pass

        out = buf.getvalue()
        assert "identify business entities (7 tables)" in out
        assert "claude-sonnet-4-6" in out
        assert "elapsed" in out
        assert "0s" in out

    def test_exception_in_body_propagates(self) -> None:
        # Critical: the `yield` is NOT inside an `except Exception`
        # clause. A body-raised exception must propagate cleanly to
        # the caller's handler. F1 had a wide-try bug that this
        # contract regression-tests against.
        from schemabrain._ui import live_llm_stage_progress

        buf = io.StringIO()
        console = Console(
            file=buf,
            force_terminal=True,
            width=120,
            color_system=None,
            legacy_windows=False,
        )

        class _SpecificError(RuntimeError):
            pass

        with (
            pytest.raises(_SpecificError, match="boom"),
            live_llm_stage_progress(
                console,
                action="x",
                model="m",
                cost_estimate_usd=0.01,
                cap_usd=1.0,
            ),
        ):
            raise _SpecificError("boom")

    def test_non_tty_pauses_active_spinner_only_for_preamble_print(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Non-TTY: only the preamble print needs the spinner
        # paused (so the static line isn't overwritten by a
        # carriage-return spinner frame). Body doesn't need
        # the pause because there's no Live to conflict with.
        # The spinner restarts BEFORE the body runs — minimizes
        # the visual interruption on log scrapers.
        from schemabrain._ui import (
            live_llm_stage_progress,
            make_console,
            register_active_spinner,
        )

        events: list[str] = []

        class _RecordingStatus:
            def start(self) -> None:
                events.append("start")

            def stop(self) -> None:
                events.append("stop")

        buf = io.StringIO()
        console = make_console(file=buf, force_terminal=False, width=120)

        with (
            register_active_spinner(_RecordingStatus()),
            live_llm_stage_progress(
                console,
                action="x",
                model="m",
                cost_estimate_usd=0.01,
                cap_usd=1.0,
            ),
        ):
            events.append("body")

        # Non-TTY contract: short-lived pause around the print.
        assert events == ["stop", "start", "body"]

    def test_tty_pauses_active_spinner_for_the_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # TTY: the inner Live conflicts with the outer Status
        # (Rich rejects nested live displays with `LiveError`).
        # Pause spans the entire body — Live is active from
        # before-yield to after-yield, and the outer Status
        # must stay stopped that whole time.
        from schemabrain._ui import (
            live_llm_stage_progress,
            register_active_spinner,
        )

        events: list[str] = []

        class _RecordingStatus:
            def start(self) -> None:
                events.append("start")

            def stop(self) -> None:
                events.append("stop")

        buf = io.StringIO()
        console = Console(
            file=buf,
            force_terminal=True,
            width=120,
            color_system=None,
            legacy_windows=False,
        )

        with (
            register_active_spinner(_RecordingStatus()),
            live_llm_stage_progress(
                console,
                action="x",
                model="m",
                cost_estimate_usd=0.01,
                cap_usd=1.0,
            ),
        ):
            events.append("body")

        # TTY contract: pause spans the body so Live can render
        # without nesting-conflict with the outer Status.
        assert events == ["stop", "body", "start"]


class TestPromptYesNo:
    """F3: ``prompt_yes_no`` is the shared yes/no helper used by the wizard's
    inline stage-6 overwrite prompt. Mirrors `prompt_for_url` shape: pauses
    the active spinner around the blocking prompt so spinner frames don't
    overwrite the user's response line.
    """

    def test_returns_true_when_confirm_ask_returns_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from schemabrain._ui import make_console, prompt_yes_no

        monkeypatch.setattr("rich.prompt.Confirm.ask", lambda *a, **kw: True)
        assert prompt_yes_no(make_console(file=io.StringIO()), "overwrite?", default=False) is True

    def test_returns_false_when_confirm_ask_returns_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from schemabrain._ui import make_console, prompt_yes_no

        monkeypatch.setattr("rich.prompt.Confirm.ask", lambda *a, **kw: False)
        assert prompt_yes_no(make_console(file=io.StringIO()), "overwrite?", default=False) is False

    def test_passes_default_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from schemabrain._ui import make_console, prompt_yes_no

        captured: dict[str, object] = {}

        def fake_ask(*args: object, **kwargs: object) -> bool:
            captured.update(kwargs)
            return False

        monkeypatch.setattr("rich.prompt.Confirm.ask", fake_ask)
        prompt_yes_no(make_console(file=io.StringIO()), "overwrite?", default=True)
        assert captured.get("default") is True

    def test_pauses_active_spinner_around_prompt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from schemabrain._ui import (
            make_console,
            prompt_yes_no,
            register_active_spinner,
        )

        events: list[str] = []

        class _RecordingStatus:
            def start(self) -> None:
                events.append("start")

            def stop(self) -> None:
                events.append("stop")

        def fake_ask(*args: object, **kwargs: object) -> bool:
            events.append("ask")
            return False

        monkeypatch.setattr("rich.prompt.Confirm.ask", fake_ask)
        with register_active_spinner(_RecordingStatus()):
            prompt_yes_no(make_console(file=io.StringIO()), "q?", default=False)
        # Spinner must stop BEFORE Confirm.ask blocks on stdin, then
        # restart after — same contract as prompt_for_url.
        assert events == ["stop", "ask", "start"]


class TestStderrIsInteractiveTty:
    """F3: ``stderr_is_interactive_tty`` is the single source of truth for
    "can we safely prompt?" — shared by cli and wizard so tests can
    monkeypatch one symbol and reach both code paths.
    """

    def test_true_when_both_stdin_and_stderr_are_ttys(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from schemabrain._ui import stderr_is_interactive_tty

        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("sys.stderr.isatty", lambda: True)
        assert stderr_is_interactive_tty() is True

    def test_false_when_stdin_is_not_tty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from schemabrain._ui import stderr_is_interactive_tty

        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        monkeypatch.setattr("sys.stderr.isatty", lambda: True)
        assert stderr_is_interactive_tty() is False

    def test_false_when_stderr_is_not_tty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from schemabrain._ui import stderr_is_interactive_tty

        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("sys.stderr.isatty", lambda: False)
        assert stderr_is_interactive_tty() is False
