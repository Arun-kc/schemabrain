"""Tests for the --events-path / --no-events wiring on `schemabrain serve`."""

from __future__ import annotations

from pathlib import Path

import pytest

from schemabrain.cli import main as cli_main
from schemabrain.core.store import SQLiteStore
from schemabrain.observability.bus import JsonlEventBus, NullEventBus


def _seed_store(path: Path) -> None:
    SQLiteStore(path).close()


def _patch_run_stdio_capture(monkeypatch: pytest.MonkeyPatch) -> dict:
    captured: dict[str, object] = {}

    def _capture(
        *,
        store,
        source_connection_id,
        embedder,
        metric_executor=None,
        event_bus=None,
        server_session_id=None,
    ) -> None:
        captured["event_bus"] = event_bus
        captured["event_bus_type"] = type(event_bus).__name__

    monkeypatch.setattr("schemabrain.cli.run_stdio", _capture)
    return captured


class TestServeBusWiring:
    def test_default_creates_jsonl_bus_at_default_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Sandbox HOME so the default ~/.schemabrain/ never touches the
        # user's real path.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("SCHEMABRAIN_EVENTS_PATH", raising=False)
        store_path = tmp_path / "store.db"
        _seed_store(store_path)
        captured = _patch_run_stdio_capture(monkeypatch)
        url = "postgresql+psycopg://fake/serve"
        monkeypatch.setenv("FAKE_URL", url)

        exit_code = cli_main(
            [
                "serve",
                "--url-env",
                "FAKE_URL",
                "--store-path",
                str(store_path),
            ]
        )
        assert exit_code == 0
        bus = captured["event_bus"]
        assert isinstance(bus, JsonlEventBus)
        # Default path resolves under sandboxed HOME.
        assert str(bus.path).startswith(str(tmp_path))
        assert bus.path.name == "events.jsonl"

    def test_explicit_events_path_used(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        store_path = tmp_path / "store.db"
        _seed_store(store_path)
        events_path = tmp_path / "custom" / "e.jsonl"
        captured = _patch_run_stdio_capture(monkeypatch)
        monkeypatch.setenv("FAKE_URL", "postgresql+psycopg://fake/serve")

        cli_main(
            [
                "serve",
                "--url-env",
                "FAKE_URL",
                "--store-path",
                str(store_path),
                "--events-path",
                str(events_path),
            ]
        )
        bus = captured["event_bus"]
        assert isinstance(bus, JsonlEventBus)
        assert bus.path == events_path

    def test_env_var_used_when_flag_absent(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        store_path = tmp_path / "store.db"
        _seed_store(store_path)
        env_events_path = tmp_path / "envdir" / "e.jsonl"
        monkeypatch.setenv("SCHEMABRAIN_EVENTS_PATH", str(env_events_path))
        captured = _patch_run_stdio_capture(monkeypatch)
        monkeypatch.setenv("FAKE_URL", "postgresql+psycopg://fake/serve")

        cli_main(
            [
                "serve",
                "--url-env",
                "FAKE_URL",
                "--store-path",
                str(store_path),
            ]
        )
        bus = captured["event_bus"]
        assert bus.path == env_events_path

    def test_flag_overrides_env(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        store_path = tmp_path / "store.db"
        _seed_store(store_path)
        flag_path = tmp_path / "flag.jsonl"
        env_path = tmp_path / "env.jsonl"
        monkeypatch.setenv("SCHEMABRAIN_EVENTS_PATH", str(env_path))
        captured = _patch_run_stdio_capture(monkeypatch)
        monkeypatch.setenv("FAKE_URL", "postgresql+psycopg://fake/serve")

        cli_main(
            [
                "serve",
                "--url-env",
                "FAKE_URL",
                "--store-path",
                str(store_path),
                "--events-path",
                str(flag_path),
            ]
        )
        bus = captured["event_bus"]
        assert bus.path == flag_path

    def test_no_events_uses_null_bus(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        store_path = tmp_path / "store.db"
        _seed_store(store_path)
        captured = _patch_run_stdio_capture(monkeypatch)
        monkeypatch.setenv("FAKE_URL", "postgresql+psycopg://fake/serve")

        cli_main(
            [
                "serve",
                "--url-env",
                "FAKE_URL",
                "--store-path",
                str(store_path),
                "--no-events",
            ]
        )
        bus = captured["event_bus"]
        assert isinstance(bus, NullEventBus)

    def test_unwritable_events_path_falls_back_to_null_bus_with_warning(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A bad --events-path (e.g. read-only volume, no parent
        permissions) must NOT crash the serve process. It falls back
        to NullEventBus with a stderr warning so the operator can
        diagnose and the server is still useful."""
        import sys as _sys

        store_path = tmp_path / "store.db"
        _seed_store(store_path)
        captured = _patch_run_stdio_capture(monkeypatch)
        monkeypatch.setenv("FAKE_URL", "postgresql+psycopg://fake/serve")

        # `_cmd_serve` does `from schemabrain.observability import
        # JsonlEventBus, NullEventBus` locally. Patch the source module
        # so the local import picks up the exploding replacement.
        bus_mod = _sys.modules["schemabrain.observability.bus"]
        obs_pkg = _sys.modules["schemabrain.observability"]

        def _explode(*args: object, **kwargs: object) -> object:
            raise OSError("Read-only file system")

        monkeypatch.setattr(bus_mod, "JsonlEventBus", _explode)
        monkeypatch.setattr(obs_pkg, "JsonlEventBus", _explode)

        exit_code = cli_main(
            [
                "serve",
                "--url-env",
                "FAKE_URL",
                "--store-path",
                str(store_path),
                "--events-path",
                "/nonexistent/path/events.jsonl",
            ]
        )
        assert exit_code == 0
        bus = captured["event_bus"]
        assert isinstance(bus, NullEventBus)
        stderr = capsys.readouterr().err
        assert "cannot initialise events file" in stderr
        assert "Read-only file system" in stderr


class TestRunStdioLifecycleEvents:
    def test_emits_server_start_and_stop(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """run_stdio emits server_start before app.run and server_stop in
        the finally block so KeyboardInterrupt still produces a stop event.
        """
        from schemabrain.core.embedding import ColumnEmbedding
        from schemabrain.core.models import Column, Table
        from schemabrain.mcp.server import run_stdio

        store = SQLiteStore(tmp_path / "s.db")
        sid = "src1"
        store.write_table(
            Table(
                schema_name="public",
                name="users",
                kind="TABLE",
                columns=(
                    Column(
                        name="id",
                        table_name="users",
                        schema_name="public",
                        data_type="INT",
                        nullable=False,
                        ordinal_position=1,
                    ),
                ),
            ),
            source_connection_id=sid,
        )
        store.write_table_embeddings(
            "public",
            "users",
            source_connection_id=sid,
            embeddings={"id": ColumnEmbedding(vector=(1.0, 0.0, 0.0, 0.0), model="t", dimension=4)},
        )

        class _StubEmbedder:
            def embed(self, text: str) -> tuple[float, ...]:
                return (1.0, 0.0, 0.0, 0.0)

        emitted = []

        class _CapturingBus:
            def emit(self, event) -> None:
                emitted.append(event)

            def close(self) -> None:
                pass

        # Replace app.run with a no-op so run_stdio returns immediately
        # after wiring everything up.
        class _FakeApp:
            def run(self, transport: str) -> None:
                return

        def _build_fake_server(**_kw) -> _FakeApp:
            return _FakeApp()

        monkeypatch.setattr("schemabrain.mcp.server.build_server", _build_fake_server)

        run_stdio(
            store=store,
            source_connection_id=sid,
            embedder=_StubEmbedder(),
            event_bus=_CapturingBus(),
            server_session_id="fixed-session-id",
        )

        subtypes = [ev.event_subtype for ev in emitted]
        assert subtypes == ["server_start", "server_stop"]
        assert all(ev.server_session_id == "fixed-session-id" for ev in emitted)
        store.close()

    def test_server_stop_emitted_even_on_exception(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from schemabrain.core.embedding import ColumnEmbedding
        from schemabrain.core.models import Column, Table
        from schemabrain.mcp.server import run_stdio

        store = SQLiteStore(tmp_path / "s.db")
        sid = "src1"
        store.write_table(
            Table(
                schema_name="public",
                name="users",
                kind="TABLE",
                columns=(
                    Column(
                        name="id",
                        table_name="users",
                        schema_name="public",
                        data_type="INT",
                        nullable=False,
                        ordinal_position=1,
                    ),
                ),
            ),
            source_connection_id=sid,
        )
        store.write_table_embeddings(
            "public",
            "users",
            source_connection_id=sid,
            embeddings={"id": ColumnEmbedding(vector=(1.0, 0.0, 0.0, 0.0), model="t", dimension=4)},
        )

        class _StubEmbedder:
            def embed(self, text: str) -> tuple[float, ...]:
                return (1.0, 0.0, 0.0, 0.0)

        emitted = []

        class _CapturingBus:
            def emit(self, event) -> None:
                emitted.append(event)

            def close(self) -> None:
                pass

        class _ExplodingApp:
            def run(self, transport: str) -> None:
                raise KeyboardInterrupt()

        monkeypatch.setattr("schemabrain.mcp.server.build_server", lambda **_kw: _ExplodingApp())

        with pytest.raises(KeyboardInterrupt):
            run_stdio(
                store=store,
                source_connection_id=sid,
                embedder=_StubEmbedder(),
                event_bus=_CapturingBus(),
                server_session_id="x",
            )

        # The finally block must have emitted server_stop.
        subtypes = [ev.event_subtype for ev in emitted]
        assert "server_stop" in subtypes
        store.close()
