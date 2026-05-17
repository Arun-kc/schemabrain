"""Tests for `schemabrain init` CLI registration + dispatch.

Goals:

  1. Flag parsing: --source/--url-env, --store-path, --host,
     --env-var, --skip-index, --yes, --print-only.
  2. Exit codes: 0 on success, 1 on claude-code shell-out failure
     (snippet still printed), 2 on operational refusal (URL conflict,
     source unreachable, store empty, etc.).
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
        # The DB URL lives in env, not args (keeps creds out of argv).
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

    def test_print_only_emits_manual_mode_header(
        self,
        seeded_store: Path,
        stub_uvx: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Polish: render the "Schema Brain init — manual mode." header
        # + "Add this to your MCP host's config:" so the JSON block has
        # a labelled top, not just naked output.
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
        assert "Schema Brain init" in captured.err
        assert "manual mode" in captured.err
        assert "Add this to your MCP host's config" in captured.err

    def test_print_only_config_paths_render_unwrapped(
        self,
        seeded_store: Path,
        stub_uvx: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Polish: the "Common config paths" block must keep each path
        # on a single line so users can copy-paste without rich's
        # auto-wrap splitting "Application Support" mid-path.
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
        # The macOS path is the longest and the one rich was wrapping;
        # asserting the full token appears on one line guards against
        # regression to the auto-wrap default.
        assert "~/Library/Application Support/Claude/claude_desktop_config.json" in captured.err
        # Defensive: the wrapped form (with a line break inside the
        # path) must NOT appear.
        assert "~/Library/Application \nSupport" not in captured.err
        assert "Application\nSupport" not in captured.err


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


class TestInitCliUrlEnv:
    """The --url-env path (preferred over --source so creds stay out
    of argv) needs end-to-end happy-path coverage at the CLI layer."""

    def test_url_env_resolves_and_init_succeeds(
        self,
        seeded_store: Path,
        stub_uvx: None,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("FAKE_DB_URL", "sqlite:///:memory:")
        exit_code = main(
            [
                "init",
                "--url-env",
                "FAKE_DB_URL",
                "--store-path",
                str(seeded_store),
                "--print-only",
            ]
        )
        assert exit_code == 0
        parsed = json.loads(capsys.readouterr().out)
        snippet = parsed["mcpServers"]["schemabrain"]
        # The env var is read at CLI level and its VALUE lands in the
        # snippet's env block under the canonical SCHEMABRAIN_DATABASE_URL
        # key (the default env-var name). The original env-var name from
        # --url-env is decoupled from the snippet's env-var name (set
        # by --env-var, default SCHEMABRAIN_DATABASE_URL).
        assert snippet["env"]["SCHEMABRAIN_DATABASE_URL"] == "sqlite:///:memory:"


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


class TestRedactEnvArgs:
    """Direct unit tests for the credential-redaction helper used in
    the claude-code shell-out fallback render."""

    def test_redacts_value_after_dash_e(self) -> None:
        from schemabrain.cli import _redact_env_args

        cmd = (
            "claude",
            "mcp",
            "add",
            "-e",
            "DATABASE_URL=postgresql://alice:hunter2@h/db",
            "schemabrain",
            "--",
            "uvx",
        )
        out = _redact_env_args(cmd)
        joined = " ".join(out)
        assert "hunter2" not in joined
        assert "alice" not in joined
        assert "DATABASE_URL=<redacted>" in joined

    def test_redacts_multiple_env_vars(self) -> None:
        from schemabrain.cli import _redact_env_args

        cmd = ("claude", "mcp", "add", "-e", "A=1", "-e", "B=2", "schemabrain")
        out = _redact_env_args(cmd)
        joined = " ".join(out)
        assert "A=<redacted>" in joined
        assert "B=<redacted>" in joined
        assert "1" not in joined.replace("A=<redacted>", "").replace("B=<redacted>", "")

    def test_token_without_equals_after_dash_e_is_left_alone(self) -> None:
        # Defensive: malformed shape where `-e` is followed by a bare
        # value (no KEY= prefix). Don't crash; pass it through.
        from schemabrain.cli import _redact_env_args

        cmd = ("claude", "-e", "bare_value", "schemabrain")
        out = _redact_env_args(cmd)
        assert "bare_value" in " ".join(out)

    def test_non_env_tokens_untouched(self) -> None:
        from schemabrain.cli import _redact_env_args

        cmd = ("claude", "mcp", "add", "schemabrain", "--", "uvx", "schemabrain==0.1")
        out = _redact_env_args(cmd)
        assert list(cmd) == out


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

    def test_shell_out_failure_redacts_credentials_in_rendered_command(
        self,
        seeded_store: Path,
        stub_uvx: None,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Passwords must never land on stderr / terminal scrollback /
        # screen recordings — the shell-out fallback render must
        # redact -e values.
        from schemabrain.setup.hosts import ClaudeCodeInstallResult

        def fake_install(snippet: object) -> ClaudeCodeInstallResult:
            return ClaudeCodeInstallResult(
                succeeded=False,
                command_run=(
                    "claude",
                    "mcp",
                    "add",
                    "-e",
                    "DATABASE_URL=postgresql://alice:hunter2@h/db",
                    "schemabrain",
                    "--",
                    "uvx",
                ),
                stderr="failed",
            )

        monkeypatch.setattr("schemabrain.setup.init_flow.install_to_claude_code", fake_install)
        # Use sqlite so source-reachable + read-only checks pass without
        # network; the password lives only inside the fake shell-out
        # command_run tuple — what we're asserting is the renderer's
        # behaviour, not init's own snippet construction.
        main(
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
        captured = capsys.readouterr()
        # The password must NEVER appear in the rendered fallback
        # command on stderr.
        assert "hunter2" not in captured.err
        assert "DATABASE_URL=<redacted>" in captured.err

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


class TestInitCliInteractiveOverlay:
    """When stderr+stdin are both TTYs, two refusal kinds become
    interactive prompts: entry-exists and empty-store."""

    @pytest.fixture
    def force_interactive(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        monkeypatch.setattr("schemabrain.cli._stderr_is_interactive_tty", lambda: True)
        yield

    def test_overwrite_prompt_y_proceeds_with_assume_yes(
        self,
        seeded_store: Path,
        stub_uvx: None,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        force_interactive: None,
    ) -> None:
        # Pre-seed the host config with an existing different entry.
        cfg = tmp_path / "Claude" / "claude_desktop_config.json"
        cfg.parent.mkdir()
        cfg.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "schemabrain": {
                            "command": "uvx",
                            "args": ["schemabrain==0.0.99", "serve"],
                            "env": {},
                        }
                    }
                }
            )
        )
        monkeypatch.setattr(
            "schemabrain.setup.init_flow.claude_desktop_config_path",
            lambda: cfg,
        )
        # User confirms overwrite.
        monkeypatch.setattr("schemabrain.cli._prompt_yes_no", lambda *_a, **_kw: True)
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
        # The new entry replaced the old one.
        merged = json.loads(cfg.read_text())
        new_args = merged["mcpServers"]["schemabrain"]["args"]
        assert not any("0.0.99" in a for a in new_args)

    def test_overwrite_prompt_n_cancels_gracefully(
        self,
        seeded_store: Path,
        stub_uvx: None,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        force_interactive: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        cfg = tmp_path / "Claude" / "claude_desktop_config.json"
        cfg.parent.mkdir()
        original = json.dumps(
            {
                "mcpServers": {
                    "schemabrain": {
                        "command": "uvx",
                        "args": ["schemabrain==0.0.99", "serve"],
                        "env": {},
                    }
                }
            }
        )
        cfg.write_text(original)
        monkeypatch.setattr(
            "schemabrain.setup.init_flow.claude_desktop_config_path",
            lambda: cfg,
        )
        # User declines overwrite.
        monkeypatch.setattr("schemabrain.cli._prompt_yes_no", lambda *_a, **_kw: False)
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
        # Exit 0 — graceful cancel, not refusal.
        assert exit_code == 0
        # File untouched.
        assert cfg.read_text() == original
        captured = capsys.readouterr()
        assert "cancelled" in captured.err

    def test_empty_store_prompt_y_continues_with_skip_index(
        self,
        tmp_path: Path,
        stub_uvx: None,
        monkeypatch: pytest.MonkeyPatch,
        force_interactive: None,
    ) -> None:
        store_path = tmp_path / "store.db"
        SQLiteStore(path=store_path).close()
        # User confirms "skip indexing for now."
        monkeypatch.setattr("schemabrain.cli._prompt_yes_no", lambda *_a, **_kw: True)
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
        assert exit_code == 0

    def test_empty_store_prompt_n_exits_two_with_hint(
        self,
        tmp_path: Path,
        stub_uvx: None,
        monkeypatch: pytest.MonkeyPatch,
        force_interactive: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        store_path = tmp_path / "store.db"
        SQLiteStore(path=store_path).close()
        monkeypatch.setattr("schemabrain.cli._prompt_yes_no", lambda *_a, **_kw: False)
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
        captured = capsys.readouterr()
        assert "schemabrain index" in captured.err

    def test_non_interactive_skips_prompts_and_refuses(
        self,
        tmp_path: Path,
        stub_uvx: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Force non-interactive — the refusal renders without
        # invoking the prompt (prompts are TTY-gated). Strengthened
        # to assert _prompt_yes_no was NEVER called, so a future
        # refactor that unconditionally prompts gets caught.
        monkeypatch.setattr("schemabrain.cli._stderr_is_interactive_tty", lambda: False)
        prompt_calls: list[str] = []

        def fake_prompt(question: str, *, default: bool) -> bool:
            prompt_calls.append(question)
            return True

        monkeypatch.setattr("schemabrain.cli._prompt_yes_no", fake_prompt)
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
        assert prompt_calls == []


class TestInitCliSkipIndexRender:
    """When init succeeds via skip_index, the 'Next:' message must
    tell the user to index BEFORE asking the agent — otherwise the
    suggested 'list the entities' question returns nothing."""

    def test_skip_index_render_includes_index_before_querying(
        self,
        tmp_path: Path,
        stub_uvx: None,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Empty store + --skip-index → init writes the host config and
        # the render must include the "Before querying: schemabrain index"
        # hint instead of the unconditional "list the entities" line.
        store_path = tmp_path / "store.db"
        SQLiteStore(path=store_path).close()
        claude_dir = tmp_path / "Claude"
        claude_dir.mkdir()
        cfg = claude_dir / "claude_desktop_config.json"
        monkeypatch.setattr("schemabrain.setup.init_flow.claude_desktop_config_path", lambda: cfg)
        exit_code = main(
            [
                "init",
                "--source",
                "sqlite:///:memory:",
                "--store-path",
                str(store_path),
                "--host",
                "claude-desktop",
                "--skip-index",
            ]
        )
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "Before querying" in captured.err
        assert "schemabrain index" in captured.err

    def test_normal_init_render_does_not_include_index_hint(
        self,
        seeded_store: Path,
        stub_uvx: None,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Seeded store (entities present) + no --skip-index → the
        # unchanged single-line "Next:" message.
        claude_dir = seeded_store.parent / "Claude"
        claude_dir.mkdir()
        cfg = claude_dir / "claude_desktop_config.json"
        monkeypatch.setattr("schemabrain.setup.init_flow.claude_desktop_config_path", lambda: cfg)
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
        # The skip-index hint should NOT appear.
        assert "Before querying" not in captured.err
        assert "list the entities" in captured.err


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

    def test_three_runs_preserve_original_backup_byte_stable(
        self,
        seeded_store: Path,
        stub_uvx: None,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # End-to-end three-run sequence exercising the backup-once
        # idempotency contract through the init() orchestrator. A
        # refactor that re-anchored the .bak pointer between runs
        # would silently destroy the user's rollback target; this
        # test catches that.
        cfg_parent = tmp_path / "Claude"
        cfg_parent.mkdir()
        cfg = cfg_parent / "claude_desktop_config.json"
        backup_path = cfg.parent / (cfg.name + ".bak")
        # Pre-seed an "original" config — the .bak MUST capture this
        # forever, regardless of subsequent runs.
        cfg.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "schemabrain": {
                            "command": "uvx",
                            "args": ["schemabrain==0.0.99", "serve"],
                            "env": {},
                        }
                    }
                }
            )
        )
        original_contents = cfg.read_text()
        monkeypatch.setattr("schemabrain.setup.init_flow.claude_desktop_config_path", lambda: cfg)
        # Run 1: overwrites with --yes; .bak created holding original.
        main(
            [
                "init",
                "--source",
                "sqlite:///:memory:",
                "--store-path",
                str(seeded_store),
                "--host",
                "claude-desktop",
                "--yes",
            ]
        )
        assert backup_path.exists()
        assert backup_path.read_text() == original_contents
        # Run 2: identical args; no-op.
        main(
            [
                "init",
                "--source",
                "sqlite:///:memory:",
                "--store-path",
                str(seeded_store),
                "--host",
                "claude-desktop",
                "--yes",
            ]
        )
        assert backup_path.read_text() == original_contents
        # Run 3: change the env-var name to force a rewrite; --yes
        # again. Backup MUST still hold the original from run 1.
        main(
            [
                "init",
                "--source",
                "sqlite:///:memory:",
                "--store-path",
                str(seeded_store),
                "--host",
                "claude-desktop",
                "--env-var",
                "CHANGED_ENV_NAME",
                "--yes",
            ]
        )
        assert backup_path.read_text() == original_contents
