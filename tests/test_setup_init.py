"""Tests for `schemabrain.setup.init_flow` — the activation gate orchestrator.

Goals:

  1. `init` validates preconditions in order (uvx, source, store) and
     refuses with a structured `InitRefusal` (carrying a `GuidedError`)
     on the first failure — the user solves problems one at a time.
  2. For `--host manual`, init NEVER writes a file. It returns an
     `InitResult` carrying the snippet so the CLI can print it.
  3. For `--host claude-desktop`, init writes via `config_io` and
     reports whether a backup was made. Re-running with identical
     inputs is a no-op (state == "unchanged"); re-running with
     different inputs requires `assume_yes=True` to overwrite without
     prompting.
  4. For `--host claude-code`, init shells out to `install_to_claude_code`
     and reports the shell-out outcome on the result so the CLI can
     fall back to printing the command if the install failed.
  5. The empty-store check refuses without `--skip-index`; with
     `--skip-index`, it warns-but-continues so users who indexed in
     a different session can re-init.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore
from schemabrain.setup.hosts import SchemabrainSnippet
from schemabrain.setup.init_flow import (
    InitRefusal,
    InitResult,
    init,
)

# ----- fixtures -------------------------------------------------------------


@pytest.fixture
def seeded_store(tmp_path: Path) -> Iterator[Path]:
    """A store with one indexed table + one entity so init's
    entity-count check passes without --skip-index."""
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
def fresh_store(tmp_path: Path) -> Iterator[Path]:
    """A store that exists with the right schema version but holds no entities."""
    path = tmp_path / "store.db"
    store = SQLiteStore(path=path)
    try:
        yield path
    finally:
        store.close()


@pytest.fixture
def stub_uvx(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pretend uvx is on PATH so init's tooling validation passes."""
    monkeypatch.setattr(
        "shutil.which",
        lambda n: "/usr/local/bin/uvx" if n == "uvx" else None,
    )
    yield


# ----- happy path: manual host ----------------------------------------------


class TestInitToManual:
    def test_returns_printed_only_state(self, seeded_store: Path, stub_uvx: None) -> None:
        result = init(
            source_url="sqlite:///:memory:",
            store_path=seeded_store,
            host="manual",
            env_var_name="DB_URL",
            skip_index=False,
            assume_yes=False,
        )
        assert isinstance(result, InitResult)
        assert result.state == "printed_only"
        assert result.host == "manual"
        assert result.config_path is None
        assert result.backup_made is False

    def test_includes_snippet_in_result(self, seeded_store: Path, stub_uvx: None) -> None:
        result = init(
            source_url="sqlite:///:memory:",
            store_path=seeded_store,
            host="manual",
            env_var_name="DB_URL",
            skip_index=False,
            assume_yes=False,
        )
        assert isinstance(result.snippet, SchemabrainSnippet)
        # Decision 3 invariant: credentials live in env, not args.
        assert "sqlite:///:memory:" not in result.snippet.args
        assert result.snippet.env == {"DB_URL": "sqlite:///:memory:"}


# ----- happy path: claude-desktop -------------------------------------------


class TestInitToClaudeDesktop:
    def test_writes_config_file_when_missing(
        self,
        seeded_store: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        stub_uvx: None,
    ) -> None:
        # The parent dir is what Claude Desktop creates on install;
        # init refuses if it's missing. The test pre-creates it.
        claude_dir = tmp_path / "Claude"
        claude_dir.mkdir()
        cfg = claude_dir / "claude_desktop_config.json"
        monkeypatch.setattr(
            "schemabrain.setup.init_flow.claude_desktop_config_path",
            lambda: cfg,
        )
        result = init(
            source_url="sqlite:///:memory:",
            store_path=seeded_store,
            host="claude-desktop",
            env_var_name="DB_URL",
            skip_index=False,
            assume_yes=False,
        )
        assert result.state == "written"
        assert result.config_path == cfg
        assert result.backup_made is False
        # The snippet is now in the JSON.
        config = json.loads(cfg.read_text())
        assert "schemabrain" in config["mcpServers"]

    def test_creates_backup_when_overwriting_existing(
        self,
        seeded_store: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        stub_uvx: None,
    ) -> None:
        cfg = tmp_path / "Claude" / "claude_desktop_config.json"
        cfg.parent.mkdir()
        cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
        monkeypatch.setattr(
            "schemabrain.setup.init_flow.claude_desktop_config_path",
            lambda: cfg,
        )
        result = init(
            source_url="sqlite:///:memory:",
            store_path=seeded_store,
            host="claude-desktop",
            env_var_name="DB_URL",
            skip_index=False,
            assume_yes=False,
        )
        assert result.state == "written"
        assert result.backup_made is True
        # The other entry survived byte-stable.
        merged = json.loads(cfg.read_text())
        assert merged["mcpServers"]["other"] == {"command": "x"}


