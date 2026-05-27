"""Argparse-layer + dispatch tests for `schemabrain dashboard`.

These tests do NOT require the `[ui]` extra. The parser tests exercise
``_build_parser`` only (no sidecar/uvicorn imports at parse time, by
design — see ``schemabrain/dashboard/sidecar.py`` module docstring on
the deferred-import contract). The dispatch test monkeypatches
``_cmd_dashboard`` so ``main`` can be exercised without booting uvicorn.

Coverage of the actual ``run_dashboard`` function lives in
``tests/dashboard/test_cli.py`` (skipped when [ui] is missing).
"""

from __future__ import annotations

import pytest

from schemabrain.cli import _build_parser


class TestDashboardArgparseSurface:
    """Verify the argparse Namespace shape for ``schemabrain dashboard``."""

    def test_defaults_when_no_flags_passed(self) -> None:
        parser = _build_parser()
        ns = parser.parse_args(["dashboard"])
        assert ns.command == "dashboard"
        assert ns.store_path == "./schemabrain.db"
        assert ns.port == 7878
        # `--no-open` is store_false; default = True (browser opens).
        assert ns.open_browser is True

    def test_store_path_flag_parses(self) -> None:
        parser = _build_parser()
        ns = parser.parse_args(["dashboard", "--store-path", "/tmp/sb-e2e.db"])
        assert ns.store_path == "/tmp/sb-e2e.db"

    def test_port_flag_parses_as_int(self) -> None:
        parser = _build_parser()
        ns = parser.parse_args(["dashboard", "--port", "9999"])
        assert ns.port == 9999
        assert isinstance(ns.port, int)

    def test_no_open_flag_flips_open_browser_to_false(self) -> None:
        parser = _build_parser()
        ns = parser.parse_args(["dashboard", "--no-open"])
        assert ns.open_browser is False

    def test_port_rejects_non_integer(self, capsys: pytest.CaptureFixture[str]) -> None:
        """argparse's `type=int` converter must reject `--port abc` early."""
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["dashboard", "--port", "abc"])
        err = capsys.readouterr().err
        assert "invalid int value" in err

    def test_no_host_flag_exposed(self) -> None:
        """`--host` must NOT be accepted — bind host is a hard invariant.

        Mirrors the architectural assertion in
        ``schemabrain.dashboard.sidecar.BIND_HOST``: the CLI must not
        offer a knob that would let an operator expose the dashboard on
        a public interface.
        """
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["dashboard", "--host", "0.0.0.0"])


class TestDashboardDispatch:
    """Verify ``main`` routes the parsed namespace to ``_cmd_dashboard``."""

    def test_main_dispatches_to_cmd_dashboard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from schemabrain import cli as cli_module

        recorded: dict[str, object] = {}

        def _fake_cmd(*, store_path: str, port: int, open_browser: bool) -> int:
            recorded["store_path"] = store_path
            recorded["port"] = port
            recorded["open_browser"] = open_browser
            return 0

        monkeypatch.setattr(cli_module, "_cmd_dashboard", _fake_cmd)
        rc = cli_module.main(
            ["dashboard", "--store-path", "/tmp/x.db", "--port", "9000", "--no-open"]
        )
        assert rc == 0
        assert recorded == {
            "store_path": "/tmp/x.db",
            "port": 9000,
            "open_browser": False,
        }

    def test_main_dispatches_with_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from schemabrain import cli as cli_module

        recorded: dict[str, object] = {}

        def _fake_cmd(*, store_path: str, port: int, open_browser: bool) -> int:
            recorded["store_path"] = store_path
            recorded["port"] = port
            recorded["open_browser"] = open_browser
            return 0

        monkeypatch.setattr(cli_module, "_cmd_dashboard", _fake_cmd)
        rc = cli_module.main(["dashboard"])
        assert rc == 0
        assert recorded["store_path"] == "./schemabrain.db"
        assert recorded["port"] == 7878
        assert recorded["open_browser"] is True
