"""Tests for the interactive host-selection prompt.

The prompt itself is a thin wrapper around ``rich.prompt.Prompt.ask`` —
these tests verify:

  1. The menu renders all 5 options (4 supported hosts + manual).
  2. Detected hosts get a ✓ chip; undetected rows render the chip-
     width whitespace so the menu columns line up.
  3. The default cursor points at the priority winner from
     ``detect_host``.
  4. Empty-detection case defaults to ``manual``.
  5. Numeric choice resolves correctly via the menu ordering.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

import pytest
from rich.console import Console

from schemabrain.setup.host_select import prompt_for_host_selection

if TYPE_CHECKING:
    pass


def _capture_console() -> tuple[Console, io.StringIO]:
    """Return a Rich console that writes to a StringIO buffer, plus the buffer.

    We assert on the rendered output so the test can pin the visible
    menu shape (option count, ✓ chips, default-index hint).
    """
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=140)
    return console, buf


class TestPromptForHostSelection:
    def test_menu_lists_all_five_options(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Menu rows: 4 supported hosts + manual. Pinned so removing a row
        without updating the host-select module surfaces here first.
        """
        monkeypatch.setattr("schemabrain.setup.host_select.detect_available_hosts", lambda: ())
        monkeypatch.setattr("schemabrain.setup.host_select.detect_host", lambda: "manual")
        # Stub Prompt.ask to return the manual default without actually
        # reading stdin.
        monkeypatch.setattr("rich.prompt.Prompt.ask", lambda *_a, **_kw: "5")
        console, buf = _capture_console()
        prompt_for_host_selection(console=console)
        output = buf.getvalue()
        assert "Claude Desktop" in output
        assert "Claude Code" in output
        assert "Cursor" in output
        assert "Windsurf" in output
        assert "Manual" in output

    def test_detected_hosts_render_with_check_chip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The ✓ chip appears next to detected rows so the operator sees
        which hosts the wizard found on disk. Undetected rows render
        the chip-width whitespace so columns align.
        """
        monkeypatch.setattr(
            "schemabrain.setup.host_select.detect_available_hosts",
            lambda: ("claude-desktop", "cursor"),
        )
        monkeypatch.setattr("schemabrain.setup.host_select.detect_host", lambda: "claude-desktop")
        monkeypatch.setattr("rich.prompt.Prompt.ask", lambda *_a, **_kw: "1")
        console, buf = _capture_console()
        prompt_for_host_selection(console=console)
        output = buf.getvalue()
        # "detected" appears 2x: once for claude-desktop, once for
        # cursor. claude-code + windsurf rows don't get the chip.
        assert output.count("detected") == 2

    def test_default_cursor_points_at_priority_winner(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pressing Enter (no number typed) returns the priority winner
        from ``detect_host``. We stub ``detect_host`` to "cursor" and
        assert the prompt resolves to index 3 (cursor's menu row).
        """
        monkeypatch.setattr(
            "schemabrain.setup.host_select.detect_available_hosts", lambda: ("cursor",)
        )
        monkeypatch.setattr("schemabrain.setup.host_select.detect_host", lambda: "cursor")

        # Capture the default kwarg Prompt.ask receives — that's the
        # menu index the user would accept by pressing Enter.
        captured: dict[str, object] = {}

        def fake_ask(*_a: object, **kwargs: object) -> str:
            captured["default"] = kwargs.get("default")
            return str(kwargs["default"])

        monkeypatch.setattr("rich.prompt.Prompt.ask", fake_ask)
        result = prompt_for_host_selection(console=_capture_console()[0])
        assert captured["default"] == "3"  # cursor's row index in the menu
        assert result == "cursor"

    def test_empty_detection_defaults_to_manual(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When nothing is detected, ``detect_host`` returns ``"manual"``
        and the prompt's default cursor lands on row 5 (manual).
        """
        monkeypatch.setattr("schemabrain.setup.host_select.detect_available_hosts", lambda: ())
        monkeypatch.setattr("schemabrain.setup.host_select.detect_host", lambda: "manual")
        captured: dict[str, object] = {}

        def fake_ask(*_a: object, **kwargs: object) -> str:
            captured["default"] = kwargs.get("default")
            return str(kwargs["default"])

        monkeypatch.setattr("rich.prompt.Prompt.ask", fake_ask)
        result = prompt_for_host_selection(console=_capture_console()[0])
        assert captured["default"] == "5"
        assert result == "manual"

    def test_each_numeric_choice_resolves_to_the_right_host(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Numeric choices 1..5 map to the menu rows in order:
        1=claude-desktop, 2=claude-code, 3=cursor, 4=windsurf, 5=manual.
        Pinned so reordering the menu without updating downstream
        consumers (or vice versa) breaks loudly.
        """
        monkeypatch.setattr("schemabrain.setup.host_select.detect_available_hosts", lambda: ())
        monkeypatch.setattr("schemabrain.setup.host_select.detect_host", lambda: "manual")
        expected = {
            "1": "claude-desktop",
            "2": "claude-code",
            "3": "cursor",
            "4": "windsurf",
            "5": "manual",
        }
        for choice_str, expected_host in expected.items():
            # Default-arg binds `choice_str` to the iteration value to
            # sidestep Python's late-binding closure (B023).
            monkeypatch.setattr(
                "rich.prompt.Prompt.ask",
                lambda *_a, choice=choice_str, **_kw: choice,
            )
            result = prompt_for_host_selection(console=_capture_console()[0])
            assert result == expected_host, f"choice {choice_str!r} resolved wrong"