# ----- idempotency ----------------------------------------------------------


class TestInitIdempotency:
    def test_second_run_with_identical_args_is_no_op(
        self,
        seeded_store: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        stub_uvx: None,
    ) -> None:
        claude_dir = tmp_path / "Claude"
        claude_dir.mkdir()
        cfg = claude_dir / "claude_desktop_config.json"
        monkeypatch.setattr(
            "schemabrain.setup.init_flow.claude_desktop_config_path",
            lambda: cfg,
        )
        first = init(
            source_url="sqlite:///:memory:",
            store_path=seeded_store,
            host="claude-desktop",
            env_var_name="DB_URL",
            skip_index=False,
            assume_yes=False,
        )
        assert first.state == "written"
        # Snapshot for comparison.
        first_content = cfg.read_text()
        second = init(
            source_url="sqlite:///:memory:",
            store_path=seeded_store,
            host="claude-desktop",
            env_var_name="DB_URL",
            skip_index=False,
            assume_yes=False,
        )
        assert second.state == "unchanged"
        assert second.backup_made is False
        # File untouched.
        assert cfg.read_text() == first_content
        # No backup at all — first run wrote a fresh file, second run
        # was a no-op. The backup-once contract only kicks in when the
        # file existed before the write.
        backup_path = cfg.parent / (cfg.name + ".bak")
        assert not backup_path.exists()

    def test_refuses_overwrite_of_different_entry_without_yes(
        self,
        seeded_store: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        stub_uvx: None,
    ) -> None:
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
        # Non-interactive without --yes → refuse (don't surprise the user).
        with pytest.raises(InitRefusal) as exc_info:
            init(
                source_url="sqlite:///:memory:",
                store_path=seeded_store,
                host="claude-desktop",
                env_var_name="DB_URL",
                skip_index=False,
                assume_yes=False,
            )
        assert "init_entry_exists" in exc_info.value.error.kind

    def test_assume_yes_overwrites_different_entry(
        self,
        seeded_store: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        stub_uvx: None,
    ) -> None:
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
        result = init(
            source_url="sqlite:///:memory:",
            store_path=seeded_store,
            host="claude-desktop",
            env_var_name="DB_URL",
            skip_index=False,
            assume_yes=True,
        )
        assert result.state == "written"
        assert result.backup_made is True
        # The old version pin is gone; the new one matches the snippet.
        new_args = json.loads(cfg.read_text())["mcpServers"]["schemabrain"]["args"]
        assert not any("0.0.99" in a for a in new_args)


# ----- claude-code shell-out ------------------------------------------------


class TestInitToClaudeCode:
    def test_shell_out_success_reports_succeeded(
        self,
        seeded_store: Path,
        monkeypatch: pytest.MonkeyPatch,
        stub_uvx: None,
    ) -> None:
        from schemabrain.setup.hosts import ClaudeCodeInstallResult

        def fake_install(snippet: SchemabrainSnippet) -> ClaudeCodeInstallResult:
            return ClaudeCodeInstallResult(
                succeeded=True,
                command_run=("claude", "mcp", "add", "schemabrain"),
            )

        monkeypatch.setattr("schemabrain.setup.init_flow.install_to_claude_code", fake_install)
        result = init(
            source_url="sqlite:///:memory:",
            store_path=seeded_store,
            host="claude-code",
            env_var_name="DB_URL",
            skip_index=False,
            assume_yes=False,
        )
        assert result.state == "shell_out_succeeded"
        assert result.shell_out_command is not None
        assert result.shell_out_stderr == ""

    def test_shell_out_failure_reports_failed_with_command(
        self,
        seeded_store: Path,
        monkeypatch: pytest.MonkeyPatch,
        stub_uvx: None,
    ) -> None:
        from schemabrain.setup.hosts import ClaudeCodeInstallResult

        def fake_install(snippet: SchemabrainSnippet) -> ClaudeCodeInstallResult:
            return ClaudeCodeInstallResult(
                succeeded=False,
                command_run=("claude", "mcp", "add", "schemabrain"),
                stderr="server already registered",
            )

        monkeypatch.setattr("schemabrain.setup.init_flow.install_to_claude_code", fake_install)
        result = init(
            source_url="sqlite:///:memory:",
            store_path=seeded_store,
            host="claude-code",
            env_var_name="DB_URL",
            skip_index=False,
            assume_yes=False,
        )
        # state reflects the shell-out result; the CLI uses this to
        # decide whether to print the fallback copy-paste command.
        assert result.state == "shell_out_failed"
        assert result.shell_out_stderr is not None
        assert "registered" in result.shell_out_stderr


# ----- validation: uvx ------------------------------------------------------


