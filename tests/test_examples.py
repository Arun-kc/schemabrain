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
_EXAMPLE_MCP_CONFIGS: tuple[str, ...] = (
    "claude_desktop_config.example.json",
    "cursor_mcp_config.example.json",
)


@pytest.mark.parametrize("config_filename", _EXAMPLE_MCP_CONFIGS)
class TestMcpConfigExamples:
    """Both Claude Desktop and Cursor consume the same MCP `mcpServers`
    schema. We pin the structural shape so a careless edit to either
    copy-paste template can't silently break the path documented in
    `docs/setup.md`.
    """

    def _load(self, config_filename: str) -> dict:
        path = _EXAMPLES / config_filename
        return json.loads(path.read_text())

    def test_file_exists_and_is_valid_json(self, config_filename: str) -> None:
        config = self._load(config_filename)
        assert isinstance(config, dict)

    def test_has_mcp_servers_block(self, config_filename: str) -> None:
        config = self._load(config_filename)
        assert "mcpServers" in config

    def test_registers_schemabrain_server(self, config_filename: str) -> None:
        config = self._load(config_filename)
        assert "schemabrain" in config["mcpServers"]

    def test_command_invokes_schemabrain_serve(self, config_filename: str) -> None:
        # The command should resolve to the `schemabrain` CLI and the
        # first positional arg must be `serve`. If we ever rename the
        # subcommand, this test fires before users paste a stale config.
        server = self._load(config_filename)["mcpServers"]["schemabrain"]
        assert "command" in server
        assert server["command"].rstrip("/").endswith("schemabrain")
        assert server["args"][0] == "serve"

    def test_args_include_source_and_store_path_flags(self, config_filename: str) -> None:
        # These two flags are required by `schemabrain serve` — if either
        # template forgets either, users get an argparse error on first launch.
        args = self._load(config_filename)["mcpServers"]["schemabrain"]["args"]
        assert "--source" in args
        assert "--store-path" in args

    def test_source_url_uses_psycopg_v3_scheme(self, config_filename: str) -> None:
        # The bare `postgresql://` scheme fails with ModuleNotFoundError
        # because we depend on psycopg v3. The example must use the
        # `postgresql+psycopg://` form so users don't fall into that pit.
        args = self._load(config_filename)["mcpServers"]["schemabrain"]["args"]
        source_idx = args.index("--source") + 1
        assert args[source_idx].startswith("postgresql+psycopg://")


class TestCursorConfigSpecifics:
    """Cursor-specific schema requirements that don't apply to Claude Desktop.

    Cursor's official docs require a `"type": "stdio"` field on stdio server
    entries; Claude Desktop's `claude_desktop_config.json` doesn't use this
    field. Pinning the Cursor-only contract here keeps the parametrized
    shared-shape tests above clean while still catching template drift.
    """

    def test_stdio_server_declares_type_field(self) -> None:
        # Per https://cursor.com/docs/mcp the `type` field is required on
        # stdio entries. The IDE has historically been lenient about omitting
        # it, but the official schema is hardening (cursor-agent CLI already
        # rejects unexpected values). Ship the template with `type: "stdio"`
        # so users don't hit a future strict-mode failure.
        path = _EXAMPLES / "cursor_mcp_config.example.json"
        config = json.loads(path.read_text())
        assert config["mcpServers"]["schemabrain"].get("type") == "stdio"


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

    def test_mentions_all_integration_paths(self) -> None:
        text = self._path().read_text()
        assert "Claude Desktop" in text
        assert "Cursor" in text
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
