"""Tests for `schemabrain.setup.doctor_flow` — the doctor check matrix
+ orchestrator + terminal renderer.

Goals:

  1. Each check function returns a `Check` with the expected outcome
     across its present/absent/broken input cases. Names are stable
     identifiers users will grep for.
  2. The orchestrator (`doctor`) composes the right checks based on
     which inputs are supplied: config-only when no source URL,
     adds source+read-only when --source/--url-env is supplied,
     gates the read-only check off on SQLite source URLs.
  3. `render_doctor` writes a colored summary + per-check line; the
     output contains pass/warn/fail glyphs deterministically; the
     final summary line counts each outcome.
  4. The JSON render produces the documented contract shape
     (checks / summary / exit_code) and roundtrips via stdlib json.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore
from schemabrain.setup.checks import Check, CheckResult
from schemabrain.setup.doctor_flow import (
    check_host_config_present,
    check_host_config_store_path_matches,
    check_host_config_uses_url_env,
    check_host_config_version_pin_matches,
    check_installed_version,
    check_source_reachable,
    check_source_read_only,
    check_store_entity_count,
    check_store_freshness,
    check_store_schema_version,
    check_uvx_invocable,
    doctor,
    render_doctor,
    render_doctor_json,
)

# ----- fixtures -------------------------------------------------------------


@pytest.fixture
def fresh_store(tmp_path: Path) -> Iterator[Path]:
    """An empty, schema-matching store at a known path."""
    path = tmp_path / "store.db"
    store = SQLiteStore(path=path)
    try:
        yield path
    finally:
        store.close()


@pytest.fixture
def seeded_store(tmp_path: Path) -> Iterator[Path]:
    """A store with one indexed table + one entity, freshness ~now."""
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


def _write_host_config(path: Path, entry: dict[str, object] | None) -> None:
    """Write a Claude Desktop config containing the supplied schemabrain entry.

    `entry=None` writes a config with an EMPTY mcpServers block — used
    to exercise the "file exists, entry missing" failure mode.
    """
    config = {"mcpServers": {}}
    if entry is not None:
        config["mcpServers"]["schemabrain"] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2))


# ----- check_uvx_invocable --------------------------------------------------


class TestCheckUvxInvocable:
    def test_pass_when_uvx_on_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("shutil.which", lambda n: "/usr/local/bin/uvx" if n == "uvx" else None)
        c = check_uvx_invocable()
        assert c.outcome == "pass"
        assert c.name == "uvx_invocable"

    def test_warn_when_uvx_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # warn (not fail) because the installed entrypoint fallback in
        # build_snippet still produces a working snippet.
        monkeypatch.setattr("shutil.which", lambda _n: None)
        c = check_uvx_invocable()
        assert c.outcome == "warn"
        assert c.suggested_next is not None


# ----- check_installed_version ----------------------------------------------


class TestCheckInstalledVersion:
    def test_pass_for_normal_release_version(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("schemabrain.setup.doctor_flow._installed_version", lambda: "0.2.0a1")
        c = check_installed_version()
        assert c.outcome == "pass"
        assert "0.2.0a1" in c.message

    def test_warn_for_dev_install_with_local_segment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A PEP 440 local version segment (e.g. "0.2.0a1+dirty") signals
        # an unstable build — the snippet's pin would be unreproducible
        # via uvx, so warn the user.
        monkeypatch.setattr(
            "schemabrain.setup.doctor_flow._installed_version",
            lambda: "0.2.0a1+dev.abc123",
        )
        c = check_installed_version()
        assert c.outcome == "warn"
        assert c.suggested_next is not None

    def test_warn_when_package_metadata_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Editable install (`pip install -e .`) sometimes leaves the
        # metadata in an unusable shape. We warn rather than fail —
        # the runtime might still work.
        def boom() -> str:
            from importlib.metadata import PackageNotFoundError

            raise PackageNotFoundError("schemabrain")

        monkeypatch.setattr("schemabrain.setup.doctor_flow._installed_version", boom)
        c = check_installed_version()
        assert c.outcome == "warn"


# ----- check_host_config_present --------------------------------------------


class TestCheckHostConfigPresent:
    def test_fail_when_config_file_missing(self, tmp_path: Path) -> None:
        c = check_host_config_present(tmp_path / "does-not-exist.json")
        assert c.outcome == "fail"
        assert "init" in (c.suggested_next or "")

    def test_fail_when_schemabrain_entry_missing(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.json"
        _write_host_config(cfg, entry=None)
        c = check_host_config_present(cfg)
        assert c.outcome == "fail"

    def test_pass_when_schemabrain_entry_present(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.json"
        _write_host_config(cfg, entry={"command": "uvx", "args": ["serve"], "env": {}})
        c = check_host_config_present(cfg)
        assert c.outcome == "pass"

    def test_fail_when_config_malformed(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.json"
        cfg.write_text("{not: json}")
        c = check_host_config_present(cfg)
        assert c.outcome == "fail"
        assert "valid JSON" in c.message or "json" in c.message.lower()

    def test_present_check_propagates_malformed_early_return(self, tmp_path: Path) -> None:
        # Drives the early-return path inside check_host_config_present
        # where _safe_read_config has already constructed the fail
        # Check (covers the `return early` arm distinct from the
        # path.exists() fast-path).
        cfg = tmp_path / "config.json"
        cfg.write_text('{"valid": "json",\n  "but_unterminated":}')
        c = check_host_config_present(cfg)
        assert c.outcome == "fail"


# ----- check_host_config_uses_url_env ---------------------------------------


class TestCheckHostConfigUsesUrlEnv:
    def test_pass_when_args_contain_url_env(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.json"
        _write_host_config(
            cfg,
            entry={
                "command": "uvx",
                "args": ["schemabrain==0.1", "serve", "--url-env", "DB"],
                "env": {"DB": "postgresql://x"},
            },
        )
        c = check_host_config_uses_url_env(cfg)
        assert c.outcome == "pass"

    def test_fail_when_source_used_with_inline_url(self, tmp_path: Path) -> None:
        # Security regression — re-running init should overwrite this
        # but doctor catches the drift even before that.
        cfg = tmp_path / "config.json"
        _write_host_config(
            cfg,
            entry={
                "command": "uvx",
                "args": [
                    "schemabrain==0.1",
                    "serve",
                    "--source",
                    "postgresql://leaked:secret@h/db",
                ],
                "env": {},
            },
        )
        c = check_host_config_uses_url_env(cfg)
        assert c.outcome == "fail"
        assert "init" in (c.suggested_next or "")

    def test_warn_when_no_schemabrain_entry(self, tmp_path: Path) -> None:
        # Different from "fail" — the present-check above is the
        # canonical failure for that. This one just observes
        # "can't evaluate" rather than re-reporting the same fact.
        cfg = tmp_path / "config.json"
        _write_host_config(cfg, entry=None)
        c = check_host_config_uses_url_env(cfg)
        assert c.outcome == "warn"

    def test_warn_when_missing_config_file(self, tmp_path: Path) -> None:
        # _safe_read_config returns (None, None) for missing files;
        # the outer check then sees entry=None and warns.
        c = check_host_config_uses_url_env(tmp_path / "missing.json")
        assert c.outcome == "warn"

    def test_warn_when_args_has_neither_url_env_nor_source(self, tmp_path: Path) -> None:
        # Edge: a hand-edited snippet missing both flags. Not a
        # security regression (no --source), but not the canonical
        # shape either — observable but not fatal.
        cfg = tmp_path / "config.json"
        _write_host_config(
            cfg,
            entry={"command": "uvx", "args": ["schemabrain==0.1", "serve"], "env": {}},
        )
        c = check_host_config_uses_url_env(cfg)
        assert c.outcome == "warn"

    def test_warn_when_args_is_not_a_list(self, tmp_path: Path) -> None:
        # Defensive: a hand-broken snippet with args as a string.
        # Don't crash on it; just warn.
        cfg = tmp_path / "config.json"
        _write_host_config(
            cfg,
            entry={"command": "uvx", "args": "schemabrain serve", "env": {}},  # type: ignore[dict-item]
        )
        c = check_host_config_uses_url_env(cfg)
        assert c.outcome == "warn"

    def test_propagates_malformed_early_return(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.json"
        cfg.write_text("{broken json")
        c = check_host_config_uses_url_env(cfg)
        assert c.outcome == "fail"

    def test_propagated_malformed_check_has_correct_name(self, tmp_path: Path) -> None:
        # Regression guard: _safe_read_config must propagate the
        # caller's name onto the synthesized fail Check rather than
        # emitting a hardcoded one — otherwise multiple callers all
        # produce Check rows with the same name in --json output.
        cfg = tmp_path / "config.json"
        cfg.write_text("{broken json")
        c = check_host_config_uses_url_env(cfg)
        assert c.name == "host_config_uses_url_env"

    def test_detects_source_equals_form(self, tmp_path: Path) -> None:
        # The single-token --source=URL form is just as dangerous as
        # the two-token form — both leak credentials into argv. The
        # check must detect both.
        cfg = tmp_path / "config.json"
        _write_host_config(
            cfg,
            entry={
                "command": "uvx",
                "args": [
                    "schemabrain==0.1",
                    "serve",
                    "--source=postgresql://leaked:secret@h/db",
                ],
                "env": {},
            },
        )
        c = check_host_config_uses_url_env(cfg)
        assert c.outcome == "fail"


# ----- check_host_config_version_pin_matches --------------------------------


class TestCheckHostConfigVersionPinMatches:
    def test_pass_when_pin_matches(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("schemabrain.setup.doctor_flow._installed_version", lambda: "0.2.0a1")
        cfg = tmp_path / "config.json"
        _write_host_config(
            cfg,
            entry={
                "command": "uvx",
                "args": ["schemabrain==0.2.0a1", "serve"],
                "env": {},
            },
        )
        c = check_host_config_version_pin_matches(cfg)
        assert c.outcome == "pass"

    def test_warn_when_pin_drifts(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("schemabrain.setup.doctor_flow._installed_version", lambda: "0.3.0")
        cfg = tmp_path / "config.json"
        _write_host_config(
            cfg,
            entry={
                "command": "uvx",
                "args": ["schemabrain==0.2.0a1", "serve"],
                "env": {},
            },
        )
        c = check_host_config_version_pin_matches(cfg)
        assert c.outcome == "warn"
        assert "0.2.0a1" in c.message
        assert "0.3.0" in c.message

    def test_warn_when_no_pin_arg(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.json"
        _write_host_config(
            cfg,
            entry={"command": "/usr/local/bin/schemabrain", "args": ["serve"], "env": {}},
        )
        c = check_host_config_version_pin_matches(cfg)
        # Absolute-path runner has no pin; observed but not failure.
        assert c.outcome == "warn"

    def test_warn_when_entry_missing(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.json"
        _write_host_config(cfg, entry=None)
        c = check_host_config_version_pin_matches(cfg)
        assert c.outcome == "warn"

    def test_warn_when_missing_config_file(self, tmp_path: Path) -> None:
        c = check_host_config_version_pin_matches(tmp_path / "missing.json")
        assert c.outcome == "warn"

    def test_warn_when_package_metadata_unresolvable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom() -> str:
            from importlib.metadata import PackageNotFoundError

            raise PackageNotFoundError("schemabrain")

        monkeypatch.setattr("schemabrain.setup.doctor_flow._installed_version", boom)
        cfg = tmp_path / "config.json"
        _write_host_config(
            cfg,
            entry={"command": "uvx", "args": ["schemabrain==0.2.0a1", "serve"], "env": {}},
        )
        c = check_host_config_version_pin_matches(cfg)
        assert c.outcome == "warn"
        assert "unresolvable" in c.message

    def test_propagates_malformed_early_return(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.json"
        cfg.write_text("{not json")
        c = check_host_config_version_pin_matches(cfg)
        assert c.outcome == "fail"

    def test_warn_when_args_is_not_a_list(self, tmp_path: Path) -> None:
        # Hand-broken snippet with args as a string. Don't iterate
        # the characters — the isinstance(list) guard short-circuits;
        # outer code reports "no pin found".
        cfg = tmp_path / "config.json"
        _write_host_config(
            cfg,
            entry={
                "command": "uvx",
                "args": "schemabrain==0.1 serve",  # type: ignore[dict-item]
                "env": {},
            },
        )
        c = check_host_config_version_pin_matches(cfg)
        assert c.outcome == "warn"

    def test_propagated_malformed_check_has_correct_name(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.json"
        cfg.write_text("{broken json")
        c = check_host_config_version_pin_matches(cfg)
        assert c.name == "host_config_version_pin"


# ----- check_host_config_store_path_matches ---------------------------------


class TestCheckHostConfigStorePathMatches:
    def test_pass_when_paths_match(self, tmp_path: Path) -> None:
        store = (tmp_path / "store.db").resolve()
        cfg = tmp_path / "config.json"
        _write_host_config(
            cfg,
            entry={
                "command": "uvx",
                "args": ["schemabrain==0.1", "serve", "--store-path", str(store)],
                "env": {},
            },
        )
        c = check_host_config_store_path_matches(cfg, expected=store)
        assert c.outcome == "pass"

    def test_warn_when_paths_drift(self, tmp_path: Path) -> None:
        snippet_store = (tmp_path / "old.db").resolve()
        expected_store = (tmp_path / "new.db").resolve()
        cfg = tmp_path / "config.json"
        _write_host_config(
            cfg,
            entry={
                "command": "uvx",
                "args": [
                    "schemabrain==0.1",
                    "serve",
                    "--store-path",
                    str(snippet_store),
                ],
                "env": {},
            },
        )
        c = check_host_config_store_path_matches(cfg, expected=expected_store)
        assert c.outcome == "warn"

    def test_warn_when_no_store_path_in_snippet(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.json"
        _write_host_config(
            cfg,
            entry={"command": "uvx", "args": ["schemabrain==0.1", "serve"], "env": {}},
        )
        c = check_host_config_store_path_matches(cfg, expected=tmp_path / "store.db")
        assert c.outcome == "warn"

    def test_warn_when_missing_config_file(self, tmp_path: Path) -> None:
        c = check_host_config_store_path_matches(
            tmp_path / "missing.json", expected=tmp_path / "store.db"
        )
        assert c.outcome == "warn"

    def test_warn_when_entry_missing(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.json"
        _write_host_config(cfg, entry=None)
        c = check_host_config_store_path_matches(cfg, expected=tmp_path / "store.db")
        assert c.outcome == "warn"

    def test_propagates_malformed_early_return(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.json"
        cfg.write_text("{broken")
        c = check_host_config_store_path_matches(cfg, expected=tmp_path / "store.db")
        assert c.outcome == "fail"

    def test_warn_when_args_is_not_a_list(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.json"
        _write_host_config(
            cfg,
            entry={
                "command": "uvx",
                "args": "serve --store-path /x",  # type: ignore[dict-item]
                "env": {},
            },
        )
        c = check_host_config_store_path_matches(cfg, expected=tmp_path / "store.db")
        assert c.outcome == "warn"

    def test_warn_when_store_path_arg_at_end_of_args(self, tmp_path: Path) -> None:
        # Defensive: --store-path is the last token, no value after.
        # The check shouldn't crash on IndexError; it should warn.
        cfg = tmp_path / "config.json"
        _write_host_config(
            cfg,
            entry={
                "command": "uvx",
                "args": ["schemabrain==0.1", "serve", "--store-path"],
                "env": {},
            },
        )
        c = check_host_config_store_path_matches(cfg, expected=tmp_path / "store.db")
        assert c.outcome == "warn"

    def test_propagated_malformed_check_has_correct_name(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.json"
        cfg.write_text("{broken json")
        c = check_host_config_store_path_matches(cfg, expected=tmp_path / "store.db")
        assert c.name == "host_config_store_path"

    def test_warn_when_store_path_value_is_not_a_string(self, tmp_path: Path) -> None:
        # Defensive: hand-broken JSON with a non-string after --store-path.
        cfg = tmp_path / "config.json"
        _write_host_config(
            cfg,
            entry={
                "command": "uvx",
                "args": ["schemabrain==0.1", "serve", "--store-path", 42],  # type: ignore[list-item]
                "env": {},
            },
        )
        c = check_host_config_store_path_matches(cfg, expected=tmp_path / "store.db")
        assert c.outcome == "warn"


# ----- check_store_schema_version -------------------------------------------


class TestCheckStoreSchemaVersion:
    def test_pass_when_store_opens_cleanly(self, fresh_store: Path) -> None:
        c = check_store_schema_version(fresh_store)
        assert c.outcome == "pass"

    def test_warn_when_store_file_missing(self, tmp_path: Path) -> None:
        # warn (not fail) — a missing store is a degenerate state but
        # also matches the "first-run before index" case.
        c = check_store_schema_version(tmp_path / "missing.db")
        assert c.outcome == "warn"

    def test_fail_when_schema_version_mismatched(self, tmp_path: Path) -> None:
        # Hand-craft a store file with the wrong schema_version row.
        import sqlite3

        path = tmp_path / "store.db"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE schemabrain_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO schemabrain_meta VALUES ('schema_version', '999')")
        conn.commit()
        conn.close()
        c = check_store_schema_version(path)
        assert c.outcome == "fail"


# ----- check_store_entity_count ---------------------------------------------


class TestCheckStoreEntityCount:
    def test_pass_when_entities_present(self, seeded_store: Path) -> None:
        c = check_store_entity_count(seeded_store)
        assert c.outcome == "pass"
        assert "1" in c.message

    def test_warn_when_no_entities(self, fresh_store: Path) -> None:
        c = check_store_entity_count(fresh_store)
        assert c.outcome == "warn"
        assert c.suggested_next is not None

    def test_warn_when_store_missing(self, tmp_path: Path) -> None:
        c = check_store_entity_count(tmp_path / "missing.db")
        assert c.outcome == "warn"


# ----- check_store_freshness ------------------------------------------------


class TestCheckStoreFreshness:
    def test_pass_when_recently_indexed(self, seeded_store: Path) -> None:
        c = check_store_freshness(seeded_store)
        assert c.outcome == "pass"

    def test_warn_when_no_tables(self, fresh_store: Path) -> None:
        c = check_store_freshness(fresh_store)
        assert c.outcome == "warn"

    def test_warn_when_indexed_too_long_ago(
        self, seeded_store: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Pretend the wall clock is 90 days ahead of the indexed_at
        # timestamp the seed wrote.
        import time as _time_module

        original_time = _time_module.time()

        def fast_forward() -> float:
            return original_time + 90 * 24 * 60 * 60

        monkeypatch.setattr("schemabrain.setup.doctor_flow.time.time", fast_forward)
        c = check_store_freshness(seeded_store)
        assert c.outcome == "warn"
        assert "days" in c.message

    def test_warn_when_store_missing(self, tmp_path: Path) -> None:
        c = check_store_freshness(tmp_path / "missing.db")
        assert c.outcome == "warn"

    def test_warn_when_sqlite_query_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Defensive: a store file that exists but isn't a valid sqlite
        # database (e.g. a directory at that path on POSIX, or a junk
        # file). Don't crash on the freshness query — warn.
        import sqlite3

        path = tmp_path / "store.db"
        path.write_bytes(b"not a sqlite file")

        def boom(*_a: object, **_kw: object) -> object:
            raise sqlite3.DatabaseError("file is not a database")

        monkeypatch.setattr("sqlite3.connect", boom)
        c = check_store_freshness(path)
        assert c.outcome == "warn"
        assert "could not query" in c.message.lower()


# ----- check_source_reachable -----------------------------------------------


class TestCheckSourceReachable:
    def test_pass_against_sqlite_memory(self) -> None:
        c = check_source_reachable("sqlite:///:memory:")
        assert c.outcome == "pass"

    def test_fail_against_unreachable_url(self) -> None:
        # nonexistent host on an unlikely port; SQLAlchemy raises on
        # SELECT 1.
        c = check_source_reachable("postgresql+psycopg://u:p@127.0.0.1:1/db")
        assert c.outcome == "fail"

    def test_fail_on_engine_construction_error(self) -> None:
        c = check_source_reachable("not-a-valid-url://")
        assert c.outcome == "fail"


# ----- check_source_read_only -----------------------------------------------


class TestCheckSourceReadOnly:
    def test_pass_when_session_reports_read_only_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Stub SQLAlchemy's create_engine to return an engine whose
        # SHOW default_transaction_read_only response is 'on'.
        from schemabrain.setup import doctor_flow

        class _Conn:
            def execute(self, _query: object) -> object:
                class _Res:
                    def scalar(self) -> str:
                        return "on"

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

        monkeypatch.setattr(doctor_flow, "create_engine", lambda *_a, **_kw: _Engine())
        c = check_source_read_only("postgresql+psycopg://u:p@h/db")
        assert c.outcome == "pass"

    def test_fail_when_session_reports_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from schemabrain.setup import doctor_flow

        class _Conn:
            def execute(self, _query: object) -> object:
                class _Res:
                    def scalar(self) -> str:
                        return "off"

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

        monkeypatch.setattr(doctor_flow, "create_engine", lambda *_a, **_kw: _Engine())
        c = check_source_read_only("postgresql+psycopg://u:p@h/db")
        assert c.outcome == "fail"

    def test_fail_when_connect_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from schemabrain.setup import doctor_flow

        def boom(*_a: object, **_kw: object) -> object:
            raise RuntimeError("could not connect")

        monkeypatch.setattr(doctor_flow, "create_engine", boom)
        c = check_source_read_only("postgresql+psycopg://u:p@h/db")
        assert c.outcome == "fail"


# ----- doctor orchestrator --------------------------------------------------


class TestDoctorOrchestrator:
    def test_runs_minimal_check_set_with_no_source(
        self, fresh_store: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No source URL → no source-reachable / read-only checks.
        # Use manual host so config-related checks aren't added.
        result = doctor(source_url=None, store_path=fresh_store, host="manual")
        assert isinstance(result, CheckResult)
        names = [c.name for c in result.checks]
        assert "source_reachable" not in names
        assert "source_session_read_only" not in names

    def test_adds_source_checks_when_url_supplied(self, fresh_store: Path) -> None:
        result = doctor(
            source_url="sqlite:///:memory:",
            store_path=fresh_store,
            host="manual",
        )
        names = [c.name for c in result.checks]
        assert "source_reachable" in names

    def test_skips_read_only_check_for_sqlite_source(self, fresh_store: Path) -> None:
        # SQLite has no session-level read-only setting; skip the
        # check rather than reporting a confusing pass/fail.
        result = doctor(
            source_url="sqlite:///:memory:",
            store_path=fresh_store,
            host="manual",
        )
        names = [c.name for c in result.checks]
        assert "source_session_read_only" not in names

    def test_adds_host_checks_when_desktop_path_resolves(
        self, fresh_store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = tmp_path / "claude_desktop_config.json"
        _write_host_config(
            cfg,
            entry={
                "command": "uvx",
                "args": [
                    "schemabrain==0.2.0a1",
                    "serve",
                    "--url-env",
                    "DB",
                    "--store-path",
                    str(fresh_store.resolve()),
                ],
                "env": {"DB": "postgresql://x"},
            },
        )
        monkeypatch.setattr("schemabrain.setup.doctor_flow.claude_desktop_config_path", lambda: cfg)
        monkeypatch.setattr("schemabrain.setup.doctor_flow._installed_version", lambda: "0.2.0a1")
        result = doctor(source_url=None, store_path=fresh_store, host="claude-desktop")
        names = [c.name for c in result.checks]
        assert "host_config_present" in names
        assert "host_config_uses_url_env" in names

    def test_emits_warn_when_desktop_unsupported_on_this_os(
        self, fresh_store: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # User explicitly asked for --host claude-desktop on an OS
        # where the config path doesn't resolve (Linux, or Windows
        # without APPDATA). Doctor must NOT silently skip — it emits
        # a single warn check so the user sees the gap.
        monkeypatch.setattr(
            "schemabrain.setup.doctor_flow.claude_desktop_config_path", lambda: None
        )
        result = doctor(source_url=None, store_path=fresh_store, host="claude-desktop")
        host_checks = [c for c in result.checks if c.name == "host_config_present"]
        assert len(host_checks) == 1
        assert host_checks[0].outcome == "warn"
        assert "OS" in host_checks[0].message

    def test_emits_warn_for_claude_code_unverifiable(self, fresh_store: Path) -> None:
        # Claude Code registration lives in the supported `claude mcp`
        # CLI, not in a programmatically-introspectable file. Doctor
        # must surface that gap rather than silently passing.
        result = doctor(source_url=None, store_path=fresh_store, host="claude-code")
        host_checks = [c for c in result.checks if c.name == "host_config_present"]
        assert len(host_checks) == 1
        assert host_checks[0].outcome == "warn"
        assert "claude mcp list" in (host_checks[0].suggested_next or "")

    def test_adds_read_only_check_for_postgres_source(
        self,
        fresh_store: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The orchestrator gates check_source_read_only ON for Postgres
        # URLs. Mock create_engine so we don't actually try to reach a
        # nonexistent host — the existence of the check in the result
        # is what we're asserting.
        from schemabrain.setup import doctor_flow

        class _Conn:
            def execute(self, _query: object) -> object:
                class _Res:
                    def scalar(self) -> str:
                        return "on"

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

        monkeypatch.setattr(doctor_flow, "create_engine", lambda *_a, **_kw: _Engine())
        result = doctor(
            source_url="postgresql+psycopg://u:p@h/db",
            store_path=fresh_store,
            host="manual",
        )
        names = [c.name for c in result.checks]
        assert "source_session_read_only" in names

    def test_aggregates_to_zero_exit_code_when_all_pass_or_warn(self, seeded_store: Path) -> None:
        result = doctor(
            source_url="sqlite:///:memory:",
            store_path=seeded_store,
            host="manual",
        )
        # Some checks (uvx, version) may warn depending on test env,
        # but no fails should appear against the seeded store + sqlite
        # memory source. If they DO appear, treat that as a real
        # regression — the test surface relies on this being clean.
        assert result.exit_code == 0


# ----- render_doctor (terminal) ---------------------------------------------


def test_render_doctor_is_reexported_from_doctor_flow() -> None:
    """Pin the re-export contract ``cli.py`` actually uses in production.

    cli.py imports ``render_doctor`` from ``doctor_flow``, not from
    ``doctor_render`` directly. A refactor that moves or renames the
    renderer without updating the re-export would break the live CLI
    silently — none of the fine-grained tests in
    ``test_doctor_render.py`` exercise this import path because they
    import directly from ``doctor_render``.
    """
    from schemabrain.setup.doctor_flow import render_doctor as reexported
    from schemabrain.setup.doctor_render import render_doctor as canonical

    assert reexported is canonical


class TestRenderDoctor:
    """Substring-level pins against the design's checklist surface.

    Fine-grained layout pins (column widths, ordinal padding, brand-
    line composition) live in ``tests/test_doctor_render.py`` so this
    file stays focused on the orchestrator + check-logic contract.
    """

    def test_writes_summary_line(self) -> None:
        import io

        from rich.console import Console

        buf = io.StringIO()
        console = Console(file=buf, force_terminal=False, no_color=True, width=120)
        result = CheckResult(
            checks=(
                Check(name="a", outcome="pass", message="ok"),
                Check(name="b", outcome="warn", message="watch"),
                Check(name="c", outcome="fail", message="bad", suggested_next="fix it"),
            )
        )
        render_doctor(result, console=console)
        out = buf.getvalue()
        # New design's footer: ``N checks · A ok · B warn · C err``.
        assert "1 ok" in out
        assert "1 warn" in out
        assert "1 err" in out

    def test_writes_per_check_lines(self) -> None:
        import io

        from rich.console import Console

        buf = io.StringIO()
        console = Console(file=buf, force_terminal=False, no_color=True, width=120)
        result = CheckResult(
            checks=(Check(name="uvx_invocable", outcome="pass", message="uvx 0.5.1"),)
        )
        render_doctor(result, console=console)
        out = buf.getvalue()
        assert "uvx_invocable" in out
        assert "uvx 0.5.1" in out
        # Numbered prefix: first check renders with the ``01`` ordinal.
        assert "01" in out

    def test_suggested_next_rendered_under_check(self) -> None:
        import io

        from rich.console import Console

        buf = io.StringIO()
        console = Console(file=buf, force_terminal=False, no_color=True, width=120)
        result = CheckResult(
            checks=(
                Check(
                    name="x",
                    outcome="fail",
                    message="bad",
                    suggested_next="run `schemabrain init`",
                ),
            )
        )
        render_doctor(result, console=console)
        out = buf.getvalue()
        # The design's ``→ fix:`` prefix introduces the remediation line.
        assert "→ fix:" in out
        assert "schemabrain init" in out

    def test_empty_result_writes_summary_with_zero_counts(self) -> None:
        import io

        from rich.console import Console

        buf = io.StringIO()
        console = Console(file=buf, force_terminal=False, no_color=True, width=120)
        render_doctor(CheckResult(checks=()), console=console)
        out = buf.getvalue()
        # Empty-result footer renders with zero counts across all tiers.
        assert "0 checks" in out
        assert "0 ok" in out
        assert "0 err" in out


class TestRenderDoctorJson:
    def test_emits_json_with_doctor_contract_shape(self) -> None:
        result = CheckResult(checks=(Check(name="a", outcome="pass", message="ok"),))
        out = render_doctor_json(result)
        parsed = json.loads(out)
        assert set(parsed.keys()) == {"checks", "summary", "exit_code"}
        assert parsed["exit_code"] == 0
        assert parsed["summary"] == {"pass": 1, "warn": 0, "fail": 0}