class TestInitValidatesUvx:
    def test_refuses_when_uvx_missing(
        self,
        seeded_store: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Pretend NOTHING is on PATH — neither uvx nor a fallback
        # schemabrain entrypoint.
        monkeypatch.setattr("shutil.which", lambda _n: None)
        with pytest.raises(InitRefusal) as exc_info:
            init(
                source_url="sqlite:///:memory:",
                store_path=seeded_store,
                host="manual",
                env_var_name="DB_URL",
                skip_index=False,
                assume_yes=False,
            )
        assert (
            "uvx" in exc_info.value.error.kind or "init_runner_missing" in exc_info.value.error.kind
        )

    def test_uses_installed_entrypoint_when_uvx_missing_but_schemabrain_present(
        self,
        seeded_store: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # uvx not on PATH, but /usr/local/bin/schemabrain is — init
        # falls back to the absolute-path runner.
        def fake_which(name: str) -> str | None:
            if name == "uvx":
                return None
            if name == "schemabrain":
                return "/usr/local/bin/schemabrain"
            return None

        monkeypatch.setattr("shutil.which", fake_which)
        result = init(
            source_url="sqlite:///:memory:",
            store_path=seeded_store,
            host="manual",
            env_var_name="DB_URL",
            skip_index=False,
            assume_yes=False,
        )
        # Snippet uses absolute-path runner (no version pin).
        assert result.snippet.command == "/usr/local/bin/schemabrain"
        assert not any("schemabrain==" in a for a in result.snippet.args)


# ----- validation: source ---------------------------------------------------


class TestInitValidatesPostgresReadOnly:
    """Drives the Postgres-only read-only validation path via mocked engine."""

    @pytest.fixture
    def _stub_engine_with_value(self, monkeypatch: pytest.MonkeyPatch) -> object:
        from schemabrain.setup import init_flow

        def make(value: str) -> None:
            class _Conn:
                def execute(self, _query: object) -> object:
                    class _Res:
                        def scalar(self) -> str:
                            return value

                    return _Res()

                def __enter__(self) -> _Conn:
                    return self

                def __exit__(self, *_a: object) -> None:
                    pass

            class _Engine:
                def connect(self) -> _Conn:
                    return _Conn()

                def dispose(self) -> None:
                    pass

            monkeypatch.setattr(init_flow, "create_engine", lambda *_a, **_kw: _Engine())

        return make

    def test_passes_when_session_reports_read_only_on(
        self,
        seeded_store: Path,
        stub_uvx: None,
        _stub_engine_with_value: object,
    ) -> None:
        _stub_engine_with_value("on")  # type: ignore[operator]
        result = init(
            source_url="postgresql+psycopg://u:p@h/db",
            store_path=seeded_store,
            host="manual",
            env_var_name="DB_URL",
            skip_index=False,
            assume_yes=False,
        )
        assert result.state == "printed_only"

    def test_refuses_when_session_reports_off(
        self,
        seeded_store: Path,
        stub_uvx: None,
        _stub_engine_with_value: object,
    ) -> None:
        _stub_engine_with_value("off")  # type: ignore[operator]
        with pytest.raises(InitRefusal) as exc_info:
            init(
                source_url="postgresql+psycopg://u:p@h/db",
                store_path=seeded_store,
                host="manual",
                env_var_name="DB_URL",
                skip_index=False,
                assume_yes=False,
            )
        assert "init_source_not_read_only" in exc_info.value.error.kind

    def test_refuses_when_read_only_engine_raises(
        self,
        seeded_store: Path,
        stub_uvx: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from schemabrain.setup import init_flow

        # Reachability stub: SELECT 1 succeeds. Then the SECOND engine
        # creation (for the read-only check) raises.
        call_count = {"n": 0}

        def fake_create_engine(*_a: object, **_kw: object) -> object:
            call_count["n"] += 1
            if call_count["n"] == 1:
                # First call: source reachability check.
                class _Conn:
                    def execute(self, _query: object) -> object:
                        return None

                    def __enter__(self) -> _Conn:
                        return self

                    def __exit__(self, *_a: object) -> None:
                        pass

                class _Engine:
                    def connect(self) -> _Conn:
                        return _Conn()

                    def dispose(self) -> None:
                        pass

                return _Engine()
            raise RuntimeError("could not connect for read-only check")

        monkeypatch.setattr(init_flow, "create_engine", fake_create_engine)
        with pytest.raises(InitRefusal) as exc_info:
            init(
                source_url="postgresql+psycopg://u:p@h/db",
                store_path=seeded_store,
                host="manual",
                env_var_name="DB_URL",
                skip_index=False,
                assume_yes=False,
            )
        assert "init_source_read_only_check_failed" in exc_info.value.error.kind


class TestInitValidatesSource:
    def test_refuses_when_source_unreachable(self, seeded_store: Path, stub_uvx: None) -> None:
        with pytest.raises(InitRefusal) as exc_info:
            init(
                source_url="postgresql+psycopg://nope:nope@127.0.0.1:1/db",
                store_path=seeded_store,
                host="manual",
                env_var_name="DB_URL",
                skip_index=False,
                assume_yes=False,
            )
        assert "init_source_unreachable" in exc_info.value.error.kind


# ----- validation: store ----------------------------------------------------


class TestInitValidatesStore:
    def test_refuses_when_store_schema_mismatched(
        self,
        tmp_path: Path,
        stub_uvx: None,
    ) -> None:
        import sqlite3

        path = tmp_path / "store.db"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE schemabrain_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO schemabrain_meta VALUES ('schema_version', '999')")
        conn.commit()
        conn.close()
        with pytest.raises(InitRefusal) as exc_info:
            init(
                source_url="sqlite:///:memory:",
                store_path=path,
                host="manual",
                env_var_name="DB_URL",
                skip_index=False,
                assume_yes=False,
            )
        assert "init_store_schema_mismatch" in exc_info.value.error.kind

    def test_refuses_when_empty_store_without_skip_index(
        self, fresh_store: Path, stub_uvx: None
    ) -> None:
        with pytest.raises(InitRefusal) as exc_info:
            init(
                source_url="sqlite:///:memory:",
                store_path=fresh_store,
                host="manual",
                env_var_name="DB_URL",
                skip_index=False,
                assume_yes=False,
            )
        assert "init_store_empty" in exc_info.value.error.kind

    def test_skip_index_allows_empty_store(self, fresh_store: Path, stub_uvx: None) -> None:
        result = init(
            source_url="sqlite:///:memory:",
            store_path=fresh_store,
            host="manual",
            env_var_name="DB_URL",
            skip_index=True,
            assume_yes=False,
        )
        assert result.state == "printed_only"

    def test_refuses_when_store_file_missing_without_skip_index(
        self, tmp_path: Path, stub_uvx: None
    ) -> None:
        # Parent exists, file doesn't; without --skip-index this fails.
        with pytest.raises(InitRefusal) as exc_info:
            init(
                source_url="sqlite:///:memory:",
                store_path=tmp_path / "store.db",
                host="manual",
                env_var_name="DB_URL",
                skip_index=False,
                assume_yes=False,
            )
        assert "init_store_empty" in exc_info.value.error.kind

    def test_skip_index_allows_missing_store_file(self, tmp_path: Path, stub_uvx: None) -> None:
        # Parent exists, store file doesn't, but --skip-index
        # acknowledges this is intentional (will be indexed later).
        result = init(
            source_url="sqlite:///:memory:",
            store_path=tmp_path / "store.db",
            host="manual",
            env_var_name="DB_URL",
            skip_index=True,
            assume_yes=False,
        )
        assert result.state == "printed_only"

    def test_refuses_when_store_path_parent_missing(self, tmp_path: Path, stub_uvx: None) -> None:
        # Parent dir doesn't exist; the store-path-writable validation
        # catches this before any other work.
        with pytest.raises(InitRefusal) as exc_info:
            init(
                source_url="sqlite:///:memory:",
                store_path=tmp_path / "nope" / "store.db",
                host="manual",
                env_var_name="DB_URL",
                skip_index=True,
                assume_yes=False,
            )
        assert "init_store_path" in exc_info.value.error.kind


# ----- validation: host config dir -----------------------------------------


class TestInitValidatesHostConfigDir:
    def test_refuses_when_claude_desktop_config_dir_missing(
        self,
        seeded_store: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        stub_uvx: None,
    ) -> None:
        # The parent dir of claude_desktop_config.json is what we
        # check — when it doesn't exist, Claude Desktop isn't installed.
        ghost = tmp_path / "no-such-dir" / "claude_desktop_config.json"
        monkeypatch.setattr(
            "schemabrain.setup.init_flow.claude_desktop_config_path",
            lambda: ghost,
        )
        with pytest.raises(InitRefusal) as exc_info:
            init(
                source_url="sqlite:///:memory:",
                store_path=seeded_store,
                host="claude-desktop",
                env_var_name="DB_URL",
                skip_index=False,
                assume_yes=False,
            )
        assert "init_host_unavailable" in exc_info.value.error.kind

    def test_refuses_when_claude_desktop_config_path_returns_none(
        self,
        seeded_store: Path,
        monkeypatch: pytest.MonkeyPatch,
        stub_uvx: None,
    ) -> None:
        # E.g. Linux — no Claude Desktop build.
        monkeypatch.setattr(
            "schemabrain.setup.init_flow.claude_desktop_config_path",
            lambda: None,
        )
        with pytest.raises(InitRefusal):
            init(
                source_url="sqlite:///:memory:",
                store_path=seeded_store,
                host="claude-desktop",
                env_var_name="DB_URL",
                skip_index=False,
                assume_yes=False,
            )
