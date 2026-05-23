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
    ClaudeDesktopEntryComparison,
    InitRefusal,
    InitResult,
    compare_existing_claude_desktop_entry,
    init,
    peek_claude_desktop_overwrite,
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
    """Pretend uvx is on PATH AND the install looks like PyPI.

    `_resolve_runner` requires both signals to pick `"uvx"` as the
    runner. Tests under this fixture want the uvx-pin happy path, so
    we stub both — the actual install in the test environment is an
    editable checkout (direct_url.json present), which would otherwise
    fall back to the absolute-path runner.
    """
    monkeypatch.setattr(
        "shutil.which",
        lambda n: "/usr/local/bin/uvx" if n == "uvx" else None,
    )
    monkeypatch.setattr(
        "schemabrain.setup.init_flow._is_pypi_install",
        lambda: True,
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
        # Credentials live in env, not args — keeps passwords out of
        # any process-listing surface (ps, journald, container logs).
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


class TestInitInstallToClaudeDesktopMalformedConfig:
    """Round-2 fold HIGH (silent-failure-hunter): the TOCTOU mirror
    of the MalformedConfigError wrap in `compare_existing_claude_desktop_entry`.

    Covers the case where the config was malformed between the
    wizard's pre-check pass AND the `init()` write — OR the F3-bypass
    path where `assume_yes=True` skipped the pre-check entirely.
    Without the wrap, this branch crashed with a raw Python
    traceback.
    """

    def test_init_wraps_malformed_config_as_init_refusal(
        self,
        seeded_store: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        stub_uvx: None,
    ) -> None:
        claude_dir = tmp_path / "Claude"
        claude_dir.mkdir()
        cfg = claude_dir / "claude_desktop_config.json"
        cfg.write_text("{not valid json", encoding="utf-8")
        monkeypatch.setattr(
            "schemabrain.setup.init_flow.claude_desktop_config_path",
            lambda: cfg,
        )

        with pytest.raises(InitRefusal) as exc_info:
            init(
                source_url="sqlite:///:memory:",
                store_path=seeded_store,
                host="claude-desktop",
                env_var_name="DB_URL",
                skip_index=False,
                assume_yes=True,  # bypasses the pre-check; exercises the wrap at the write boundary
            )
        assert exc_info.value.error.kind == "init_host_config_malformed"
        assert "not valid JSON" in exc_info.value.error.message


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


# ----- compare_existing_claude_desktop_entry --------------------------------


def _build_snippet_for_test(
    *,
    store_path: str = "/tmp/sb.db",
    db_url_env: str = "DATABASE_URL",
    version: str = "0.99.0",
    runner: str = "uvx",
) -> SchemabrainSnippet:
    """Tiny helper — F3 comparison tests construct snippets directly."""
    serve_args: tuple[str, ...] = (
        "serve",
        "--url-env",
        db_url_env,
        "--store-path",
        store_path,
    )
    if runner == "uvx":
        return SchemabrainSnippet(
            command="uvx",
            args=(f"schemabrain=={version}", *serve_args),
            env={db_url_env: "postgresql://u:p@h/d"},
        )
    return SchemabrainSnippet(
        command=runner,
        args=serve_args,
        env={db_url_env: "postgresql://u:p@h/d"},
    )


class TestCompareExistingClaudeDesktopEntry:
    """F3: comparison verdict + diff field naming for the wizard's pre-check."""

    def test_new_when_config_file_missing(self, tmp_path: Path) -> None:
        cfg = tmp_path / "claude_desktop_config.json"
        # File does NOT exist.
        result = compare_existing_claude_desktop_entry(
            snippet=_build_snippet_for_test(),
            config_path=cfg,
        )
        assert result.state == "new"
        assert result.config_path == cfg
        # The snippet's store path threads through so the renderer
        # can mention it even on a fresh-write path.
        assert result.new_store_path == "/tmp/sb.db"
        assert result.existing_store_path is None
        # D3: no diff preview for fresh-write paths — nothing
        # meaningful to diff against.
        assert result.diff_preview == ""

    def test_new_when_config_has_no_schemabrain_entry(self, tmp_path: Path) -> None:
        cfg = tmp_path / "claude_desktop_config.json"
        cfg.write_text(json.dumps({"mcpServers": {"other": {}}}))
        result = compare_existing_claude_desktop_entry(
            snippet=_build_snippet_for_test(),
            config_path=cfg,
        )
        assert result.state == "new"

    def test_unchanged_when_entries_match_byte_for_byte(self, tmp_path: Path) -> None:
        snippet = _build_snippet_for_test(store_path="/tmp/sb.db")
        cfg = tmp_path / "claude_desktop_config.json"
        cfg.write_text(json.dumps({"mcpServers": {"schemabrain": snippet.to_mcp_entry()}}))
        result = compare_existing_claude_desktop_entry(snippet=snippet, config_path=cfg)
        assert result.state == "unchanged"
        assert result.differing_field_names == ()
        # Both store-path values present so the renderer can choose.
        assert result.existing_store_path == "/tmp/sb.db"
        assert result.new_store_path == "/tmp/sb.db"
        # D3: no diff preview when there's nothing to overwrite.
        assert result.diff_preview == ""

    def test_differs_store_path_only_when_only_store_path_changes(self, tmp_path: Path) -> None:
        # The common case: operator moved their store. Verdict must
        # be the auto-acceptable one — anything else would surface a
        # noisy prompt for what's effectively a path update.
        existing_snippet = _build_snippet_for_test(store_path="/old/sb.db")
        new_snippet = _build_snippet_for_test(store_path="/new/sb.db")
        cfg = tmp_path / "claude_desktop_config.json"
        cfg.write_text(json.dumps({"mcpServers": {"schemabrain": existing_snippet.to_mcp_entry()}}))
        result = compare_existing_claude_desktop_entry(snippet=new_snippet, config_path=cfg)
        assert result.state == "differs_store_path_only"
        assert result.differing_field_names == ("args[--store-path]",)
        assert result.existing_store_path == "/old/sb.db"
        assert result.new_store_path == "/new/sb.db"
        # D3: diff preview shows the store-path swap as a one-line
        # change with the config-file path in both unified-diff
        # headers so the operator can see the file context.
        assert result.diff_preview != ""
        assert '"/old/sb.db"' in result.diff_preview
        assert '"/new/sb.db"' in result.diff_preview
        assert "(current)" in result.diff_preview
        assert "(after init)" in result.diff_preview

    def test_differs_when_env_var_name_changes(self, tmp_path: Path) -> None:
        # env-var name change is a meaningful difference — the user
        # may have moved their DB credentials to a different env var,
        # or worse, mis-typed it. Prompt the operator.
        existing_snippet = _build_snippet_for_test(db_url_env="OLD_DB_URL")
        new_snippet = _build_snippet_for_test(db_url_env="DATABASE_URL")
        cfg = tmp_path / "claude_desktop_config.json"
        cfg.write_text(json.dumps({"mcpServers": {"schemabrain": existing_snippet.to_mcp_entry()}}))
        result = compare_existing_claude_desktop_entry(snippet=new_snippet, config_path=cfg)
        assert result.state == "differs"
        # Both args (DB URL env appears in --url-env arg) and env
        # (the env block keyed on the DB-URL env-var name) differ.
        assert "args" in result.differing_field_names
        assert "env" in result.differing_field_names
        # D3: diff preview surfaces the env-var rename so the operator
        # sees both the OLD and NEW names side-by-side before approving.
        assert result.diff_preview != ""
        assert "OLD_DB_URL" in result.diff_preview
        assert "DATABASE_URL" in result.diff_preview

    def test_differs_when_version_pin_changes(self, tmp_path: Path) -> None:
        # Version pin upgrade (e.g. 0.3.0 → 0.4.0) is meaningful —
        # the operator should know the host is being re-pointed at
        # a different schemabrain release.
        existing_snippet = _build_snippet_for_test(version="0.3.0")
        new_snippet = _build_snippet_for_test(version="0.4.0")
        cfg = tmp_path / "claude_desktop_config.json"
        cfg.write_text(json.dumps({"mcpServers": {"schemabrain": existing_snippet.to_mcp_entry()}}))
        result = compare_existing_claude_desktop_entry(snippet=new_snippet, config_path=cfg)
        assert result.state == "differs"
        assert "args" in result.differing_field_names

    def test_differs_when_only_env_block_changes(self, tmp_path: Path) -> None:
        # Edge case: args identical, env differs. Exercised when an
        # operator hand-edits the env block (e.g. adds extra
        # environment variables beyond the DB URL). The diff
        # reports `env` only, not `args`.
        snippet = _build_snippet_for_test(store_path="/same/sb.db")
        existing_entry = snippet.to_mcp_entry()
        # Mutate the env block in the existing entry to differ from
        # the new snippet's env. Args stay identical.
        existing_entry["env"] = {"DATABASE_URL": "postgresql://different@h/d"}
        cfg = tmp_path / "claude_desktop_config.json"
        cfg.write_text(json.dumps({"mcpServers": {"schemabrain": existing_entry}}))

        result = compare_existing_claude_desktop_entry(snippet=snippet, config_path=cfg)
        assert result.state == "differs"
        assert result.differing_field_names == ("env",)

    def test_malformed_config_wraps_as_init_refusal(self, tmp_path: Path) -> None:
        # Round-2 fold HIGH (silent-failure-hunter): pre-fold, the
        # docstring CLAIMED this function "raises InitRefusal if the
        # config file is present but malformed" — but the code
        # raised MalformedConfigError directly. The wizard's
        # `except InitRefusal` guard at stage 6 missed it, crashing
        # init on any malformed claude_desktop_config.json with a
        # raw Python traceback. Fix wraps MalformedConfigError at
        # this boundary so the guard catches it and renders a
        # guided error.
        cfg = tmp_path / "claude_desktop_config.json"
        cfg.write_text("{this is: not valid json", encoding="utf-8")

        with pytest.raises(InitRefusal) as exc_info:
            compare_existing_claude_desktop_entry(
                snippet=_build_snippet_for_test(),
                config_path=cfg,
            )
        assert exc_info.value.error.kind == "init_host_config_malformed"
        assert "not valid JSON" in exc_info.value.error.message
        assert "line" in exc_info.value.error.why

    def test_differs_when_command_changes(self, tmp_path: Path) -> None:
        # uvx → installed schemabrain entrypoint (or vice versa) is
        # a runner swap; surface the prompt.
        existing_snippet = _build_snippet_for_test(runner="uvx")
        new_snippet = _build_snippet_for_test(runner="/opt/sb/bin/schemabrain")
        cfg = tmp_path / "claude_desktop_config.json"
        cfg.write_text(json.dumps({"mcpServers": {"schemabrain": existing_snippet.to_mcp_entry()}}))
        result = compare_existing_claude_desktop_entry(snippet=new_snippet, config_path=cfg)
        assert result.state == "differs"
        assert "command" in result.differing_field_names


class TestPeekClaudeDesktopOverwrite:
    """F3: end-to-end pre-check helper that the wizard's stage 6 calls."""

    def test_returns_none_when_no_claude_desktop_config_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # On a platform without a Claude Desktop config directory,
        # `claude_desktop_config_path` returns None — peek must
        # gracefully early-out so init()'s downstream InitRefusal
        # owns the messaging.
        monkeypatch.setattr(
            "shutil.which",
            lambda n: "/usr/local/bin/uvx" if n == "uvx" else None,
        )
        monkeypatch.setattr(
            "schemabrain.setup.init_flow.claude_desktop_config_path",
            lambda: None,
        )
        result = peek_claude_desktop_overwrite(
            source_url="postgresql://u:p@h/d",
            store_path=tmp_path / "sb.db",
            env_var_name="DATABASE_URL",
        )
        assert result is None

    def test_returns_differs_when_existing_entry_conflicts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # End-to-end happy path: existing config has a different
        # entry; peek returns "differs" so the wizard can prompt.
        monkeypatch.setattr(
            "shutil.which",
            lambda n: "/usr/local/bin/uvx" if n == "uvx" else None,
        )
        monkeypatch.setattr(
            "schemabrain.setup.init_flow._is_pypi_install",
            lambda: True,
        )
        cfg = tmp_path / "claude_desktop_config.json"
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
        result = peek_claude_desktop_overwrite(
            source_url="postgresql://u:p@h/d",
            store_path=tmp_path / "sb.db",
            env_var_name="DATABASE_URL",
        )
        assert result is not None
        assert result.state == "differs"
        assert result.config_path == cfg


class TestStageWireHostInlineOverwritePrompt:
    """F3: the wizard's stage 6 owns the overwrite prompt (not the CLI loop).

    Tests in `tests/test_cli_init.py::TestInitCliInteractiveOverlay`
    cover the end-to-end CLI behavior; tests here pin the helper's
    auto-accept rule for the store-path-only case, which the
    end-to-end test would need a more elaborate setup to exercise.
    """

    def test_store_path_only_diff_renders_replaced_in_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Auto-accept fires when only --store-path differs; the
        # `wire_host` stage's "wrote ..." message must surface
        # `(replaced /old → /new)` so the operator sees what changed
        # without needing to read the JSON diff.
        from schemabrain.setup.wizard import _wire_host_message

        old_path = "/old/sb.db"
        new_path = "/new/sb.db"
        comparison = ClaudeDesktopEntryComparison(
            state="differs_store_path_only",
            config_path=tmp_path / "claude_desktop_config.json",
            differing_field_names=("args[--store-path]",),
            existing_store_path=old_path,
            new_store_path=new_path,
        )
        result = InitResult(
            host="claude-desktop",
            snippet=_build_snippet_for_test(store_path=new_path),
            state="written",
            config_path=tmp_path / "claude_desktop_config.json",
            backup_made=False,
        )
        msg = _wire_host_message(result, comparison=comparison)
        assert "(replaced /old/sb.db → /new/sb.db)" in msg

    def test_unchanged_state_does_not_render_replaced(self, tmp_path: Path) -> None:
        from schemabrain.setup.wizard import _wire_host_message

        comparison = ClaudeDesktopEntryComparison(
            state="unchanged",
            config_path=tmp_path / "claude_desktop_config.json",
            existing_store_path="/same/sb.db",
            new_store_path="/same/sb.db",
        )
        result = InitResult(
            host="claude-desktop",
            snippet=_build_snippet_for_test(store_path="/same/sb.db"),
            state="written",
            config_path=tmp_path / "claude_desktop_config.json",
            backup_made=False,
        )
        msg = _wire_host_message(result, comparison=comparison)
        # No "replaced" trailer when state is not differs_store_path_only.
        assert "replaced" not in msg

    def test_no_comparison_falls_back_to_plain_message(self) -> None:
        # claude-code + manual hosts never set comparison; helper
        # must handle the None case cleanly.
        from schemabrain.setup.wizard import _wire_host_message

        result = InitResult(
            host="claude-code",
            snippet=_build_snippet_for_test(),
            state="shell_out_succeeded",
        )
        msg = _wire_host_message(result, comparison=None)
        assert "registered schemabrain with Claude Code" in msg


class TestFormatMcpEntryDiff:
    """D3: unified-diff formatter feeding the wizard's inline preview."""

    def test_full_diff_when_entries_differ(self, tmp_path: Path) -> None:
        from schemabrain.setup.config_io import format_mcp_entry_diff

        existing = {
            "command": "uvx",
            "args": ["schemabrain==0.3.0", "serve", "--store-path", "/old/sb.db"],
            "env": {"OLD_DB_URL": "postgresql://u@h/d"},
        }
        new = {
            "command": "uvx",
            "args": ["schemabrain==0.3.0", "serve", "--store-path", "/new/sb.db"],
            "env": {"DATABASE_URL": "postgresql://u@h/d"},
        }
        cfg = tmp_path / "claude_desktop_config.json"
        diff = format_mcp_entry_diff(
            existing_entry=existing,
            new_entry=new,
            config_path=cfg,
        )
        # Headers carry the config path + which-side suffix so the
        # operator can see the file context.
        assert f"--- {cfg} (current)" in diff
        assert f"+++ {cfg} (after init)" in diff
        # Both changed values surface (one removed, one added).
        assert "/old/sb.db" in diff
        assert "/new/sb.db" in diff
        assert "OLD_DB_URL" in diff
        assert "DATABASE_URL" in diff
        # Removal/addition leaders present — this is what the
        # wizard renderer colorizes (red for `-`, green for `+`).
        assert any(
            line.startswith("-") and not line.startswith("---") for line in diff.splitlines()
        )
        assert any(
            line.startswith("+") and not line.startswith("+++") for line in diff.splitlines()
        )

    def test_empty_diff_when_entries_identical(self, tmp_path: Path) -> None:
        from schemabrain.setup.config_io import format_mcp_entry_diff

        entry = {"command": "uvx", "args": ["schemabrain"], "env": {}}
        diff = format_mcp_entry_diff(
            existing_entry=entry,
            new_entry=entry,
            config_path=tmp_path / "x.json",
        )
        # difflib emits zero bytes when the inputs are identical —
        # the wizard relies on `if not comparison.diff_preview` to
        # skip the diff block, so this contract must hold.
        assert diff == ""

    def test_existing_none_renders_pure_addition(self, tmp_path: Path) -> None:
        from schemabrain.setup.config_io import format_mcp_entry_diff

        new = {"command": "uvx", "args": ["schemabrain"], "env": {}}
        diff = format_mcp_entry_diff(
            existing_entry=None,
            new_entry=new,
            config_path=tmp_path / "x.json",
        )
        # No removal lines — every entry line shows up as `+`.
        non_header = [
            line for line in diff.splitlines() if not line.startswith(("---", "+++", "@@"))
        ]
        assert non_header  # there are body lines
        for line in non_header:
            assert line.startswith("+")

    def test_keys_sorted_for_stable_diff(self, tmp_path: Path) -> None:
        # The formatter must be deterministic regardless of dict
        # iteration order — otherwise unrelated key-shuffles inside
        # the env block would surface as a spurious "diff" and the
        # operator would re-approve no-op overwrites.
        from schemabrain.setup.config_io import format_mcp_entry_diff

        existing = {"env": {"B": "1", "A": "2"}, "command": "uvx", "args": []}
        new = {"command": "uvx", "args": [], "env": {"A": "2", "B": "1"}}
        diff = format_mcp_entry_diff(
            existing_entry=existing,
            new_entry=new,
            config_path=tmp_path / "x.json",
        )
        assert diff == ""


# ----- _is_pypi_install + _resolve_runner (FC1 — version-skew protection) ---


class TestIsPypiInstall:
    """`_is_pypi_install()` decides whether the wizard's uvx pin is safe.

    True → `pip install schemabrain` from PyPI; PyPI's published wheel of
        the same version matches the locally-installed substrate, so
        uvx-launched serve will read the same store schema the wizard
        wrote.
    False → local wheel / editable / VCS install; PyPI's published wheel
        of the same package version may have an older substrate (store
        schema, charter version) and uvx-launched serve would refuse the
        local store. Caller should use absolute-path runner instead.
    """

    def test_returns_true_when_direct_url_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # PEP 610: pip omits direct_url.json when installing from an
        # index. PyPI is the canonical index.
        class _Stub:
            def read_text(self, _name: str) -> str | None:
                return None

        monkeypatch.setattr(
            "importlib.metadata.distribution",
            lambda _name: _Stub(),
        )
        from schemabrain.setup.init_flow import _is_pypi_install

        assert _is_pypi_install() is True

    def test_returns_false_when_direct_url_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # direct_url.json is written for file://, VCS, and editable
        # installs. Any of those means the local substrate may diverge
        # from PyPI's published wheel of the same version.
        class _Stub:
            def read_text(self, _name: str) -> str | None:
                return '{"url": "file:///tmp/schemabrain-0.3.0.whl", "archive_info": {}}'

        monkeypatch.setattr(
            "importlib.metadata.distribution",
            lambda _name: _Stub(),
        )
        from schemabrain.setup.init_flow import _is_pypi_install

        assert _is_pypi_install() is False

    def test_returns_false_when_package_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Edge case: running uninstalled (e.g., from a checkout via
        # `python -m schemabrain`). The package isn't on PyPI under
        # this exact version either; treat as non-PyPI.
        import importlib.metadata as md

        def _raise(_name: str) -> object:
            raise md.PackageNotFoundError("schemabrain")

        monkeypatch.setattr("importlib.metadata.distribution", _raise)
        from schemabrain.setup.init_flow import _is_pypi_install

        assert _is_pypi_install() is False


class TestResolveRunnerPypiDetection:
    """`_resolve_runner` picks uvx only when the local install is PyPI-style.

    The default `uvx schemabrain==<__version__>` pin is reproducible
    across host restarts only when PyPI publishes a wheel with the same
    version AND the same substrate. For local wheel / editable / VCS
    installs that share the same package version but a newer substrate,
    `uvx` would fetch an older PyPI substrate that can't read the local
    store. The resolver falls back to the absolute-path runner so the
    host launches the same code that ran the wizard.
    """

    def test_uses_uvx_when_pypi_install_and_uvx_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "schemabrain.setup.init_flow._is_pypi_install",
            lambda: True,
        )
        monkeypatch.setattr(
            "shutil.which",
            lambda n: "/usr/local/bin/uvx" if n == "uvx" else None,
        )
        from schemabrain.setup.init_flow import _resolve_runner

        assert _resolve_runner() == "uvx"

    def test_uses_absolute_path_when_local_wheel_even_if_uvx_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The FC1 case: local wheel install, uvx is on PATH, but uvx
        # would fetch PyPI's older substrate. Resolver MUST fall back
        # to absolute path even though uvx is available.
        monkeypatch.setattr(
            "schemabrain.setup.init_flow._is_pypi_install",
            lambda: False,
        )

        def _which(n: str) -> str | None:
            if n == "uvx":
                return "/usr/local/bin/uvx"
            if n == "schemabrain":
                return "/private/tmp/venv/bin/schemabrain"
            return None

        monkeypatch.setattr("shutil.which", _which)
        from schemabrain.setup.init_flow import _resolve_runner

        assert _resolve_runner() == "/private/tmp/venv/bin/schemabrain"

    def test_uses_absolute_path_when_uvx_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # uvx not on PATH at all (containers / minimal envs). Falls
        # back to the local entrypoint regardless of install source.
        monkeypatch.setattr(
            "schemabrain.setup.init_flow._is_pypi_install",
            lambda: True,
        )

        def _which(n: str) -> str | None:
            return "/usr/local/bin/schemabrain" if n == "schemabrain" else None

        monkeypatch.setattr("shutil.which", _which)
        from schemabrain.setup.init_flow import _resolve_runner

        assert _resolve_runner() == "/usr/local/bin/schemabrain"

    def test_raises_when_neither_uvx_nor_schemabrain_on_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "schemabrain.setup.init_flow._is_pypi_install",
            lambda: True,
        )
        monkeypatch.setattr("shutil.which", lambda _n: None)
        from schemabrain.setup.init_flow import InitRefusal, _resolve_runner

        with pytest.raises(InitRefusal) as exc_info:
            _resolve_runner()
        assert exc_info.value.error.kind == "init_runner_missing"
