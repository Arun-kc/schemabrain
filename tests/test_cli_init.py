"""Tests for `schemabrain init` CLI registration + dispatch.

Goals:

  1. Flag parsing: --source/--url-env, --store-path, --host,
     --env-var, --skip-index, --yes, --print-only.
  2. Exit codes per Decision 9: 0 on success, 1 on claude-code
     shell-out failure (snippet still printed), 2 on operational
     refusal (URL conflict, source unreachable, store empty, etc.).
  3. `--print-only` and `--host manual` both produce the same
     snippet JSON to stdout.
  4. Successful claude-desktop wiring writes the JSON file and
     reports the path + "next" line to stderr.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from schemabrain.cli import main
from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore


@pytest.fixture
def seeded_store(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "store.db"
    store = SQLiteStore(path=path)
    try:
        store.write_table(
            Table(
                name="orders",
                schema_name="public",
                columns=(
                    Column(
                        name="id",
                        table_name="orders",
                        schema_name="public",
                        data_type="bigint",
                        nullable=False,
                        ordinal_position=1,
                        is_primary_key=True,
                    ),
                ),
            ),
            source_connection_id="src_a",
        )
        store.write_entity(
            Entity(
                name="order",
                description="",
                binding=SingleTableBinding(qualified_table="public.orders"),
                identity="id",
            ),
            source_connection_id="src_a",
        )
        yield path
    finally:
        store.close()


@pytest.fixture
def stub_uvx(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(
        "shutil.which",
        lambda n: "/usr/local/bin/uvx" if n == "uvx" else None,
    )
    yield


class TestInitCliPrintOnly:
    def test_print_only_exits_zero(self, seeded_store: Path, stub_uvx: None) -> None:
        exit_code = main(
            [
                "init",
                "--source",
                "sqlite:///:memory:",
                "--store-path",
                str(seeded_store),
                "--print-only",
            ]
        )
        assert exit_code == 0

    def test_print_only_writes_snippet_to_stdout(
        self,
        seeded_store: Path,
        stub_uvx: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main(
            [
                "init",
                "--source",
                "sqlite:///:memory:",
                "--store-path",
                str(seeded_store),
                "--print-only",
            ]
        )
        captured = capsys.readouterr()
        # JSON block lands on stdout, paste-ready.
        parsed = json.loads(captured.out)
        assert "schemabrain" in parsed["mcpServers"]
        # The DB URL lives in env, not args (Decision 3 invariant).
        snippet = parsed["mcpServers"]["schemabrain"]
        assert "sqlite:///:memory:" not in snippet["args"]
        assert snippet["env"] == {"SCHEMABRAIN_DATABASE_URL": "sqlite:///:memory:"}

    def test_host_manual_is_equivalent_to_print_only(
        self,
        seeded_store: Path,
        stub_uvx: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main(
            [
                "init",
                "--source",
                "sqlite:///:memory:",
                "--store-path",
                str(seeded_store),
                "--host",
                "manual",
            ]
        )
        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert "schemabrain" in parsed["mcpServers"]

    def test_print_only_emits_helpful_next_block_to_stderr(
        self,
        seeded_store: Path,
        stub_uvx: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main(
            [
                "init",
                "--source",
                "sqlite:///:memory:",
                "--store-path",
                str(seeded_store),
                "--print-only",
            ]
        )
        captured = capsys.readouterr()
        assert "Common config paths" in captured.err
        assert "restart your host" in captured.err


class TestInitCliClaudeDesktop:
    def test_writes_config_and_exits_zero(
        self,
        seeded_store: Path,
        stub_uvx: None,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        cfg_parent = tmp_path / "Claude"
        cfg_parent.mkdir()
        cfg = cfg_parent / "claude_desktop_config.json"
        monkeypatch.setattr(
            "schemabrain.setup.init_flow.claude_desktop_config_path",
            lambda: cfg,
        )
        exit_code = main(
            [
                "init",
                "--source",
                "sqlite:///:memory:",
                "--store-path",
                str(seeded_store),
                "--host",
                "claude-desktop",
            ]
        )
        assert exit_code == 0
        # File written.
        config = json.loads(cfg.read_text())
        assert "schemabrain" in config["mcpServers"]
        captured = capsys.readouterr()
        # Confirmation + next-step line on stderr.
        assert "wrote schemabrain entry" in captured.err
        assert "list the entities" in captured.err


class TestInitCliRefusals:
    def test_no_url_exits_two(self) -> None:
        # Neither --source nor --url-env supplied.
        exit_code = main(["init", "--host", "manual"])
        assert exit_code == 2

    def test_both_url_forms_exits_two(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DB_URL", "sqlite:///:memory:")
        exit_code = main(
            [
                "init",
                "--host",
                "manual",
                "--source",
                "sqlite:///:memory:",
                "--url-env",
                "DB_URL",
            ]
        )
        assert exit_code == 2

    def test_empty_store_without_skip_index_exits_two(self, tmp_path: Path, stub_uvx: None) -> None:
        store_path = tmp_path / "store.db"
        SQLiteStore(path=store_path).close()
        exit_code = main(
            [
                "init",
                "--source",
                "sqlite:///:memory:",
                "--store-path",
                str(store_path),
                "--host",
                "manual",
            ]
        )
        assert exit_code == 2

    def test_skip_index_allows_empty_store(self, tmp_path: Path, stub_uvx: None) -> None:
        store_path = tmp_path / "store.db"
        SQLiteStore(path=store_path).close()
        exit_code = main(
            [
                "init",
                "--source",
                "sqlite:///:memory:",
                "--store-path",
                str(store_path),
                "--host",
                "manual",
                "--skip-index",
            ]
        )
        assert exit_code == 0


class TestInitCliEnvVarFlag:
    def test_custom_env_var_lands_in_snippet(
        self,
        seeded_store: Path,
        stub_uvx: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main(
            [
                "init",
                "--source",
                "sqlite:///:memory:",
                "--store-path",
                str(seeded_store),
                "--print-only",
                "--env-var",
                "ACME_PROD_DB",
            ]
        )
        parsed = json.loads(capsys.readouterr().out)
        snippet = parsed["mcpServers"]["schemabrain"]
        assert "ACME_PROD_DB" in snippet["env"]
        # The snippet's args reference the SAME env var name via
        # --url-env so the server reads from it at launch.
        assert "ACME_PROD_DB" in snippet["args"]


class TestInitCliClaudeCode:
    def test_shell_out_failure_exits_one_and_prints_fallback(
        self,
        seeded_store: Path,
        stub_uvx: None,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from schemabrain.setup.hosts import ClaudeCodeInstallResult

        def fake_install(snippet: object) -> ClaudeCodeInstallResult:
            return ClaudeCodeInstallResult(
                succeeded=False,
                command_run=("claude", "mcp", "add", "schemabrain", "--", "uvx"),
                stderr="server already registered",
            )

        monkeypatch.setattr("schemabrain.setup.init_flow.install_to_claude_code", fake_install)
        exit_code = main(
            [
                "init",
                "--source",
                "sqlite:///:memory:",
                "--store-path",
                str(seeded_store),
                "--host",
                "claude-code",
            ]
        )
        assert exit_code == 1
        captured = capsys.readouterr()
        # The command is rendered so the user can copy-paste it.
        assert "claude mcp add" in captured.err

    def test_shell_out_success_exits_zero(
        self,
        seeded_store: Path,
        stub_uvx: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from schemabrain.setup.hosts import ClaudeCodeInstallResult

        def fake_install(snippet: object) -> ClaudeCodeInstallResult:
            return ClaudeCodeInstallResult(
                succeeded=True,
                command_run=("claude", "mcp", "add", "schemabrain"),
            )

        monkeypatch.setattr("schemabrain.setup.init_flow.install_to_claude_code", fake_install)
        exit_code = main(
            [
                "init",
                "--source",
                "sqlite:///:memory:",
                "--store-path",
                str(seeded_store),
                "--host",
                "claude-code",
            ]
        )
        assert exit_code == 0


class TestInitCliIdempotency:
    def test_second_run_exits_zero_no_changes(
        self,
        seeded_store: Path,
        stub_uvx: None,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        cfg_parent = tmp_path / "Claude"
        cfg_parent.mkdir()
        cfg = cfg_parent / "claude_desktop_config.json"
        monkeypatch.setattr(
            "schemabrain.setup.init_flow.claude_desktop_config_path",
            lambda: cfg,
        )
        main(
            [
                "init",
                "--source",
                "sqlite:///:memory:",
                "--store-path",
                str(seeded_store),
                "--host",
                "claude-desktop",
            ]
        )
        capsys.readouterr()  # discard first-run output
        exit_code = main(
            [
                "init",
                "--source",
                "sqlite:///:memory:",
                "--store-path",
                str(seeded_store),
                "--host",
                "claude-desktop",
            ]
        )
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "already configured" in captured.err
