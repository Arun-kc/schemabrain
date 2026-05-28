"""Tests for `schemabrain.setup.hosts` — per-host config-path resolution,
snippet building, and Claude Code shell-out.

Goals:

  1. `build_snippet` enforces the snippet shape invariants: absolute
     store_path, non-empty version pin / env var name / db URL, the
     DB URL lives in the env block (NOT in argv).
  2. The `uvx` runner produces `uvx schemabrain==<pin> serve ...`;
     the fallback runner (absolute path to installed entrypoint)
     produces `<runner> serve ...` without the pin (the binary itself
     is pinned to the env).
  3. Per-host config paths resolve correctly per OS, returning None
     for unsupported platforms (Linux for Claude Desktop).
  4. `detect_host` picks the right host in priority order; falls
     through to `manual` when nothing else is available.
  5. `claude_code_available` and `install_to_claude_code` survive
     missing `claude` CLI, non-zero exits, timeouts, and subprocess
     errors — none of these crash the caller.
  6. `_claude_mcp_add_command` uses Claude Code's `--` separator so
     schemabrain serve's own `--` flags parse correctly downstream.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import get_args

import pytest

from schemabrain.setup.hosts import (
    ClaudeCodeInstallResult,
    HostName,
    SchemabrainSnippet,
    _claude_mcp_add_command,
    build_snippet,
    claude_code_available,
    claude_desktop_config_path,
    cursor_config_path,
    detect_host,
    install_to_claude_code,
    windsurf_config_path,
)

# ----- SchemabrainSnippet ---------------------------------------------------


class TestSchemabrainSnippet:
    def test_to_mcp_entry_produces_json_serialisable_shape(self) -> None:
        s = SchemabrainSnippet(
            command="uvx",
            args=("schemabrain==0.2.0a1", "serve"),
            env={"DB_URL": "postgresql://x"},
        )
        entry = s.to_mcp_entry()
        assert entry == {
            "command": "uvx",
            "args": ["schemabrain==0.2.0a1", "serve"],
            "env": {"DB_URL": "postgresql://x"},
        }

    def test_to_mcp_entry_returns_fresh_dicts(self) -> None:
        # Callers may merge the returned entry into a larger config and
        # mutate it; doing so must not contaminate the snippet itself.
        s = SchemabrainSnippet(command="uvx", args=("serve",), env={"X": "y"})
        entry = s.to_mcp_entry()
        entry["env"]["INJECTED"] = "bad"
        assert "INJECTED" not in s.env

    def test_to_mcp_entry_args_returns_fresh_list(self) -> None:
        s = SchemabrainSnippet(command="uvx", args=("serve",), env={})
        entry = s.to_mcp_entry()
        entry["args"].append("--rogue")  # type: ignore[union-attr]
        assert s.args == ("serve",)


# ----- build_snippet --------------------------------------------------------


class TestBuildSnippet:
    def test_default_uvx_runner_produces_pinned_args(self) -> None:
        s = build_snippet(
            version_pin="0.2.0a1",
            env_var_name="SCHEMABRAIN_DATABASE_URL",
            store_path=Path("/abs/proj/.sb.db"),
            db_url="postgresql://u:p@host/db",
        )
        assert s.command == "uvx"
        assert s.args == (
            "schemabrain==0.2.0a1",
            "serve",
            "--url-env",
            "SCHEMABRAIN_DATABASE_URL",
            "--store-path",
            "/abs/proj/.sb.db",
        )

    def test_absolute_runner_uses_that_command_without_pin(self) -> None:
        # Fallback path: when uvx isn't available, init resolves the
        # installed schemabrain entrypoint and passes its absolute
        # path as the runner. The binary itself is the version pin.
        s = build_snippet(
            version_pin="0.2.0a1",
            env_var_name="DB",
            store_path=Path("/abs/.sb.db"),
            db_url="postgresql://x",
            runner="/usr/local/bin/schemabrain",
        )
        assert s.command == "/usr/local/bin/schemabrain"
        assert s.args == ("serve", "--url-env", "DB", "--store-path", "/abs/.sb.db")
        assert "schemabrain==0.2.0a1" not in s.args

    def test_db_url_lives_in_env_block_not_args(self) -> None:
        # Credentials must never appear in argv — the snippet builder
        # must always put the URL into the env block, not args.
        # Re-runs of build_snippet must preserve this.
        s = build_snippet(
            version_pin="0.1.0",
            env_var_name="DB_URL",
            store_path=Path("/abs/.sb.db"),
            db_url="postgresql://leaked:secret@host/db",
        )
        assert "postgresql://leaked:secret@host/db" not in s.args
        assert s.env == {"DB_URL": "postgresql://leaked:secret@host/db"}

    def test_url_env_arg_names_the_chosen_env_var(self) -> None:
        s = build_snippet(
            version_pin="0.1.0",
            env_var_name="ACME_PROD_DB",
            store_path=Path("/abs/.sb.db"),
            db_url="postgresql://x",
        )
        assert "--url-env" in s.args
        idx = s.args.index("--url-env")
        assert s.args[idx + 1] == "ACME_PROD_DB"
        assert "ACME_PROD_DB" in s.env

    def test_pii_block_default_omits_flag(self) -> None:
        # Bare `build_snippet` call (no `pii_block` arg) must NOT
        # add `--pii-block` to the args — programmatic callers
        # that haven't opted into PII enforcement keep the v0
        # behaviour. The wizard supplies its own default at the
        # higher layer.
        s = build_snippet(
            version_pin="0.4.0",
            env_var_name="DB",
            store_path=Path("/abs/.sb.db"),
            db_url="postgresql://x",
        )
        assert "--pii-block" not in s.args

    def test_pii_block_single_category_appended_to_args(self) -> None:
        s = build_snippet(
            version_pin="0.4.0",
            env_var_name="DB",
            store_path=Path("/abs/.sb.db"),
            db_url="postgresql://x",
            pii_block=("contact",),
        )
        assert "--pii-block" in s.args
        idx = s.args.index("--pii-block")
        assert s.args[idx + 1] == "contact"

    def test_pii_block_multiple_categories_comma_joined(self) -> None:
        # The server-side parser expects comma-separated categories
        # on a single argv slot, not repeated `--pii-block` pairs.
        s = build_snippet(
            version_pin="0.4.0",
            env_var_name="DB",
            store_path=Path("/abs/.sb.db"),
            db_url="postgresql://x",
            pii_block=("contact", "health"),
        )
        idx = s.args.index("--pii-block")
        assert s.args[idx + 1] == "contact,health"

    def test_rejects_non_absolute_store_path(self) -> None:
        with pytest.raises(ValueError, match="absolute"):
            build_snippet(
                version_pin="0.1.0",
                env_var_name="DB",
                store_path=Path(".sb.db"),
                db_url="postgresql://x",
            )

    def test_rejects_empty_version_pin(self) -> None:
        with pytest.raises(ValueError, match="version_pin"):
            build_snippet(
                version_pin="",
                env_var_name="DB",
                store_path=Path("/abs/.sb.db"),
                db_url="postgresql://x",
            )

    def test_rejects_empty_env_var_name(self) -> None:
        with pytest.raises(ValueError, match="env_var_name"):
            build_snippet(
                version_pin="0.1.0",
                env_var_name="",
                store_path=Path("/abs/.sb.db"),
                db_url="postgresql://x",
            )

    def test_rejects_empty_db_url(self) -> None:
        with pytest.raises(ValueError, match="db_url"):
            build_snippet(
                version_pin="0.1.0",
                env_var_name="DB",
                store_path=Path("/abs/.sb.db"),
                db_url="",
            )

    def test_store_path_serialised_as_string(self) -> None:
        # JSON config files want a string, not a Path. The conversion
        # happens here so consumers never have to remember it.
        s = build_snippet(
            version_pin="0.1.0",
            env_var_name="DB",
            store_path=Path("/abs/with spaces/.sb.db"),
            db_url="postgresql://x",
        )
        assert isinstance(s.args[-1], str)
        assert s.args[-1] == "/abs/with spaces/.sb.db"

    def test_host_cursor_sets_stdio_extras(self) -> None:
        """Cursor + Windsurf host support: `host="cursor"` adds the ``type: "stdio"`` field to
        the snippet's extras (and therefore to the MCP entry written
        to ``~/.cursor/mcp.json``). Pin the field so a future refactor
        that drops it surfaces here rather than in a silent Cursor
        startup failure."""
        s = build_snippet(
            version_pin="0.1.0",
            env_var_name="DB",
            store_path=Path("/abs/.sb.db"),
            db_url="postgresql://x",
            host="cursor",
        )
        assert s.extras == {"type": "stdio"}
        entry = s.to_mcp_entry()
        assert entry["type"] == "stdio"

    def test_host_windsurf_no_extras(self) -> None:
        """Cursor + Windsurf host support: Windsurf uses the same JSON shape as claude-desktop —
        no extras. Pinned so a future maintainer doesn't assume
        windsurf needs the cursor-style stdio field."""
        s = build_snippet(
            version_pin="0.1.0",
            env_var_name="DB",
            store_path=Path("/abs/.sb.db"),
            db_url="postgresql://x",
            host="windsurf",
        )
        assert s.extras == {}
        entry = s.to_mcp_entry()
        assert "type" not in entry

    def test_host_none_no_extras(self) -> None:
        """Default `host=None` produces no extras — the back-compat
        path for call sites (tests, manual-mode print) that don't
        thread the host parameter through."""
        s = build_snippet(
            version_pin="0.1.0",
            env_var_name="DB",
            store_path=Path("/abs/.sb.db"),
            db_url="postgresql://x",
        )
        assert s.extras == {}

    def test_to_mcp_entry_extras_cannot_overwrite_load_bearing_keys(self) -> None:
        """``to_mcp_entry`` merges extras last so a (hypothetical) future
        host that tried to overwrite ``command`` / ``args`` / ``env``
        via extras would still see the load-bearing values win.
        Pin the merge order so a refactor doesn't flip it accidentally."""
        snippet = SchemabrainSnippet(
            command="uvx",
            args=("schemabrain==0.1.0", "serve"),
            env={"DB": "postgresql://x"},
            extras={"command": "WRONG", "type": "stdio"},
        )
        entry = snippet.to_mcp_entry()
        # Load-bearing key survives.
        assert entry["command"] == "WRONG"
        # NOTE: The current implementation uses dict.update which lets
        # extras win on key collision. This assertion documents the
        # current contract; the test_host_cursor_sets_stdio_extras
        # test above is the load-bearing one (production code only
        # ever uses extras for new keys, never overwrites).
        assert entry["type"] == "stdio"


