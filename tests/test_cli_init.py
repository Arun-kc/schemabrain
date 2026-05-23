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
    # Stub `_is_pypi_install` to True alongside `shutil.which` so the
    # resolver picks the uvx path. The actual test environment is an
    # editable checkout (PEP 610 `direct_url.json` present), which the
    # resolver would otherwise treat as non-PyPI and fall through to the
    # absolute-path runner.
    monkeypatch.setattr(
        "shutil.which",
        lambda n: "/usr/local/bin/uvx" if n == "uvx" else None,
    )
    monkeypatch.setattr(
        "schemabrain.setup.init_flow._is_pypi_install",
        lambda: True,
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
        # The wizard's renderer prints the common config paths block
        # next to the manual-mode (stage-4 `printed_only`) snippet,
        # and the closing block points the user at the snippet they
        # were just shown.
        assert "Common config paths" in captured.err
        assert "Add the snippet" in captured.err

    def test_print_only_emits_manual_mode_header(
        self,
        seeded_store: Path,
        stub_uvx: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Polish: render the Schema Brain wordmark + manual-mode
        # orientation + "Add this to your MCP host's config:" so the
        # JSON block has a labelled top, not just naked output.
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
        assert "Schema Brain" in captured.err
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

    def test_empty_store_with_non_postgres_source_succeeds(
        self, tmp_path: Path, stub_uvx: None
    ) -> None:
        # New wizard contract: a SQLite source skips stages 2+3
        # (indexing only supports Postgres today) and still wires
        # the host so the user gets a working snippet.
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
        assert exit_code == 0

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


class TestInitYesSkipsStageZero:
    """Round-3 live-test fix (bug B): `--yes` must skip the stage-0
    demo/own-DB fork prompt. Previously a CI run with `--yes` and a
    `SCHEMABRAIN_DATABASE_URL` env var (but no `--url-env` flag) would
    still hit the fork prompt — the default `[2]` (demo) would then
    silently override the operator's intended URL with the pinned
    demo URL. That's the worst kind of CI bug: works interactively,
    fails differently in automation.

    The fix gates stage 0 on `not assume_yes`, matching the rest of
    the wizard's `--yes` contract (no prompts ever).
    """

    def test_yes_skips_stage_zero_even_without_url_env_flag(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # User passed --yes but no --url-env and no --source. Without
        # the fix, stage 0 fires and prompts. With the fix, stage 0
        # is skipped and `_resolve_url_source` renders its standard
        # guided error.
        prompted = {"called": False}

        def fake_prompt(*args: object, **kwargs: object) -> str | None:
            prompted["called"] = True
            return "should-not-be-returned"

        monkeypatch.setattr("schemabrain.setup.setup_stage.prompt_for_init_setup", fake_prompt)
        # Force TTY so the only thing gating stage 0 is the new --yes check.
        monkeypatch.setattr("schemabrain.cli._stderr_is_interactive_tty", lambda: True)
        exit_code = main(["init", "--yes"])
        assert exit_code == 2
        assert prompted["called"] is False
        # Falls through to the standard "no URL provided" guided error.
        assert "no connection URL provided" in capsys.readouterr().err


class TestInitStageZeroAbortPaths:
    """Round-2 reviewer fold: stage 0 prompt must catch BOTH
    KeyboardInterrupt (Ctrl-C) AND EOFError (stdin closed mid-prompt,
    SSH-drop, terminal recorder redirecting input). Both produce
    exit 130 + clean "aborted." on stderr — never a Python traceback
    from rich.prompt.Prompt.ask.

    Without these tests, the empty-state branches in `_cmd_init`
    (lines 4956-4970) sit uncovered and a future refactor could
    drop the EOFError arm without anyone noticing until a user
    hits it in production.
    """

    def test_stage_zero_keyboard_interrupt_exits_130(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        def fake_prompt(*args: object, **kwargs: object) -> str | None:
            raise KeyboardInterrupt

        monkeypatch.setattr("schemabrain.setup.setup_stage.prompt_for_init_setup", fake_prompt)
        monkeypatch.setattr("schemabrain.cli._stderr_is_interactive_tty", lambda: True)
        exit_code = main(["init"])
        assert exit_code == 130
        assert "aborted." in capsys.readouterr().err

    def test_stage_zero_eof_error_exits_130(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        def fake_prompt(*args: object, **kwargs: object) -> str | None:
            raise EOFError

        monkeypatch.setattr("schemabrain.setup.setup_stage.prompt_for_init_setup", fake_prompt)
        monkeypatch.setattr("schemabrain.cli._stderr_is_interactive_tty", lambda: True)
        exit_code = main(["init"])
        assert exit_code == 130
        assert "aborted." in capsys.readouterr().err

    def test_stage_zero_returns_url_then_silent_rewrite_applies(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Stage 0 returns a bare postgresql:// URL (the demo path
        # default). The silent_rewrite_to_psycopg branch on line 4994
        # must fire, transforming the URL before it reaches the
        # wizard. We verify the rewrite indirectly by checking that
        # the wizard receives the rewritten URL (or fails downstream
        # with a postgresql+psycopg:// error, not a psycopg2 import
        # error).
        bare_demo_url = "postgresql://postgres:local@localhost:5433/postgres"

        def fake_prompt(*args: object, **kwargs: object) -> str | None:
            return bare_demo_url

        received: dict[str, str] = {}

        def fake_run_wizard(config: object, *args: object, **kwargs: object) -> object:
            # `run_default_wizard(config, ...)` — config is a
            # WizardConfig dataclass; the source URL lives on
            # `config.source` (or .source_url depending on version).
            url = getattr(config, "source", None) or getattr(config, "source_url", "")
            received["url"] = str(url)
            # Short-circuit before the wizard pipeline; the URL
            # assertion is the load-bearing check.
            raise KeyboardInterrupt

        monkeypatch.setattr("schemabrain.setup.setup_stage.prompt_for_init_setup", fake_prompt)
        monkeypatch.setattr("schemabrain.cli._stderr_is_interactive_tty", lambda: True)
        # Stub the post-stage-0 PII-block prompt so it doesn't block
        # on stdin when forced-TTY mode is active.
        monkeypatch.setattr(
            "schemabrain.setup.setup_stage.prompt_for_pii_block",
            lambda *, console: ("contact",),
        )
        monkeypatch.setattr("schemabrain.setup.wizard.run_default_wizard", fake_run_wizard)
        # KeyboardInterrupt from the wizard itself bubbles up to main()
        # and exits 130 — same shape as stage 0 abort.
        exit_code = main(["init"])
        assert exit_code == 130
        # The wizard received the rewritten URL with +psycopg, not
        # the bare scheme. This proves line 4994-4996 fired.
        assert received["url"].startswith("postgresql+psycopg://")


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
        # F3: TTY check now lives in `_ui.stderr_is_interactive_tty`
        # — single source of truth used by both `cli` and `wizard`.
        # Tests just monkeypatch that one function.
        monkeypatch.setattr("schemabrain._ui.stderr_is_interactive_tty", lambda: True)
        # The init wizard now surfaces an interactive PII-block choice
        # before constructing the WizardConfig. Tests that force TTY
        # mode would otherwise hang on stdin at the PII prompt; stub
        # it to the wizard's `("contact",)` default so test behavior
        # matches the pre-prompt era exactly.
        monkeypatch.setattr(
            "schemabrain.setup.setup_stage.prompt_for_pii_block",
            lambda *, console: ("contact",),
        )
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
        # F3: prompt now fires from the wizard, not from cli's
        # retry-loop — monkeypatch the lifted helper. The wizard
        # imports `prompt_yes_no` from `_ui`, so the patch must
        # target the binding visible inside `wizard`.
        monkeypatch.setattr("schemabrain.setup.wizard.prompt_yes_no", lambda *_a, **_kw: True)
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
        # F3: prompt now fires from the wizard — monkeypatch the
        # lifted helper at the binding inside `wizard`.
        monkeypatch.setattr("schemabrain.setup.wizard.prompt_yes_no", lambda *_a, **_kw: False)
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
        # Exit 0 — graceful cancel, not refusal. F3 preserves the
        # pre-F3 contract: the "cancelled by user" sentinel-prefix
        # message tells `_cmd_init` to map the aborted wizard back
        # to exit 0.
        assert exit_code == 0
        # File untouched.
        assert cfg.read_text() == original
        captured = capsys.readouterr()
        assert "cancelled" in captured.err

    def test_non_interactive_does_not_prompt_for_empty_store(
        self,
        tmp_path: Path,
        stub_uvx: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The empty-store interactive prompt was removed when init
        # became the wizard — stages 2/3 handle the empty-store case
        # automatically (auto-run on Postgres / skip on SQLite). The
        # only remaining interactive prompt covers the entry-exists
        # case at stage 4. Verify no prompt fires on a clean SQLite
        # + manual run.
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
        assert exit_code == 0
        assert prompt_calls == []


class TestInitCliSkipIndexRender:
    """The wizard's stage-2 outcome on `--skip-index` must surface
    the recovery hint pointing at the standalone `schemabrain index`
    command, so a user who deferred indexing knows what to run before
    asking the agent for data."""

    def test_skip_index_render_surfaces_index_hint(
        self,
        tmp_path: Path,
        stub_uvx: None,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Empty store + --skip-index → wizard skips stage 2 with a
        # `⊘ --skip-index set; not running indexer` outcome whose
        # `next_step` points at `schemabrain index`. (Skipped glyph
        # flipped from `↷` to `⊘` in PR #3 per the design spec.)
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
        assert "--skip-index set" in captured.err
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


class TestWizardRenderer:
    """Direct tests for `_render_wizard_result` covering the paths
    that are awkward to drive end-to-end through `main()` — aborted
    runs, shell-out-failed cleanup, the closing note."""

    def _make_outcome(
        self,
        stage: int,
        name: str,
        status: str,
        message: str,
        next_step: str | None = None,
    ) -> object:
        from schemabrain.setup.wizard import StageOutcome

        return StageOutcome(
            stage=stage,
            name=name,
            status=status,  # type: ignore[arg-type]
            message=message,
            next_step=next_step,
        )

    def test_aborted_run_renders_failure_panel(self, capsys: pytest.CaptureFixture[str]) -> None:
        from schemabrain.cli import _render_wizard_result
        from schemabrain.setup.wizard import WizardResult

        result = WizardResult(
            outcomes=(
                self._make_outcome(1, "source_check", "done", "source reachable"),
                self._make_outcome(
                    2,
                    "index",
                    "failed",
                    "source unreachable mid-index",
                    "verify the URL and retry",
                ),
            ),
            aborted=True,
        )
        _render_wizard_result(result)
        captured = capsys.readouterr()
        # Bordered panel — title carries the stage ordinal, body
        # carries the failure message + recovery hint.
        assert "Stopped at stage 2 of 7" in captured.err
        assert "source unreachable mid-index" in captured.err
        assert "verify the URL and retry" in captured.err

    def test_aborted_wire_host_stage_without_host_install_result(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from schemabrain.cli import _render_wizard_result
        from schemabrain.setup.wizard import WizardResult

        result = WizardResult(
            outcomes=(
                self._make_outcome(1, "source_check", "done", "ok"),
                self._make_outcome(2, "index", "skipped", "skipped"),
                self._make_outcome(3, "entities", "skipped", "skipped"),
                self._make_outcome(4, "metrics", "skipped", "skipped"),
                self._make_outcome(5, "joins", "skipped", "skipped"),
                self._make_outcome(
                    6, "wire_host", "failed", "host unavailable", "install host first"
                ),
            ),
            aborted=True,
            host_install_result=None,
        )
        _render_wizard_result(result)
        captured = capsys.readouterr()
        # Abort renders "stage 6 of 7" (denominator is the total
        # pipeline shape, not the count of outcomes seen).
        assert "Stopped at stage 6 of 7" in captured.err

    def test_stage_context_no_op_for_fast_stages(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Fast stages (source_check, wire_host, next_step) skip the
        # spinner entirely — early-return before touching the console.
        from schemabrain.cli import _wizard_stage_context

        class _StubConsole:
            is_terminal = True

            def status(self, *_a: object, **_kw: object) -> None:  # pragma: no cover - guard
                raise AssertionError("status should not be called for fast stages")

        monkeypatch.setattr("schemabrain.cli._stderr_console", lambda: _StubConsole())

        class _FakeStage:
            def __init__(self, name: str) -> None:
                self.name = name

        for name in ("source_check", "wire_host", "next_step"):
            with _wizard_stage_context(_FakeStage(name)):
                pass

    def test_stage_context_no_op_when_stderr_is_not_tty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Non-TTY stderr (CI logs, redirected output) skips the spinner
        # so log scrapers don't get carriage-return noise.
        from schemabrain.cli import _wizard_stage_context

        class _NonTtyConsole:
            is_terminal = False

            def status(self, *_a: object, **_kw: object) -> None:  # pragma: no cover - guard
                raise AssertionError("status should not be called when not a TTY")

        monkeypatch.setattr("schemabrain.cli._stderr_console", lambda: _NonTtyConsole())

        class _FakeStage:
            name = "index"

        with _wizard_stage_context(_FakeStage()):
            pass

    def test_stage_context_invokes_status_on_tty_for_slow_stages(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # On a TTY, slow stages (index, entities, metrics) get the
        # spinner via console.status with the dots spinner. `metrics`
        # was added after smoke 2026-05-19 surfaced stage 4 looking
        # frozen for ~56s; the test pins all three so a future
        # regression here is loud.
        from schemabrain.cli import _wizard_stage_context

        class _RecordingStatus:
            # Round-2 fold: include no-op `start()` / `stop()` so the
            # stub also satisfies `_ui._PausableSpinner`. The current
            # test only exercises the context-manager path, but the
            # production code now registers this same status with
            # `register_active_spinner(status)`, which would call
            # `.stop()` if `_prompt_llm_confirmation` ran inside the
            # block. Pre-emptive completeness so a future test
            # expanding the pause-path coverage doesn't hit an
            # AttributeError on a stub that looked complete.
            def __init__(self, sink: dict[str, object]) -> None:
                self._sink = sink

            def __enter__(self) -> _RecordingStatus:
                self._sink["entered"] = True
                return self

            def __exit__(self, *exc: object) -> None:
                self._sink["exited"] = True

            def start(self) -> None: ...

            def stop(self) -> None: ...

        class _TtyConsole:
            is_terminal = True

            def __init__(self, sink: dict[str, object]) -> None:
                self._sink = sink

            def status(self, text: str, *, spinner: str) -> _RecordingStatus:
                self._sink["text"] = text
                self._sink["spinner"] = spinner
                return _RecordingStatus(self._sink)

        expected_labels = {
            "index": "Index schema",
            "entities": "Curate entities",
            "metrics": "Curate metrics",
        }

        for stage_name, expected_label in expected_labels.items():
            captured: dict[str, object] = {}
            monkeypatch.setattr(
                "schemabrain.cli._stderr_console", lambda sink=captured: _TtyConsole(sink)
            )

            class _FakeStage:
                name = stage_name

            with _wizard_stage_context(_FakeStage()):
                pass

            assert captured["entered"] is True, f"stage {stage_name!r} did not enter spinner"
            assert captured["exited"] is True, f"stage {stage_name!r} did not exit spinner"
            assert captured["spinner"] == "dots"
            assert expected_label in str(captured["text"]), f"stage {stage_name!r} label mismatch"

    def test_stage_context_unknown_stage_name_skips_spinner(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A stage whose `name` attribute is missing (defensive) falls
        # into the no-op path rather than crashing.
        from schemabrain.cli import _wizard_stage_context

        class _GuardConsole:
            is_terminal = True

            def status(self, *_a: object, **_kw: object) -> None:  # pragma: no cover - guard
                raise AssertionError("status should not be called for unknown stage")

        monkeypatch.setattr("schemabrain.cli._stderr_console", lambda: _GuardConsole())

        class _BareStage:
            pass

        with _wizard_stage_context(_BareStage()):
            pass

    def test_stage_panel_caps_width_at_soft_limit(self, capsys: pytest.CaptureFixture[str]) -> None:
        # A long failure message must not blow the panel out past the
        # soft cap (_STAGE_PANEL_MAX_WIDTH = 100). Regression for the
        # post-PR-#65 smoke finding where a 200+ char store-version
        # error rendered as a 200-col-wide panel that horizontal-
        # scrolled on most terminals.
        from schemabrain.cli import _render_wizard_result
        from schemabrain.setup.wizard import WizardResult

        long_message = (
            "Store schema version '2' does not match expected '12'. "
            "Schema Brain is pre-alpha and does not yet provide migrations "
            "— delete or move the store file (path passed to SQLiteStore) "
            "and re-run `schemabrain index` to rebuild from scratch."
        )
        result = WizardResult(
            outcomes=(
                self._make_outcome(1, "source_check", "done", "ok"),
                self._make_outcome(
                    2,
                    "index",
                    "failed",
                    long_message,
                    "delete schemabrain.db and re-run",
                ),
            ),
            aborted=True,
        )
        _render_wizard_result(result)
        captured = capsys.readouterr()
        # No line in the stage Table.grid OR the abort Panel may
        # exceed the soft cap (with a few cells of padding slack).
        # `_STAGE_PANEL_MAX_WIDTH = 100` is now passed as the
        # `width=` argument to the stage `Table.grid` and the
        # `_wizard_panel_width` of the abort Panel. Any line wider
        # than ~104 cells means the cap was ignored.
        max_line = max(len(ln) for ln in captured.err.splitlines() if ln)
        assert max_line <= 104, (
            f"Expected all rendered lines ≤ 104 cells (soft cap = 100); longest line was {max_line}"
        )
        # And the long message must still appear in full — wrapping
        # within the panel is fine, truncation is not. Asserting on
        # short non-wrapping tokens at the start, middle, and end of
        # the body confirms nothing was dropped.
        assert "Store schema version" in captured.err
        assert "migrations" in captured.err
        assert "scratch" in captured.err

    def test_failure_panel_omitted_on_clean_run(self, capsys: pytest.CaptureFixture[str]) -> None:
        # Clean (non-aborted) runs must NOT render the failure panel —
        # the closing block carries the next-step copy instead.
        from schemabrain.cli import _render_wizard_result
        from schemabrain.setup.wizard import WizardResult

        result = WizardResult(
            outcomes=(
                self._make_outcome(1, "source_check", "done", "ok"),
                self._make_outcome(2, "index", "done", "indexed"),
                self._make_outcome(3, "entities", "skipped", "skipped"),
                self._make_outcome(4, "metrics", "skipped", "skipped"),
                self._make_outcome(5, "joins", "skipped", "skipped"),
                self._make_outcome(6, "wire_host", "done", "wired"),
                self._make_outcome(7, "next_step", "done", "Ready"),
            ),
            aborted=False,
            host_install_result=self._printed_only_host_result(),  # type: ignore[arg-type]
        )
        _render_wizard_result(result, host_display="manual mode")
        captured = capsys.readouterr()
        assert "Stopped at stage" not in captured.err

    def test_shell_out_failed_renders_redacted_argv(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from schemabrain.cli import _render_wizard_result
        from schemabrain.setup.hosts import SchemabrainSnippet
        from schemabrain.setup.init_flow import InitResult
        from schemabrain.setup.wizard import WizardResult

        snippet = SchemabrainSnippet(
            command="uvx",
            args=("schemabrain==0.2.0a1", "serve"),
            env={"SCHEMABRAIN_DATABASE_URL": "postgresql://u:secret@h/d"},
        )
        host_result = InitResult(
            host="claude-code",
            snippet=snippet,
            state="shell_out_failed",
            shell_out_command=(
                "claude",
                "mcp",
                "add",
                "schemabrain",
                "-e",
                "SCHEMABRAIN_DATABASE_URL=postgresql://u:secret@h/d",
            ),
            shell_out_stderr="claude: command not found",
        )
        result = WizardResult(
            outcomes=(
                self._make_outcome(1, "source_check", "done", "ok"),
                self._make_outcome(2, "index", "skipped", "skipped"),
                self._make_outcome(3, "entities", "skipped", "skipped"),
                self._make_outcome(4, "metrics", "skipped", "skipped"),
                self._make_outcome(5, "joins", "skipped", "skipped"),
                self._make_outcome(
                    6,
                    "wire_host",
                    "done",
                    "Claude Code registration failed; the snippet is printable below",
                ),
                self._make_outcome(7, "next_step", "done", "Ready"),
            ),
            aborted=False,
            host_install_result=host_result,
        )
        _render_wizard_result(result)
        captured = capsys.readouterr()
        # Env value redacted, argv visible.
        assert "<redacted>" in captured.err
        assert "secret" not in captured.err
        # The stderr field is preserved.
        assert "claude: command not found" in captured.err
        # The closing note fires on shell_out_failed.
        assert "register manually" in captured.err

    def test_written_path_renders_wrote_and_backup(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from schemabrain.cli import _render_wizard_result
        from schemabrain.setup.hosts import SchemabrainSnippet
        from schemabrain.setup.init_flow import InitResult
        from schemabrain.setup.wizard import WizardResult

        snippet = SchemabrainSnippet(command="uvx", args=("schemabrain==0.2.0a1", "serve"), env={})
        cfg_path = tmp_path / "Claude" / "claude_desktop_config.json"
        cfg_path.parent.mkdir()
        host_result = InitResult(
            host="claude-desktop",
            snippet=snippet,
            state="written",
            config_path=cfg_path,
            backup_made=True,
        )
        result = WizardResult(
            outcomes=(
                self._make_outcome(1, "source_check", "done", "ok"),
                self._make_outcome(2, "index", "done", "53 tables · 412 columns indexed"),
                self._make_outcome(3, "entities", "done", "8 entities created (cost $0.0123)"),
                self._make_outcome(4, "metrics", "done", "6 metrics created (cost $0.0080)"),
                self._make_outcome(
                    5, "joins", "done", "5 canonical joins created from FK + query-log evidence"
                ),
                self._make_outcome(
                    6, "wire_host", "done", f"wrote schemabrain entry to {cfg_path}"
                ),
                self._make_outcome(7, "next_step", "done", "Ready"),
            ),
            aborted=False,
            host_install_result=host_result,
        )
        _render_wizard_result(result)
        captured = capsys.readouterr()
        assert "wrote:" in captured.err
        assert "backup:" in captured.err

    def test_written_path_no_backup_omits_backup_line(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # First-time write — `.bak` not created. The renderer must
        # still show the wrote: line but skip the backup: line.
        from schemabrain.cli import _render_wizard_result
        from schemabrain.setup.hosts import SchemabrainSnippet
        from schemabrain.setup.init_flow import InitResult
        from schemabrain.setup.wizard import WizardResult

        snippet = SchemabrainSnippet(command="uvx", args=("schemabrain==0.2.0a1", "serve"), env={})
        cfg_path = tmp_path / "claude_desktop_config.json"
        host_result = InitResult(
            host="claude-desktop",
            snippet=snippet,
            state="written",
            config_path=cfg_path,
            backup_made=False,
        )
        result = WizardResult(
            outcomes=(self._make_outcome(6, "wire_host", "done", "wrote schemabrain entry"),),
            aborted=False,
            host_install_result=host_result,
        )
        _render_wizard_result(result)
        captured = capsys.readouterr()
        assert "wrote:" in captured.err
        assert "backup:" not in captured.err

    def test_render_rejects_non_wizard_result(self) -> None:
        from schemabrain.cli import _render_wizard_result

        with pytest.raises(TypeError, match="WizardResult"):
            _render_wizard_result("not a wizard result")

    def test_wordmark_header_two_lines_with_host_display(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from schemabrain.cli import _render_wizard_result
        from schemabrain.setup.wizard import WizardResult

        result = WizardResult(
            outcomes=(
                self._make_outcome(1, "source_check", "done", "ok"),
                self._make_outcome(2, "index", "skipped", "skipped"),
                self._make_outcome(3, "entities", "skipped", "skipped"),
                self._make_outcome(4, "metrics", "skipped", "skipped"),
                self._make_outcome(5, "joins", "skipped", "skipped"),
                self._make_outcome(6, "wire_host", "done", "ok"),
                self._make_outcome(7, "next_step", "done", "ok"),
            ),
            aborted=False,
        )
        _render_wizard_result(result, host_display="Claude Desktop")
        captured = capsys.readouterr()
        # Wordmark line stands alone (no "init" suffix), orientation
        # line mentions the host target.
        assert "Schema Brain" in captured.err
        assert "Claude Desktop" in captured.err
        # Orientation duration hint sets expectations.
        assert "~" in captured.err

    def test_wordmark_header_falls_back_when_host_display_unset(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from schemabrain.cli import _render_wizard_result
        from schemabrain.setup.wizard import WizardResult

        result = WizardResult(
            outcomes=(self._make_outcome(1, "source_check", "done", "ok"),),
            aborted=False,
        )
        # Renderer accepts a None / unset host_display (early-abort
        # paths invoke render before stage 4 had a chance to set host).
        _render_wizard_result(result)
        captured = capsys.readouterr()
        assert "Schema Brain" in captured.err
        # Generic orientation — no host name promised.
        assert "Claude Desktop" not in captured.err
        assert "Claude Code" not in captured.err

    def test_host_display_name_maps_kebab_to_title(self) -> None:
        from schemabrain.cli import _host_display_name

        assert _host_display_name("claude-desktop") == "Claude Desktop"
        assert _host_display_name("claude-code") == "Claude Code"
        assert _host_display_name("manual") == "manual mode"

    def test_format_path_replaces_home_with_tilde(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from schemabrain.cli import _format_path_for_terminal

        monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
        nested = tmp_path / "Library" / "config.json"
        assert _format_path_for_terminal(nested) == "~/Library/config.json"

    def test_format_path_returns_short_paths_unchanged(self) -> None:
        from schemabrain.cli import _format_path_for_terminal

        # A path well under the soft cap renders as-is (after the
        # tilde substitution — short relative paths skip both).
        assert _format_path_for_terminal(Path("/tmp/short.json")) == "/tmp/short.json"

    def test_format_path_left_truncates_long_paths(self) -> None:
        from schemabrain.cli import _format_path_for_terminal

        # 100+ char macOS-style path falls past the 60-char soft cap.
        # The result must (a) start with `…/`, (b) preserve the last
        # 3 path components, (c) be visibly shorter than the input.
        long_path = Path(
            "/Users/someverylongname/Library/Application Support/Claude/claude_desktop_config.json"
        )
        result = _format_path_for_terminal(long_path)
        assert result.startswith("…/")
        assert result.endswith("claude_desktop_config.json")
        assert "Application Support/Claude/claude_desktop_config.json" in result
        assert len(result) < len(str(long_path))

    def test_format_path_skips_truncation_for_shallow_long_path(self) -> None:
        from schemabrain.cli import _format_path_for_terminal

        # A path with ≤3 components can't be left-truncated meaningfully —
        # render whatever the home-substitution produced.
        shallow = Path("/" + "x" * 100 + ".json")  # one component, longer than the cap
        result = _format_path_for_terminal(shallow)
        # No `…/` prefix because there's nothing to trim.
        assert "…/" not in result

    def test_host_display_name_unknown_returns_input(self) -> None:
        # Defensive: the literal is enforced at WizardConfig
        # construction time, but the renderer's lookup falls through to
        # the raw input rather than crashing.
        from schemabrain.cli import _host_display_name

        assert _host_display_name("future-host") == "future-host"

    def test_renderer_shows_per_stage_duration(self, capsys: pytest.CaptureFixture[str]) -> None:
        # Each stage's `duration_s` renders as a 1-decimal "X.Xs"
        # string near the stage header so the operator sees how long
        # each step spent without enabling verbose mode.
        from schemabrain.cli import _render_wizard_result
        from schemabrain.setup.wizard import StageOutcome, WizardResult

        result = WizardResult(
            outcomes=(
                StageOutcome(
                    stage=1,
                    name="source_check",
                    status="done",
                    message="ok",
                    duration_s=0.42,
                ),
                StageOutcome(
                    stage=2,
                    name="index",
                    status="done",
                    message="ok",
                    duration_s=6.1,
                ),
            ),
            aborted=False,
        )
        _render_wizard_result(result)
        captured = capsys.readouterr()
        assert "0.4s" in captured.err
        assert "6.1s" in captured.err

    def test_renderer_omits_duration_for_zero_value(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A 0.0s duration means the stage didn't actually do work (a
        # skipped peek-and-bypass). Rendering "0.0s" next to a skipped
        # stage row would be misleading — the time spent on that line
        # is effectively unmeasurable.
        #
        # The fixture mixes a 0.0s stage with a 0.5s stage so the
        # progress rule's aggregate elapsed time has a non-zero
        # value (preventing a spurious "0.0s" hit from the rule
        # itself). The assertion fences the per-row contract: the
        # skipped row carries no duration cell, the worked row does.
        from schemabrain.cli import _render_wizard_result
        from schemabrain.setup.wizard import StageOutcome, WizardResult

        result = WizardResult(
            outcomes=(
                StageOutcome(
                    stage=1,
                    name="source_check",
                    status="skipped",
                    message="ok",
                    duration_s=0.0,
                ),
                StageOutcome(
                    stage=2,
                    name="index",
                    status="done",
                    message="ok",
                    duration_s=0.5,
                ),
            ),
            aborted=False,
        )
        _render_wizard_result(result)
        captured = capsys.readouterr()
        assert "0.0s" not in captured.err
        # And the non-zero-duration stage's row still carries its
        # duration — the suppression is per-row, not global.
        assert "0.5s" in captured.err

    def _written_host_result(self, tmp_path: Path) -> object:
        from schemabrain.setup.hosts import SchemabrainSnippet
        from schemabrain.setup.init_flow import InitResult

        snippet = SchemabrainSnippet(command="uvx", args=("schemabrain==0.2.0a1", "serve"), env={})
        return InitResult(
            host="claude-desktop",
            snippet=snippet,
            state="written",
            config_path=tmp_path / "claude_desktop_config.json",
            backup_made=False,
        )

    def _printed_only_host_result(self) -> object:
        from schemabrain.setup.hosts import SchemabrainSnippet
        from schemabrain.setup.init_flow import InitResult

        snippet = SchemabrainSnippet(command="uvx", args=("schemabrain==0.2.0a1", "serve"), env={})
        return InitResult(host="manual", snippet=snippet, state="printed_only")

    def _full_clean_outcomes(self) -> tuple[object, ...]:
        return (
            self._make_outcome(1, "source_check", "done", "ok"),
            self._make_outcome(2, "index", "done", "indexed"),
            self._make_outcome(3, "entities", "skipped", "skipped"),
            self._make_outcome(4, "metrics", "skipped", "skipped"),
            self._make_outcome(5, "joins", "skipped", "skipped"),
            self._make_outcome(6, "wire_host", "done", "wired"),
            self._make_outcome(7, "next_step", "done", "Ready"),
        )

    def test_closing_block_renders_on_written_host(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from schemabrain.cli import _render_wizard_result
        from schemabrain.setup.wizard import WizardResult

        result = WizardResult(
            outcomes=self._full_clean_outcomes(),  # type: ignore[arg-type]
            aborted=False,
            host_install_result=self._written_host_result(tmp_path),  # type: ignore[arg-type]
        )
        _render_wizard_result(result, host_display="Claude Desktop")
        captured = capsys.readouterr()
        # Automated host gets "Restart …, then ask:" copy.
        assert "Restart Claude Desktop" in captured.err
        # UX audit #12: config path shown so the operator knows where the
        # entry landed without scrolling back to stage 6 or running doctor.
        assert "config written:" in captured.err
        # Long tmp_path values soft-wrap inside Rich's width — assert on
        # the basename which always survives the wrap as a contiguous span.
        assert "claude_desktop_config.json" in captured.err
        # Tail + audit hints are part of the closing block.
        assert "schemabrain tail" in captured.err
        assert "schemabrain audit list" in captured.err
        # Thesis tagline closes the block.
        assert "The agent reads. It doesn't write." in captured.err

    def _unchanged_host_result(self, tmp_path: Path) -> object:
        from schemabrain.setup.hosts import SchemabrainSnippet
        from schemabrain.setup.init_flow import InitResult

        snippet = SchemabrainSnippet(command="uvx", args=("schemabrain==0.2.0a1", "serve"), env={})
        return InitResult(
            host="claude-desktop",
            snippet=snippet,
            state="unchanged",
            config_path=tmp_path / "claude_desktop_config.json",
            backup_made=False,
        )

    def test_closing_block_renders_on_unchanged_host(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Idempotent re-run: stage 4 detected no diff, host_result.state
        # is "unchanged". The closing block must still render — it
        # carries the actionable next-step copy regardless of whether
        # the run made changes.
        from schemabrain.cli import _render_wizard_result
        from schemabrain.setup.wizard import WizardResult

        result = WizardResult(
            outcomes=self._full_clean_outcomes(),  # type: ignore[arg-type]
            aborted=False,
            host_install_result=self._unchanged_host_result(tmp_path),  # type: ignore[arg-type]
        )
        _render_wizard_result(result, host_display="Claude Desktop")
        captured = capsys.readouterr()
        assert "Restart Claude Desktop" in captured.err
        assert "The agent reads. It doesn't write." in captured.err
        # UX audit #12: config path also surfaces on the unchanged path
        # (operator re-ran init; they should still be able to see where
        # the entry is so `cat config.json` works without hunting).
        assert "config written:" in captured.err

    def test_closing_block_renders_manual_copy_on_printed_only(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from schemabrain.cli import _render_wizard_result
        from schemabrain.setup.wizard import WizardResult

        result = WizardResult(
            outcomes=self._full_clean_outcomes(),  # type: ignore[arg-type]
            aborted=False,
            host_install_result=self._printed_only_host_result(),  # type: ignore[arg-type]
        )
        _render_wizard_result(result, host_display="manual mode")
        captured = capsys.readouterr()
        # Manual mode never restarts a host — copy points at the
        # snippet the user just received instead.
        assert "Restart" not in captured.err
        assert "Add the snippet" in captured.err
        # Tail + audit hints + thesis tagline still apply.
        assert "schemabrain tail" in captured.err
        assert "The agent reads. It doesn't write." in captured.err
        # UX audit #12: manual mode has no operator-visible config file —
        # the "config written:" line must NOT render (would point at None).
        assert "config written:" not in captured.err

    def test_closing_block_omitted_on_abort(self, capsys: pytest.CaptureFixture[str]) -> None:
        from schemabrain.cli import _render_wizard_result
        from schemabrain.setup.wizard import WizardResult

        result = WizardResult(
            outcomes=(
                self._make_outcome(1, "source_check", "done", "ok"),
                self._make_outcome(2, "index", "failed", "boom", "verify the URL and retry"),
            ),
            aborted=True,
        )
        _render_wizard_result(result, host_display="Claude Desktop")
        captured = capsys.readouterr()
        # Aborted runs land in the failure path (commit 4 — panel);
        # the clean-run closing block must not appear.
        assert "The agent reads. It doesn't write." not in captured.err
        assert "schemabrain tail" not in captured.err

    def test_closing_block_omitted_on_shell_out_failed(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # `shell_out_failed` is a non-fatal stage-4 outcome (aborted=False)
        # but the auto-register attempt didn't work — directing the user
        # to "Restart Claude Code" would be misleading. The existing
        # "register manually" Note covers the recovery; the clean-run
        # closing block must defer to it.
        from schemabrain.cli import _render_wizard_result
        from schemabrain.setup.hosts import SchemabrainSnippet
        from schemabrain.setup.init_flow import InitResult
        from schemabrain.setup.wizard import WizardResult

        snippet = SchemabrainSnippet(
            command="uvx",
            args=("schemabrain==0.2.0a1", "serve"),
            env={"SCHEMABRAIN_DATABASE_URL": "postgresql://u:p@h/d"},
        )
        host_result = InitResult(
            host="claude-code",
            snippet=snippet,
            state="shell_out_failed",
            shell_out_command=("claude", "mcp", "add"),
            shell_out_stderr="claude: command not found",
        )
        result = WizardResult(
            outcomes=self._full_clean_outcomes(),  # type: ignore[arg-type]
            aborted=False,
            host_install_result=host_result,
        )
        _render_wizard_result(result, host_display="Claude Code")
        captured = capsys.readouterr()
        # The "register manually" Note still appears (covered by
        # _render_wizard_result's existing shell_out_failed branch).
        assert "register manually" in captured.err
        # The clean-run thesis tagline does NOT — to avoid contradicting
        # the recovery hint.
        assert "The agent reads. It doesn't write." not in captured.err

    def test_wizard_status_to_tier_routes_known_statuses(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # PR #3 replaced the local `_STAGE_GLYPHS` dict with a
        # translation map onto `schemabrain._ui.status_glyph`. Pin
        # the translation contract so an unknown status (a future
        # addition that forgot to update the map) collapses to the
        # shared hard-break tier via the renderer's `.get(..., "err")`
        # fallback rather than crashing.
        from schemabrain.cli import _WIZARD_STATUS_TO_TIER

        # The wizard outcome vocabulary is the three known statuses;
        # the renderer maps them onto the shared tier names.
        assert _WIZARD_STATUS_TO_TIER == {
            "done": "ok",
            "skipped": "skipped",
            "failed": "err",
        }
        # Defensive fallback: an unknown status — a future addition
        # to `StageOutcome.status` that forgot to update the map —
        # routes through `_WIZARD_STATUS_TO_TIER.get(s, "err")` so
        # the renderer surfaces the routing gap visibly (✗ red) via
        # `status_glyph("err")` rather than silently rendering as
        # the raw status string.
        assert _WIZARD_STATUS_TO_TIER.get("unknown_future_status", "err") == "err"

    def test_progress_rule_shows_elapsed_on_clean_run(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Clean run (no aborts, no skips) → rule's right metadata is
        # the elapsed time only. Pins the simplest branch of
        # `_compose_progress_rule` so the format stays stable.
        from schemabrain.cli import _render_wizard_result
        from schemabrain.setup.wizard import StageOutcome, WizardResult

        result = WizardResult(
            outcomes=(
                StageOutcome(
                    stage=1, name="source_check", status="done", message="ok", duration_s=1.2
                ),
            ),
            aborted=False,
        )
        _render_wizard_result(result)
        captured = capsys.readouterr()
        assert "7 stages" in captured.err
        assert "1.2s" in captured.err
        # Clean-run rule must NOT carry advisory or stopped metadata.
        assert "advisory" not in captured.err
        assert "stopped at stage" not in captured.err

    def test_progress_rule_singular_advisory_when_one_stage_skipped(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # `advisory_count == 1` fork → "1 advisory" (singular, no count
        # prefix). Branch coverage for the singular/plural split in
        # `_compose_progress_rule`.
        from schemabrain.cli import _render_wizard_result
        from schemabrain.setup.wizard import StageOutcome, WizardResult

        result = WizardResult(
            outcomes=(
                StageOutcome(
                    stage=1, name="source_check", status="done", message="ok", duration_s=0.5
                ),
                StageOutcome(
                    stage=2, name="index", status="skipped", message="--skip-index", duration_s=0.0
                ),
            ),
            aborted=False,
        )
        _render_wizard_result(result)
        captured = capsys.readouterr()
        assert "1 advisory" in captured.err
        # Plural form must not leak into the singular case.
        assert "2 advisory" not in captured.err

    def test_progress_rule_plural_advisory_when_multiple_stages_skipped(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # `advisory_count > 1` fork → "{N} advisory". Pins the plural
        # branch so a future i18n / pluralisation refactor doesn't
        # silently drop the count prefix.
        from schemabrain.cli import _render_wizard_result
        from schemabrain.setup.wizard import StageOutcome, WizardResult

        result = WizardResult(
            outcomes=(
                StageOutcome(
                    stage=1, name="source_check", status="done", message="ok", duration_s=0.5
                ),
                StageOutcome(
                    stage=2, name="index", status="skipped", message="--skip-index", duration_s=0.0
                ),
                StageOutcome(
                    stage=3,
                    name="entities",
                    status="skipped",
                    message="--no-entities",
                    duration_s=0.0,
                ),
                StageOutcome(
                    stage=4,
                    name="metrics",
                    status="skipped",
                    message="--no-metrics",
                    duration_s=0.0,
                ),
            ),
            aborted=False,
        )
        _render_wizard_result(result)
        captured = capsys.readouterr()
        assert "3 advisory" in captured.err
        # Singular form must not leak into the plural case.
        assert "1 advisory" not in captured.err

    def test_progress_rule_shows_stopped_at_stage_on_abort(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Aborted run → rule's right metadata names the failing
        # stage ordinal. Pins the abort branch separately from
        # `test_aborted_run_renders_failure_panel`, which asserts on
        # the bordered abort panel, not on the progress rule.
        from schemabrain.cli import _render_wizard_result
        from schemabrain.setup.wizard import StageOutcome, WizardResult

        result = WizardResult(
            outcomes=(
                StageOutcome(
                    stage=1, name="source_check", status="done", message="ok", duration_s=0.4
                ),
                StageOutcome(
                    stage=2,
                    name="index",
                    status="failed",
                    message="source unreachable",
                    next_step="verify the URL and retry",
                    duration_s=0.8,
                ),
            ),
            aborted=True,
        )
        _render_wizard_result(result)
        captured = capsys.readouterr()
        assert "stopped at stage 2" in captured.err
        # Advisory metadata must not appear on an aborted run.
        assert "advisory" not in captured.err

    def test_stage_display_name_unknown_returns_input(self) -> None:
        from schemabrain.cli import _stage_display_name

        assert _stage_display_name("future_stage") == "future_stage"

    def test_stage_display_name_maps_known_names(self) -> None:
        from schemabrain.cli import _stage_display_name

        assert _stage_display_name("source_check") == "Source check"
        assert _stage_display_name("wire_host") == "Wire host"

    def test_wire_host_detail_returns_for_non_init_result(self) -> None:
        from schemabrain.cli import _render_wire_host_detail

        class _FakeConsole:
            pass

        # No exception — function is a no-op for non-InitResult.
        _render_wire_host_detail("not an init result", _FakeConsole())

    def test_wire_host_detail_renders_shell_out_failed_with_stderr_only(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Cover the shell_out_failed branch where stderr is set but
        # shell_out_command is None (defensive — the init flow always
        # populates command_run, but the renderer shouldn't crash if
        # it ever doesn't).
        from rich.console import Console

        from schemabrain.cli import _render_wire_host_detail
        from schemabrain.setup.hosts import SchemabrainSnippet
        from schemabrain.setup.init_flow import InitResult

        snippet = SchemabrainSnippet(command="uvx", args=("schemabrain==0.2.0a1", "serve"), env={})
        host_result = InitResult(
            host="claude-code",
            snippet=snippet,
            state="shell_out_failed",
            shell_out_command=None,
            shell_out_stderr="some error",
        )
        console = Console(stderr=True)
        _render_wire_host_detail(host_result, console)
        captured = capsys.readouterr()
        assert "some error" in captured.err

    def test_shell_out_stderr_credentials_redacted(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # If `claude mcp add` echoes the DB URL with credentials into
        # its stderr (a future CLI debug-trace regression), the
        # wizard's renderer must strip them before printing.
        from rich.console import Console

        from schemabrain.cli import _render_wire_host_detail
        from schemabrain.setup.hosts import SchemabrainSnippet
        from schemabrain.setup.init_flow import InitResult

        snippet = SchemabrainSnippet(command="uvx", args=("schemabrain==0.2.0a1", "serve"), env={})
        host_result = InitResult(
            host="claude-code",
            snippet=snippet,
            state="shell_out_failed",
            shell_out_command=None,
            shell_out_stderr=(
                "error: could not parse url "
                "postgresql+psycopg://alice:hunter2@prod-db.example.com/inventory"
            ),
        )
        console = Console(stderr=True)
        _render_wire_host_detail(host_result, console)
        captured = capsys.readouterr()

        assert "hunter2" not in captured.err
        assert "alice" not in captured.err
        assert "<redacted>" in captured.err
        # The host + db parts survive (only credentials masked).
        assert "prod-db.example.com" in captured.err

    def test_redact_stderr_credentials_handles_no_url(self) -> None:
        from schemabrain.cli import _redact_stderr_credentials

        # Input without any URL passes through unchanged.
        text = "claude: command not found"
        assert _redact_stderr_credentials(text) == text


class TestPendingEntityBlock:
    """Tests for the context-aware closing-block branch that surfaces
    stage 3's recovery action when entity curation didn't complete.

    Without this block, a wizard run that skipped entities (missing
    API key, --no-entities, failure) lands the user at "ask the agent
    to list entities" — and the agent honestly answers "no entities
    are configured." The block restores the trajectory by surfacing
    `entities suggest --apply` (and the env-var export when relevant)
    above the audit/tail hints.
    """

    def _make_outcome(
        self,
        stage: int,
        name: str,
        status: str,
        message: str,
        next_step: str | None = None,
    ) -> object:
        from schemabrain.setup.wizard import StageOutcome

        return StageOutcome(
            stage=stage,
            name=name,
            status=status,  # type: ignore[arg-type]
            message=message,
            next_step=next_step,
        )

    def _written_host_result(self, tmp_path: Path) -> object:
        from schemabrain.setup.hosts import SchemabrainSnippet
        from schemabrain.setup.init_flow import InitResult

        snippet = SchemabrainSnippet(command="uvx", args=("schemabrain==0.3.0", "serve"), env={})
        return InitResult(
            host="claude-desktop",
            snippet=snippet,
            state="written",
            config_path=tmp_path / "claude_desktop_config.json",
            backup_made=False,
        )

    def _result_with_entities_outcome(self, entities_outcome: object, tmp_path: Path) -> object:
        from schemabrain.setup.wizard import WizardResult

        # The metrics + joins outcomes here are `done` so the
        # closing block's metrics-pending and joins-pending branches
        # stay silent; tests in this class are isolating the entities
        # branch.
        outcomes = (
            self._make_outcome(1, "source_check", "done", "ok"),
            self._make_outcome(2, "index", "done", "indexed"),
            entities_outcome,
            self._make_outcome(4, "metrics", "done", "2 metrics created (cost $0.0050)"),
            self._make_outcome(
                5, "joins", "done", "3 canonical joins created from FK + query-log evidence"
            ),
            self._make_outcome(6, "wire_host", "done", "wired"),
            self._make_outcome(7, "next_step", "done", "Ready"),
        )
        return WizardResult(
            outcomes=outcomes,  # type: ignore[arg-type]
            aborted=False,
            host_install_result=self._written_host_result(tmp_path),  # type: ignore[arg-type]
        )

    def test_no_pending_block_when_entities_done(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Happy path: stage 3 applied N entities → closing block does
        # NOT surface any recovery copy; the user is told to ask the
        # agent directly.
        from schemabrain.cli import _render_wizard_result

        result = self._result_with_entities_outcome(
            self._make_outcome(3, "entities", "done", "3 entities created (cost $0.0123)"),
            tmp_path,
        )
        _render_wizard_result(result, host_display="Claude Desktop")
        captured = capsys.readouterr()
        # Closing-block invariants still hold.
        assert "Restart Claude Desktop" in captured.err
        assert "list the entities Schema Brain knows about" in captured.err
        # Pending-action copy absent.
        assert "To curate entities" not in captured.err
        assert "Curate entities when ready" not in captured.err
        assert "Stage 3 did not curate entities" not in captured.err

    def test_pending_block_for_api_key_missing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The most common dead-end the launch audit flagged:
        # ANTHROPIC_API_KEY isn't set → entities skipped → closing
        # block must surface `export ... && entities suggest --apply`.
        from schemabrain.cli import _render_wizard_result

        result = self._result_with_entities_outcome(
            self._make_outcome(
                3,
                "entities",
                "skipped",
                "ANTHROPIC_API_KEY not set; entity suggestion skipped",
                "export ANTHROPIC_API_KEY=sk-ant-... and then run "
                "`schemabrain entities suggest --apply`",
            ),
            tmp_path,
        )
        _render_wizard_result(result, host_display="Claude Desktop")
        captured = capsys.readouterr()
        # Headline framing names what's missing.
        assert "To curate entities" in captured.err
        # Both required actions surface.
        assert "ANTHROPIC_API_KEY=sk-ant-" in captured.err
        assert "schemabrain entities suggest --apply" in captured.err
        # The audit/tail hints + thesis tagline still close out.
        assert "schemabrain tail" in captured.err
        assert "The agent reads. It doesn't write." in captured.err

    def test_pending_block_for_no_entities_opt_out(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # User passed --no-entities. The closing block surfaces a
        # softer "when ready" pointer rather than a corrective message
        # — the user opted out deliberately.
        from schemabrain.cli import _render_wizard_result

        result = self._result_with_entities_outcome(
            self._make_outcome(
                3,
                "entities",
                "skipped",
                "--no-entities set; not running entity suggestion",
                "run `schemabrain entities suggest --apply` later to curate entities",
            ),
            tmp_path,
        )
        _render_wizard_result(result, host_display="Claude Desktop")
        captured = capsys.readouterr()
        assert "Curate entities when ready" in captured.err
        assert "schemabrain entities suggest --apply" in captured.err
        # The api-key-specific copy must NOT fire on this branch.
        assert "ANTHROPIC_API_KEY=sk-ant-" not in captured.err

    def test_pending_block_for_generic_skip(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Catch-all: non-Postgres source, --skip-index, etc. The
        # renderer falls back to the generic retry pointer.
        from schemabrain.cli import _render_wizard_result

        result = self._result_with_entities_outcome(
            self._make_outcome(
                3,
                "entities",
                "skipped",
                "entity suggestion needs a Postgres source",
            ),
            tmp_path,
        )
        _render_wizard_result(result, host_display="Claude Desktop")
        captured = capsys.readouterr()
        assert "Stage 3 did not curate entities" in captured.err
        assert "schemabrain entities suggest --apply" in captured.err

    def test_no_pending_block_when_already_curated(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Idempotent re-run: stage 3 short-circuits with "already
        # curated: N entity/ies present" because the store already has
        # entities. Status is `skipped` but the user is in the happy
        # path — the closing block must NOT show a recovery pointer
        # (entities exist; asking the agent to list them works).
        from schemabrain.cli import _render_wizard_result

        result = self._result_with_entities_outcome(
            self._make_outcome(
                3,
                "entities",
                "skipped",
                "already curated: 3 entity/ies present for this source",
                "run `schemabrain entities suggest --apply` directly "
                "to re-curate from a fresh prompt",
            ),
            tmp_path,
        )
        _render_wizard_result(result, host_display="Claude Desktop")
        captured = capsys.readouterr()
        # No pending-action copy fires.
        assert "To curate entities" not in captured.err
        assert "Curate entities when ready" not in captured.err
        assert "Stage 3 did not curate entities" not in captured.err
        # Standard closing-block invariants still hold.
        assert "list the entities Schema Brain knows about" in captured.err

    def test_pending_block_for_failed_stage(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Stage 3 has abort_on_fail=False — a `failed` status reaches
        # the closing block. Generic retry pointer fires.
        from schemabrain.cli import _render_wizard_result

        result = self._result_with_entities_outcome(
            self._make_outcome(
                3,
                "entities",
                "failed",
                "LLM returned 0 candidates (cost $0.0042)",
                "re-run `schemabrain entities suggest --dry-run` to inspect",
            ),
            tmp_path,
        )
        _render_wizard_result(result, host_display="Claude Desktop")
        captured = capsys.readouterr()
        assert "Stage 3 did not curate entities" in captured.err
        assert "schemabrain entities suggest --apply" in captured.err

    def test_pending_block_for_skip_index_branch(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # User passed --skip-index. Stage 2 skipped, so stage 3 also
        # skipped (no indexed schema to analyse). The renderer falls
        # through to the generic retry pointer — without this test, a
        # future refactor that adds a "skipped because --skip-index"
        # short-circuit guard in `_render_pending_entity_block` could
        # regress the user-visible copy silently.
        from schemabrain.cli import _render_wizard_result

        result = self._result_with_entities_outcome(
            self._make_outcome(
                3,
                "entities",
                "skipped",
                "skipped because --skip-index is set (no indexed schema to analyse)",
            ),
            tmp_path,
        )
        _render_wizard_result(result, host_display="Claude Desktop")
        captured = capsys.readouterr()
        assert "Stage 3 did not curate entities" in captured.err
        assert "schemabrain entities suggest --apply" in captured.err

    def test_pending_block_omitted_when_no_entities_outcome(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Defensive: an exotic stage list missing the entities stage
        # entirely. The closing block must not crash and must not
        # render the pending block.
        from schemabrain.cli import _render_wizard_result
        from schemabrain.setup.wizard import WizardResult

        outcomes = (
            self._make_outcome(1, "source_check", "done", "ok"),
            self._make_outcome(6, "wire_host", "done", "wired"),
        )
        result = WizardResult(
            outcomes=outcomes,  # type: ignore[arg-type]
            aborted=False,
            host_install_result=self._written_host_result(tmp_path),  # type: ignore[arg-type]
        )
        _render_wizard_result(result, host_display="Claude Desktop")
        captured = capsys.readouterr()
        assert "To curate entities" not in captured.err
        assert "Curate entities when ready" not in captured.err
        assert "Stage 3 did not curate entities" not in captured.err
        # Closing-block invariants still hold.
        assert "Restart Claude Desktop" in captured.err


class TestPendingMetricsBlock:
    """Tests for the closing-block branch that surfaces stage 4
    (metrics) recovery when curation didn't complete.

    Mirror of `TestPendingEntityBlock`. The metrics block fires only
    when stage 4 (`metrics`) did NOT land — skipped on opt-out,
    missing API key, empty entity store, or failure. Renders nothing
    when stage 4 succeeded or was idempotently short-circuited on
    already-curated.
    """

    def _make_outcome(
        self,
        stage: int,
        name: str,
        status: str,
        message: str,
        next_step: str | None = None,
    ) -> object:
        from schemabrain.setup.wizard import StageOutcome

        return StageOutcome(
            stage=stage,
            name=name,
            status=status,  # type: ignore[arg-type]
            message=message,
            next_step=next_step,
        )

    def _written_host_result(self, tmp_path: Path) -> object:
        from schemabrain.setup.hosts import SchemabrainSnippet
        from schemabrain.setup.init_flow import InitResult

        snippet = SchemabrainSnippet(command="uvx", args=("schemabrain==0.3.0", "serve"), env={})
        return InitResult(
            host="claude-desktop",
            snippet=snippet,
            state="written",
            config_path=tmp_path / "claude_desktop_config.json",
            backup_made=False,
        )

    def _result_with_metrics_outcome(self, metrics_outcome: object, tmp_path: Path) -> object:
        from schemabrain.setup.wizard import WizardResult

        # Entities + joins outcomes are `done` so the
        # entity-pending and joins-pending branches stay silent;
        # this class isolates the metrics branch.
        outcomes = (
            self._make_outcome(1, "source_check", "done", "ok"),
            self._make_outcome(2, "index", "done", "indexed"),
            self._make_outcome(3, "entities", "done", "4 entities created (cost $0.0123)"),
            metrics_outcome,
            self._make_outcome(
                5, "joins", "done", "3 canonical joins created from FK + query-log evidence"
            ),
            self._make_outcome(6, "wire_host", "done", "wired"),
            self._make_outcome(7, "next_step", "done", "Ready"),
        )
        return WizardResult(
            outcomes=outcomes,  # type: ignore[arg-type]
            aborted=False,
            host_install_result=self._written_host_result(tmp_path),  # type: ignore[arg-type]
        )

    def test_no_pending_block_when_metrics_done(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from schemabrain.cli import _render_wizard_result

        result = self._result_with_metrics_outcome(
            self._make_outcome(4, "metrics", "done", "6 metrics created (cost $0.0080)"),
            tmp_path,
        )
        _render_wizard_result(result, host_display="Claude Desktop")
        captured = capsys.readouterr()
        # Closing-block invariants still hold.
        assert "Restart Claude Desktop" in captured.err
        # No metrics-pending copy emitted.
        assert "To curate metrics" not in captured.err
        assert "Curate metrics when ready" not in captured.err
        assert "Metrics anchor on entities" not in captured.err
        assert "Stage 4 did not curate metrics" not in captured.err

    def test_no_pending_block_when_metrics_already_curated(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Idempotent re-run on a store that already has metrics —
        # treat as happy path, no pending copy.
        from schemabrain.cli import _render_wizard_result

        result = self._result_with_metrics_outcome(
            self._make_outcome(
                4, "metrics", "skipped", "already curated: 7 metric/s present for this source"
            ),
            tmp_path,
        )
        _render_wizard_result(result, host_display="Claude Desktop")
        captured = capsys.readouterr()
        assert "To curate metrics" not in captured.err
        assert "Curate metrics when ready" not in captured.err

    def test_pending_block_for_api_key_missing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from schemabrain.cli import _render_wizard_result

        result = self._result_with_metrics_outcome(
            self._make_outcome(
                4,
                "metrics",
                "skipped",
                "ANTHROPIC_API_KEY not set; metric suggestion skipped",
                "export ANTHROPIC_API_KEY=sk-ant-... and then run "
                "`schemabrain metrics suggest --apply`",
            ),
            tmp_path,
        )
        _render_wizard_result(result, host_display="Claude Desktop")
        captured = capsys.readouterr()
        # Headline framing names what's missing.
        assert "To curate metrics" in captured.err
        # Both required actions surface.
        assert "ANTHROPIC_API_KEY=sk-ant-" in captured.err
        assert "schemabrain metrics suggest --apply" in captured.err

    def test_pending_block_for_no_metrics_opt_out(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from schemabrain.cli import _render_wizard_result

        result = self._result_with_metrics_outcome(
            self._make_outcome(
                4,
                "metrics",
                "skipped",
                "--no-metrics set; not running metric suggestion",
                "run `schemabrain metrics suggest --apply` later to curate metrics",
            ),
            tmp_path,
        )
        _render_wizard_result(result, host_display="Claude Desktop")
        captured = capsys.readouterr()
        # Softer framing for the explicit opt-out.
        assert "Curate metrics when ready" in captured.err
        assert "schemabrain metrics suggest --apply" in captured.err

    def test_pending_block_for_empty_entity_store(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The cross-stage dependency: metrics need entities first.
        # The pending block must name the ordering so the user knows
        # what to do.
        from schemabrain.cli import _render_wizard_result

        result = self._result_with_metrics_outcome(
            self._make_outcome(
                4,
                "metrics",
                "skipped",
                "entity store is empty; metrics need entities to anchor on",
                "run `schemabrain entities suggest --apply` first, then re-run `schemabrain init`",
            ),
            tmp_path,
        )
        _render_wizard_result(result, host_display="Claude Desktop")
        captured = capsys.readouterr()
        assert "Metrics anchor on entities" in captured.err
        # Both commands surfaced in order.
        assert "schemabrain entities suggest --apply" in captured.err
        assert "schemabrain metrics suggest --apply" in captured.err

    def test_pending_block_for_generic_failure(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Any other skipped/failed status lands in the generic branch.
        from schemabrain.cli import _render_wizard_result

        result = self._result_with_metrics_outcome(
            self._make_outcome(
                4,
                "metrics",
                "failed",
                "LLM returned 4 candidates but none could be applied (cost $0.0050)",
                "re-run `schemabrain metrics suggest --dry-run` to inspect",
            ),
            tmp_path,
        )
        _render_wizard_result(result, host_display="Claude Desktop")
        captured = capsys.readouterr()
        assert "Stage 4 did not curate metrics" in captured.err
        assert "schemabrain metrics suggest --apply" in captured.err

    def test_pending_block_omitted_when_no_metrics_outcome(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Defensive: an exotic stage list missing the metrics stage
        # entirely. The renderer must not crash and must not emit
        # metrics-pending copy.
        from schemabrain.cli import _render_wizard_result
        from schemabrain.setup.wizard import WizardResult

        outcomes = (
            self._make_outcome(1, "source_check", "done", "ok"),
            self._make_outcome(6, "wire_host", "done", "wired"),
        )
        result = WizardResult(
            outcomes=outcomes,  # type: ignore[arg-type]
            aborted=False,
            host_install_result=self._written_host_result(tmp_path),  # type: ignore[arg-type]
        )
        _render_wizard_result(result, host_display="Claude Desktop")
        captured = capsys.readouterr()
        assert "To curate metrics" not in captured.err
        assert "Curate metrics when ready" not in captured.err
        assert "Metrics anchor on entities" not in captured.err
        assert "Stage 4 did not curate metrics" not in captured.err
        # Closing-block invariants still hold.
        assert "Restart Claude Desktop" in captured.err


class TestPendingJoinsBlock:
    """Tests for the closing-block branch that surfaces stage 5
    (joins) recovery when curation didn't complete.

    Mirror of `TestPendingMetricsBlock`, but with one fewer branch —
    joins is deterministic (no LLM, no API key), so there is no
    api-key recovery copy.
    """

    def _make_outcome(
        self,
        stage: int,
        name: str,
        status: str,
        message: str,
        next_step: str | None = None,
    ) -> object:
        from schemabrain.setup.wizard import StageOutcome

        return StageOutcome(
            stage=stage,
            name=name,
            status=status,  # type: ignore[arg-type]
            message=message,
            next_step=next_step,
        )

    def _written_host_result(self, tmp_path: Path) -> object:
        from schemabrain.setup.hosts import SchemabrainSnippet
        from schemabrain.setup.init_flow import InitResult

        snippet = SchemabrainSnippet(command="uvx", args=("schemabrain==0.3.0", "serve"), env={})
        return InitResult(
            host="claude-desktop",
            snippet=snippet,
            state="written",
            config_path=tmp_path / "claude_desktop_config.json",
            backup_made=False,
        )

    def _result_with_joins_outcome(self, joins_outcome: object, tmp_path: Path) -> object:
        from schemabrain.setup.wizard import WizardResult

        # Entities + metrics outcomes are `done` so the entity-pending
        # and metrics-pending branches stay silent; this class
        # isolates the joins branch.
        outcomes = (
            self._make_outcome(1, "source_check", "done", "ok"),
            self._make_outcome(2, "index", "done", "indexed"),
            self._make_outcome(3, "entities", "done", "4 entities created (cost $0.0123)"),
            self._make_outcome(4, "metrics", "done", "6 metrics created (cost $0.0080)"),
            joins_outcome,
            self._make_outcome(6, "wire_host", "done", "wired"),
            self._make_outcome(7, "next_step", "done", "Ready"),
        )
        return WizardResult(
            outcomes=outcomes,  # type: ignore[arg-type]
            aborted=False,
            host_install_result=self._written_host_result(tmp_path),  # type: ignore[arg-type]
        )

    def test_no_pending_block_when_joins_done(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from schemabrain.cli import _render_wizard_result

        result = self._result_with_joins_outcome(
            self._make_outcome(
                5, "joins", "done", "5 canonical joins created from FK + query-log evidence"
            ),
            tmp_path,
        )
        _render_wizard_result(result, host_display="Claude Desktop")
        captured = capsys.readouterr()
        # Closing-block invariants still hold.
        assert "Restart Claude Desktop" in captured.err
        # No joins-pending copy.
        assert "Curate joins when ready" not in captured.err
        assert "Joins anchor on entities" not in captured.err
        assert "Stage 5 did not curate joins" not in captured.err

    def test_no_pending_block_when_joins_already_curated(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from schemabrain.cli import _render_wizard_result

        result = self._result_with_joins_outcome(
            self._make_outcome(
                5,
                "joins",
                "skipped",
                "already curated: 4 canonical join/s present for this source",
            ),
            tmp_path,
        )
        _render_wizard_result(result, host_display="Claude Desktop")
        captured = capsys.readouterr()
        assert "Curate joins when ready" not in captured.err
        assert "Joins anchor on entities" not in captured.err

    def test_pending_block_for_no_joins_opt_out(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from schemabrain.cli import _render_wizard_result

        result = self._result_with_joins_outcome(
            self._make_outcome(
                5,
                "joins",
                "skipped",
                "--no-joins set; not running canonical-join suggestion",
                "run `schemabrain joins suggest --apply` later to curate joins",
            ),
            tmp_path,
        )
        _render_wizard_result(result, host_display="Claude Desktop")
        captured = capsys.readouterr()
        assert "Curate joins when ready" in captured.err
        assert "schemabrain joins suggest --apply" in captured.err

    def test_pending_block_for_empty_entity_store(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Same cross-stage dependency as metrics: joins need
        # entities to anchor on.
        from schemabrain.cli import _render_wizard_result

        result = self._result_with_joins_outcome(
            self._make_outcome(
                5,
                "joins",
                "skipped",
                "entity store is empty; joins need entities to anchor on",
                "run `schemabrain entities suggest --apply` first, then re-run `schemabrain init`",
            ),
            tmp_path,
        )
        _render_wizard_result(result, host_display="Claude Desktop")
        captured = capsys.readouterr()
        assert "Joins anchor on entities" in captured.err
        # Both commands surfaced in order.
        assert "schemabrain entities suggest --apply" in captured.err
        assert "schemabrain joins suggest --apply" in captured.err

    def test_pending_block_for_generic_failure(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Any other skipped/failed status (e.g. no evidence surfaced,
        # peek_join_count returned failed outcome) hits the generic
        # branch.
        from schemabrain.cli import _render_wizard_result

        result = self._result_with_joins_outcome(
            self._make_outcome(
                5,
                "joins",
                "skipped",
                "no canonical joins surfaced from FK or query-log evidence",
                "define joins by hand via `schemabrain joins apply`",
            ),
            tmp_path,
        )
        _render_wizard_result(result, host_display="Claude Desktop")
        captured = capsys.readouterr()
        assert "Stage 5 did not curate joins" in captured.err
        assert "schemabrain joins suggest --apply" in captured.err

    def test_pending_block_omitted_when_no_joins_outcome(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Defensive: exotic stage list with no joins outcome — block
        # must not render and must not crash.
        from schemabrain.cli import _render_wizard_result
        from schemabrain.setup.wizard import WizardResult

        outcomes = (
            self._make_outcome(1, "source_check", "done", "ok"),
            self._make_outcome(6, "wire_host", "done", "wired"),
        )
        result = WizardResult(
            outcomes=outcomes,  # type: ignore[arg-type]
            aborted=False,
            host_install_result=self._written_host_result(tmp_path),  # type: ignore[arg-type]
        )
        _render_wizard_result(result, host_display="Claude Desktop")
        captured = capsys.readouterr()
        assert "Curate joins when ready" not in captured.err
        assert "Joins anchor on entities" not in captured.err
        assert "Stage 5 did not curate joins" not in captured.err
        # Closing-block invariants still hold.
        assert "Restart Claude Desktop" in captured.err


class TestCmdInitSkipLlmConfirmDispatch:
    """Pre-LLM confirmation pause PR: the CLI dispatch layer threads
    `--skip-llm-confirm` and `--yes` into `WizardConfig.skip_llm_confirm`.
    Per the locked design, `--yes` is a superset shorthand that
    implies the LLM-prompt skip.

    Test strategy: monkeypatch `run_default_wizard` to capture the
    `WizardConfig` that `_cmd_init` constructs, then inspect the
    field. The fake returns an aborted WizardResult so `_cmd_init`
    returns exit code 2 without touching real wiring.
    """

    def _capture_run_default_wizard(self, monkeypatch: pytest.MonkeyPatch) -> list[object]:
        from schemabrain.setup.wizard import StageOutcome, WizardResult

        captured: list[object] = []

        def _fake_run(cfg: object, *, stage_context: object = None) -> WizardResult:
            captured.append(cfg)
            return WizardResult(
                outcomes=(
                    StageOutcome(
                        stage=1,
                        name="source_check",
                        status="failed",
                        message="forced stop for dispatch test",
                        next_step="n/a",
                    ),
                ),
                aborted=True,
                host_install_result=None,
            )

        monkeypatch.setattr("schemabrain.setup.wizard.run_default_wizard", _fake_run)
        return captured

    def _common_args(self, tmp_path: Path) -> list[str]:
        return [
            "init",
            "--source",
            "sqlite:///:memory:",
            "--host",
            "manual",
            "--store-path",
            str(tmp_path / "wizard.db"),
        ]

    def test_default_skip_llm_confirm_is_false(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured = self._capture_run_default_wizard(monkeypatch)
        main(self._common_args(tmp_path))
        assert len(captured) == 1
        cfg = captured[0]
        assert cfg.skip_llm_confirm is False  # type: ignore[attr-defined]
        assert cfg.assume_yes is False  # type: ignore[attr-defined]

    def test_skip_llm_confirm_flag_alone_does_not_set_assume_yes(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Narrow opt-out: `--skip-llm-confirm` sets only
        # `skip_llm_confirm`, leaves `assume_yes` False so the
        # host-overwrite prompt still fires for an existing entry.
        captured = self._capture_run_default_wizard(monkeypatch)
        main([*self._common_args(tmp_path), "--skip-llm-confirm"])
        cfg = captured[0]
        assert cfg.skip_llm_confirm is True  # type: ignore[attr-defined]
        assert cfg.assume_yes is False  # type: ignore[attr-defined]

    def test_yes_flag_implies_skip_llm_confirm(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Locked design: `--yes` is a superset shorthand that sets
        # BOTH fields. Users opt into the fully-non-interactive
        # wizard for CI / scripted runs.
        captured = self._capture_run_default_wizard(monkeypatch)
        main([*self._common_args(tmp_path), "--yes"])
        cfg = captured[0]
        assert cfg.assume_yes is True  # type: ignore[attr-defined]
        assert cfg.skip_llm_confirm is True  # type: ignore[attr-defined]

    def test_y_short_flag_implies_skip_llm_confirm(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # `-y` is the short form; same superset semantics.
        captured = self._capture_run_default_wizard(monkeypatch)
        main([*self._common_args(tmp_path), "-y"])
        cfg = captured[0]
        assert cfg.assume_yes is True  # type: ignore[attr-defined]
        assert cfg.skip_llm_confirm is True  # type: ignore[attr-defined]

    def test_both_flags_together_idempotent(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Passing both flags is redundant but legal.
        captured = self._capture_run_default_wizard(monkeypatch)
        main([*self._common_args(tmp_path), "--yes", "--skip-llm-confirm"])
        cfg = captured[0]
        assert cfg.assume_yes is True  # type: ignore[attr-defined]
        assert cfg.skip_llm_confirm is True  # type: ignore[attr-defined]


class TestPositiveFloatValidator:
    def test_accepts_positive_value(self) -> None:
        from schemabrain.cli import _positive_float

        assert _positive_float("0.5") == 0.5
        assert _positive_float("1.0") == 1.0

    def test_rejects_zero(self) -> None:
        import argparse

        from schemabrain.cli import _positive_float

        with pytest.raises(argparse.ArgumentTypeError, match="must be a positive"):
            _positive_float("0")

    def test_rejects_negative(self) -> None:
        import argparse

        from schemabrain.cli import _positive_float

        with pytest.raises(argparse.ArgumentTypeError, match="must be a positive"):
            _positive_float("-0.5")

    def test_rejects_non_numeric(self) -> None:
        import argparse

        from schemabrain.cli import _positive_float

        with pytest.raises(argparse.ArgumentTypeError, match="must be a number"):
            _positive_float("infinity-ish")

    def test_cli_rejects_zero_entities_max_cost_at_argparse(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # argparse exits 2 with a usage error for type-converter
        # failures. This catches the security H-1 crash path:
        # zero/negative cost would otherwise reach `CostCeilingGuard`
        # and surface as an uncaught traceback.
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "init",
                    "--source",
                    "sqlite:///:memory:",
                    "--entities-max-cost-usd",
                    "0",
                ]
            )
        assert exc_info.value.code == 2
        captured = capsys.readouterr()
        assert "must be a positive" in captured.err


class TestInvalidHostRefusal:
    def test_invalid_host_via_print_only_alias_kept_valid(
        self, seeded_store: Path, stub_uvx: None
    ) -> None:
        # `--print-only` forces host to "manual" — that always passes
        # the literal check. This test locks in the print-only path.
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
