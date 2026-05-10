"""Tests for the bundled example artifacts.

These don't exercise real Claude Desktop or Anthropic API — they pin
the structural shape of the example files so a careless edit to the
copy-paste templates can't silently break the path documented in
`docs/setup.md`.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_EXAMPLES = _REPO_ROOT / "examples"


class TestClaudeDesktopConfigExample:
    """The `mcpServers` block users will paste into Claude Desktop's
    config. Schema is defined by Anthropic's MCP integration; the
    important shape is the one that lets `schemabrain serve` actually
    spawn correctly when Claude Desktop reads it.
    """

    def _load(self) -> dict:
        path = _EXAMPLES / "claude_desktop_config.example.json"
        return json.loads(path.read_text())

    def test_file_exists_and_is_valid_json(self) -> None:
        config = self._load()
        assert isinstance(config, dict)

    def test_has_mcp_servers_block(self) -> None:
        config = self._load()
        assert "mcpServers" in config

    def test_registers_schemabrain_server(self) -> None:
        config = self._load()
        assert "schemabrain" in config["mcpServers"]

    def test_command_invokes_schemabrain_serve(self) -> None:
        # The command should resolve to the `schemabrain` CLI and the
        # first positional arg must be `serve`. If we ever rename the
        # subcommand, this test fires before users paste a stale config.
        server = self._load()["mcpServers"]["schemabrain"]
        assert "command" in server
        assert server["command"].rstrip("/").endswith("schemabrain")
        assert server["args"][0] == "serve"

    def test_args_include_source_and_store_path_flags(self) -> None:
        # These two flags are required by `schemabrain serve` — if the
        # template forgets either, users get an argparse error on first
        # Claude Desktop launch.
        args = self._load()["mcpServers"]["schemabrain"]["args"]
        assert "--source" in args
        assert "--store-path" in args

    def test_source_url_uses_psycopg_v3_scheme(self) -> None:
        # The bare `postgresql://` scheme fails with ModuleNotFoundError
        # because we depend on psycopg v3. The example must use the
        # `postgresql+psycopg://` form so users don't fall into that pit.
        args = self._load()["mcpServers"]["schemabrain"]["args"]
        source_idx = args.index("--source") + 1
        assert args[source_idx].startswith("postgresql+psycopg://")


class TestAnthropicDemoScript:
    """The standalone Anthropic-SDK demo. We don't run it (would burn
    real Anthropic credits) but verify it's parseable Python and the
    advertised entry points exist so a refactor that breaks the demo
    fails CI.
    """

    def _path(self) -> Path:
        return _EXAMPLES / "anthropic_demo.py"

    def test_file_exists(self) -> None:
        assert self._path().exists()

    def test_is_valid_python(self) -> None:
        # ast.parse raises SyntaxError on broken code. Cheaper than
        # importlib (which would execute module-level statements).
        ast.parse(self._path().read_text())

    def test_advertises_main_and_run_agent_loop(self) -> None:
        # `main()` is the CLI entry; `_run_agent_loop()` is the inner
        # async function the docs reference. If either is renamed, the
        # docs in setup.md drift silently.
        source = self._path().read_text()
        assert "def main()" in source
        assert "async def _run_agent_loop" in source

    def test_uses_official_mcp_client_session(self) -> None:
        # The demo's whole pedagogical point is that it uses the same
        # path Claude Desktop does. If someone replaces ClientSession
        # with a custom transport, that lesson is lost.
        source = self._path().read_text()
        assert "from mcp import ClientSession" in source
        assert "from mcp.client.stdio import stdio_client" in source

    def test_has_max_turns_safety_cap(self) -> None:
        # The agent loop MUST be bounded. An unbounded tool-use loop
        # against a real API is a great way to spend $50 on a typo.
        source = self._path().read_text()
        assert "max_turns" in source
        assert "_DEFAULT_MAX_TURNS" in source


class TestSetupDocs:
    def _path(self) -> Path:
        return _REPO_ROOT / "docs" / "setup.md"

    def test_file_exists(self) -> None:
        assert self._path().exists()

    def test_mentions_both_install_paths(self) -> None:
        text = self._path().read_text()
        assert "Claude Desktop" in text
        assert "anthropic_demo" in text or "Anthropic SDK" in text

    @pytest.mark.parametrize(
        "warning",
        [
            "postgresql+psycopg://",
            "absolute",
            "max-turns",
            "ANTHROPIC_API_KEY",
        ],
    )
    def test_mentions_known_pitfalls(self, warning: str) -> None:
        # The Troubleshooting table earned its existence — every entry
        # there came from a real footgun. If the doc is rewritten and
        # drops a warning, this test catches it.
        assert warning in self._path().read_text(), f"setup docs lost warning about {warning!r}"