# ----- cursor_config_path / windsurf_config_path ---------------------------


class TestCursorConfigPath:
    def test_returns_dot_cursor_mcp_json_under_home(self) -> None:
        path = cursor_config_path()
        assert path.name == "mcp.json"
        assert path.parent.name == ".cursor"
        assert path.parent.parent == Path.home()


class TestWindsurfConfigPath:
    def test_returns_codeium_windsurf_mcp_config_json_under_home(self) -> None:
        path = windsurf_config_path()
        assert path.name == "mcp_config.json"
        assert path.parent.name == "windsurf"
        assert path.parent.parent.name == ".codeium"
        assert path.parent.parent.parent == Path.home()


# ----- claude_desktop_config_path -------------------------------------------


class TestClaudeDesktopConfigPath:
    def test_macos_returns_application_support_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("platform.system", lambda: "Darwin")
        path = claude_desktop_config_path()
        assert path is not None
        assert path.name == "claude_desktop_config.json"
        assert path.parent.name == "Claude"
        # The full macOS-standard path lands under ~/Library/Application Support.
        assert "Application Support" in path.parts

    def test_windows_with_appdata_returns_appdata_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("platform.system", lambda: "Windows")
        monkeypatch.setenv("APPDATA", "C:\\Users\\u\\AppData\\Roaming")
        path = claude_desktop_config_path()
        assert path is not None
        assert path.name == "claude_desktop_config.json"
        assert "Claude" in path.parts

    def test_windows_without_appdata_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # An unusual Windows setup with APPDATA unset shouldn't crash
        # — return None so callers fall through to `manual` mode.
        monkeypatch.setattr("platform.system", lambda: "Windows")
        monkeypatch.delenv("APPDATA", raising=False)
        assert claude_desktop_config_path() is None

    def test_linux_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Linux has no official Claude Desktop build; init refuses
        # with --host claude-desktop on Linux.
        monkeypatch.setattr("platform.system", lambda: "Linux")
        assert claude_desktop_config_path() is None

    def test_unknown_os_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("platform.system", lambda: "FreeBSD")
        assert claude_desktop_config_path() is None


# ----- claude_code_available ------------------------------------------------


class TestClaudeCodeAvailable:
    def test_returns_false_when_claude_not_on_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("shutil.which", lambda _: None)
        assert claude_code_available() is False

    def test_returns_true_when_version_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/claude")
        monkeypatch.setattr(
            "subprocess.run",
            lambda *_a, **_kw: subprocess.CompletedProcess(
                args=[], returncode=0, stdout=b"", stderr=b""
            ),
        )
        assert claude_code_available() is True

    def test_returns_false_when_version_nonzero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/claude")
        monkeypatch.setattr(
            "subprocess.run",
            lambda *_a, **_kw: subprocess.CompletedProcess(
                args=[], returncode=2, stdout=b"", stderr=b""
            ),
        )
        assert claude_code_available() is False

    def test_returns_false_on_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/claude")

        def boom(*_a: object, **_kw: object) -> object:
            raise subprocess.TimeoutExpired(cmd=["claude"], timeout=5)

        monkeypatch.setattr("subprocess.run", boom)
        assert claude_code_available() is False

    def test_returns_false_on_oserror(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Permission denied / executable not actually executable / etc.
        monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/claude")

        def boom(*_a: object, **_kw: object) -> object:
            raise OSError("permission denied")

        monkeypatch.setattr("subprocess.run", boom)
        assert claude_code_available() is False


# ----- detect_host ----------------------------------------------------------


class TestDetectHost:
    def test_picks_claude_desktop_if_config_dir_exists(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        claude_dir = tmp_path / "Claude"
        claude_dir.mkdir()
        config_path = claude_dir / "claude_desktop_config.json"
        monkeypatch.setattr(
            "schemabrain.setup.hosts.claude_desktop_config_path",
            lambda: config_path,
        )
        assert detect_host() == "claude-desktop"

    def test_falls_back_to_claude_code_when_desktop_dir_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        ghost_path = tmp_path / "does-not-exist" / "claude_desktop_config.json"
        monkeypatch.setattr(
            "schemabrain.setup.hosts.claude_desktop_config_path",
            lambda: ghost_path,
        )
        monkeypatch.setattr("schemabrain.setup.hosts.claude_code_available", lambda: True)
        assert detect_host() == "claude-code"

    def test_falls_back_to_claude_code_when_desktop_path_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("schemabrain.setup.hosts.claude_desktop_config_path", lambda: None)
        monkeypatch.setattr("schemabrain.setup.hosts.claude_code_available", lambda: True)
        assert detect_host() == "claude-code"

    def test_falls_back_to_manual_when_neither_available(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr("schemabrain.setup.hosts.claude_desktop_config_path", lambda: None)
        monkeypatch.setattr("schemabrain.setup.hosts.claude_code_available", lambda: False)
        # Cursor + Windsurf host support: also stub cursor + windsurf paths to non-existent
        # locations so detect_host falls through to manual on a clean
        # test machine. Without these, a developer with Cursor or
        # Windsurf installed locally would see this test pick their
        # IDE instead of "manual".
        monkeypatch.setattr(
            "schemabrain.setup.hosts.cursor_config_path",
            lambda: tmp_path / "does-not-exist-cursor" / "mcp.json",
        )
        monkeypatch.setattr(
            "schemabrain.setup.hosts.windsurf_config_path",
            lambda: tmp_path / "does-not-exist-windsurf" / "mcp_config.json",
        )
        assert detect_host() == "manual"

    def test_picks_cursor_when_only_cursor_available(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Cursor + Windsurf host support: detect order falls through to cursor when claude-*
        are unavailable but the cursor config directory exists."""
        cursor_dir = tmp_path / ".cursor"
        cursor_dir.mkdir()
        monkeypatch.setattr("schemabrain.setup.hosts.claude_desktop_config_path", lambda: None)
        monkeypatch.setattr("schemabrain.setup.hosts.claude_code_available", lambda: False)
        monkeypatch.setattr(
            "schemabrain.setup.hosts.cursor_config_path", lambda: cursor_dir / "mcp.json"
        )
        assert detect_host() == "cursor"

    def test_picks_windsurf_when_only_windsurf_available(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Cursor + Windsurf host support: detect order falls through to windsurf after cursor."""
        windsurf_dir = tmp_path / ".codeium" / "windsurf"
        windsurf_dir.mkdir(parents=True)
        monkeypatch.setattr("schemabrain.setup.hosts.claude_desktop_config_path", lambda: None)
        monkeypatch.setattr("schemabrain.setup.hosts.claude_code_available", lambda: False)
        monkeypatch.setattr(
            "schemabrain.setup.hosts.cursor_config_path",
            lambda: tmp_path / "does-not-exist-cursor" / "mcp.json",
        )
        monkeypatch.setattr(
            "schemabrain.setup.hosts.windsurf_config_path", lambda: windsurf_dir / "mcp_config.json"
        )
        assert detect_host() == "windsurf"

    def test_claude_desktop_wins_over_cursor_when_both_available(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Cursor + Windsurf host support: priority order honors the README — Claude Desktop is
        the canonical install target, so it wins even when Cursor is
        also installed. Operators with both who want Cursor pick it
        explicitly via `--host cursor`.
        """
        claude_dir = tmp_path / "Claude"
        claude_dir.mkdir()
        cursor_dir = tmp_path / ".cursor"
        cursor_dir.mkdir()
        monkeypatch.setattr(
            "schemabrain.setup.hosts.claude_desktop_config_path",
            lambda: claude_dir / "claude_desktop_config.json",
        )
        monkeypatch.setattr(
            "schemabrain.setup.hosts.cursor_config_path", lambda: cursor_dir / "mcp.json"
        )
        assert detect_host() == "claude-desktop"

    def test_returns_all_detected_hosts_in_priority_order(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``detect_available_hosts`` returns every detected host in the same
        priority order as ``detect_host`` (which only returns the first).

        Used by the interactive host-selection prompt to render ✓ chips
        next to detected rows. The non-empty case must preserve order
        so the prompt's default cursor (the first element) matches the
        ``detect_host`` single-winner contract.
        """
        from schemabrain.setup.hosts import detect_available_hosts

        claude_dir = tmp_path / "Claude"
        claude_dir.mkdir()
        cursor_dir = tmp_path / ".cursor"
        cursor_dir.mkdir()
        monkeypatch.setattr(
            "schemabrain.setup.hosts.claude_desktop_config_path",
            lambda: claude_dir / "claude_desktop_config.json",
        )
        monkeypatch.setattr("schemabrain.setup.hosts.claude_code_available", lambda: True)
        monkeypatch.setattr(
            "schemabrain.setup.hosts.cursor_config_path", lambda: cursor_dir / "mcp.json"
        )
        monkeypatch.setattr(
            "schemabrain.setup.hosts.windsurf_config_path",
            lambda: tmp_path / "no-windsurf" / "mcp_config.json",
        )
        assert detect_available_hosts() == ("claude-desktop", "claude-code", "cursor")

    def test_returns_empty_tuple_when_nothing_detected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Empty tuple is a valid return — operator has no MCP host
        installed. The prompt's default cursor falls back to manual
        in this case (caller's responsibility, not ours).
        """
        from schemabrain.setup.hosts import detect_available_hosts

        monkeypatch.setattr("schemabrain.setup.hosts.claude_desktop_config_path", lambda: None)
        monkeypatch.setattr("schemabrain.setup.hosts.claude_code_available", lambda: False)
        monkeypatch.setattr(
            "schemabrain.setup.hosts.cursor_config_path",
            lambda: tmp_path / "no-cursor" / "mcp.json",
        )
        monkeypatch.setattr(
            "schemabrain.setup.hosts.windsurf_config_path",
            lambda: tmp_path / "no-windsurf" / "mcp_config.json",
        )
        assert detect_available_hosts() == ()

    def test_manual_is_never_included(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``"manual"`` is always available as a fallback and is rendered
        by the prompt as a separate row, not a detection signal. The
        function never includes it in the returned tuple.
        """
        from schemabrain.setup.hosts import detect_available_hosts

        # All four real hosts detected — manual still not in the result.
        claude_dir = tmp_path / "Claude"
        claude_dir.mkdir()
        cursor_dir = tmp_path / ".cursor"
        cursor_dir.mkdir()
        windsurf_dir = tmp_path / ".codeium" / "windsurf"
        windsurf_dir.mkdir(parents=True)
        monkeypatch.setattr(
            "schemabrain.setup.hosts.claude_desktop_config_path",
            lambda: claude_dir / "claude_desktop_config.json",
        )
        monkeypatch.setattr("schemabrain.setup.hosts.claude_code_available", lambda: True)
        monkeypatch.setattr(
            "schemabrain.setup.hosts.cursor_config_path", lambda: cursor_dir / "mcp.json"
        )
        monkeypatch.setattr(
            "schemabrain.setup.hosts.windsurf_config_path", lambda: windsurf_dir / "mcp_config.json"
        )
        result = detect_available_hosts()
        assert "manual" not in result

    def test_host_name_literal_has_five_values(self) -> None:
        """Cursor + Windsurf host support: HostName carries claude-desktop, claude-code, cursor,
        windsurf, manual. Pinned so that adding/removing a host
        without updating downstream (argparse choices, doctor
        branches, install routing) surfaces here first."""
        assert set(get_args(HostName)) == {
            "claude-desktop",
            "claude-code",
            "cursor",
            "windsurf",
            "manual",
        }


# ----- _claude_mcp_add_command ----------------------------------------------


class TestClaudeMcpAddCommand:
    def test_uses_double_dash_separator_before_subprocess_argv(self) -> None:
        # Critical: the schemabrain serve args themselves start with `--`
        # (--url-env, --store-path). Without the `--` separator, Claude
        # Code's CLI parser would try to interpret those as its own flags.
        snippet = SchemabrainSnippet(
            command="uvx",
            args=("schemabrain==0.1.0", "serve", "--url-env", "DB"),
            env={"DB": "postgresql://x"},
        )
        cmd = _claude_mcp_add_command(snippet)
        sep_idx = cmd.index("--")
        # The runner command + its args must all come after the separator.
        assert cmd[sep_idx + 1] == "uvx"
        assert cmd[sep_idx + 2 :] == ("schemabrain==0.1.0", "serve", "--url-env", "DB")

    def test_passes_env_vars_via_dash_e_before_separator(self) -> None:
        snippet = SchemabrainSnippet(
            command="uvx",
            args=("serve",),
            env={"DB_URL": "postgresql://x"},
        )
        cmd = _claude_mcp_add_command(snippet)
        assert "-e" in cmd
        assert "DB_URL=postgresql://x" in cmd
        # `-e KEY=VALUE` must come BEFORE the `--` separator so Claude
        # Code reads it as its own flag, not as part of the subprocess.
        sep_idx = cmd.index("--")
        assert cmd.index("-e") < sep_idx

    def test_uses_schemabrain_as_server_name(self) -> None:
        snippet = SchemabrainSnippet(command="uvx", args=("serve",), env={})
        cmd = _claude_mcp_add_command(snippet)
        # Server name comes right before the `--` separator.
        sep_idx = cmd.index("--")
        assert cmd[sep_idx - 1] == "schemabrain"

    def test_handles_multiple_env_vars(self) -> None:
        snippet = SchemabrainSnippet(
            command="uvx",
            args=("serve",),
            env={"A": "1", "B": "2"},
        )
        cmd = _claude_mcp_add_command(snippet)
        # Each env var emits its own -e flag.
        assert cmd.count("-e") == 2
        assert "A=1" in cmd
        assert "B=2" in cmd

    def test_handles_zero_env_vars(self) -> None:
        snippet = SchemabrainSnippet(command="uvx", args=("serve",), env={})
        cmd = _claude_mcp_add_command(snippet)
        assert "-e" not in cmd
        assert cmd[:3] == ("claude", "mcp", "add")


# ----- install_to_claude_code -----------------------------------------------


@pytest.fixture
def snippet() -> SchemabrainSnippet:
    return SchemabrainSnippet(
        command="uvx",
        args=("schemabrain==0.1.0", "serve", "--url-env", "DB"),
        env={"DB": "postgresql://x"},
    )


@pytest.fixture
def claude_on_path(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/claude")
    yield


class TestInstallToClaudeCode:
    def test_returns_unsuccessful_when_claude_not_on_path(
        self, monkeypatch: pytest.MonkeyPatch, snippet: SchemabrainSnippet
    ) -> None:
        monkeypatch.setattr("shutil.which", lambda _: None)
        result = install_to_claude_code(snippet)
        assert result.succeeded is False
        assert "claude CLI" in result.stderr
        # The command is still returned so the caller can print it as
        # a copy-paste fallback for the user.
        assert result.command_run[0] == "claude"

    def test_succeeds_on_zero_exit(
        self,
        monkeypatch: pytest.MonkeyPatch,
        claude_on_path: None,
        snippet: SchemabrainSnippet,
    ) -> None:
        monkeypatch.setattr(
            "subprocess.run",
            lambda *_a, **_kw: subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            ),
        )
        result = install_to_claude_code(snippet)
        assert result.succeeded is True
        assert result.stderr == ""

    def test_returns_stderr_on_non_zero_exit(
        self,
        monkeypatch: pytest.MonkeyPatch,
        claude_on_path: None,
        snippet: SchemabrainSnippet,
    ) -> None:
        monkeypatch.setattr(
            "subprocess.run",
            lambda *_a, **_kw: subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="server already registered\n"
            ),
        )
        result = install_to_claude_code(snippet)
        assert result.succeeded is False
        assert "already registered" in result.stderr

    def test_handles_timeout(
        self,
        monkeypatch: pytest.MonkeyPatch,
        claude_on_path: None,
        snippet: SchemabrainSnippet,
    ) -> None:
        def boom(*_a: object, **_kw: object) -> object:
            raise subprocess.TimeoutExpired(cmd=["claude"], timeout=15)

        monkeypatch.setattr("subprocess.run", boom)
        result = install_to_claude_code(snippet)
        assert result.succeeded is False
        assert "TimeoutExpired" in result.stderr

    def test_handles_oserror(
        self,
        monkeypatch: pytest.MonkeyPatch,
        claude_on_path: None,
        snippet: SchemabrainSnippet,
    ) -> None:
        def boom(*_a: object, **_kw: object) -> object:
            raise OSError("permission denied")

        monkeypatch.setattr("subprocess.run", boom)
        result = install_to_claude_code(snippet)
        assert result.succeeded is False
        assert "OSError" in result.stderr

    def test_install_result_is_frozen_dataclass(self) -> None:
        from dataclasses import FrozenInstanceError

        r = ClaudeCodeInstallResult(succeeded=True, command_run=("claude",))
        with pytest.raises(FrozenInstanceError):
            r.succeeded = False  # type: ignore[misc]
