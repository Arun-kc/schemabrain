"""Tests for the --no-audit wiring on `schemabrain serve`."""

from __future__ import annotations

from pathlib import Path

import pytest

from schemabrain.audit.writer import AuditWriter
from schemabrain.cli import main as cli_main
from schemabrain.core.store import SQLiteStore


def _seed_store(path: Path) -> None:
    SQLiteStore(path).close()


def _patch_run_stdio_capture(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Stub out `run_stdio` so the CLI returns after wiring everything
    up. The capture dict records every kwarg the caller passed."""
    captured: dict[str, object] = {}

    def _capture(
        *,
        store,
        source_connection_id,
        embedder,
        metric_executor=None,
        event_bus=None,
        server_session_id=None,
        audit_writer=None,
        pii_block=frozenset(),
        tracer=None,
    ) -> None:
        captured["audit_writer"] = audit_writer
        captured["audit_writer_type"] = type(audit_writer).__name__
        captured["pii_block"] = pii_block

    monkeypatch.setattr("schemabrain.cli.run_stdio", _capture)
    return captured


class TestServeAuditWiring:
    def test_default_creates_audit_writer(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        store_path = tmp_path / "store.db"
        _seed_store(store_path)
        captured = _patch_run_stdio_capture(monkeypatch)
        monkeypatch.setenv("FAKE_URL", "postgresql+psycopg://fake/serve")

        exit_code = cli_main(
            [
                "serve",
                "--url-env",
                "FAKE_URL",
                "--store-path",
                str(store_path),
                "--no-events",  # silence bus side; focus on audit
            ]
        )
        assert exit_code == 0
        writer = captured["audit_writer"]
        assert isinstance(writer, AuditWriter)
        writer.close()

    def test_no_audit_flag_passes_none(
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
                "--no-audit",
            ]
        )
        assert captured["audit_writer"] is None

    def test_unwritable_store_falls_back_to_no_audit_with_warning(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A bad store path for the audit writer (read-only volume, no
        parent perms) must NOT crash serve. Falls back to None with a
        stderr warning so the operator can diagnose and the server is
        still useful."""
        import sys as _sys

        store_path = tmp_path / "store.db"
        _seed_store(store_path)
        captured = _patch_run_stdio_capture(monkeypatch)
        monkeypatch.setenv("FAKE_URL", "postgresql+psycopg://fake/serve")

        # _cmd_serve does `from schemabrain.audit.writer import
        # AuditWriter` locally. Patch the source module so the local
        # import picks up the exploding replacement.
        writer_mod = _sys.modules["schemabrain.audit.writer"]

        def _explode(*args: object, **kwargs: object) -> object:
            raise OSError("Read-only file system")

        monkeypatch.setattr(writer_mod, "AuditWriter", _explode)

        exit_code = cli_main(
            [
                "serve",
                "--url-env",
                "FAKE_URL",
                "--store-path",
                str(store_path),
                "--no-events",
            ]
        )
        assert exit_code == 0
        assert captured["audit_writer"] is None
        stderr = capsys.readouterr().err
        assert "cannot initialise audit writer" in stderr
        assert "Read-only file system" in stderr
        # Exception class name is included so operators can distinguish
        # OSError from sqlite3.DatabaseError from ValueError.
        assert "OSError" in stderr

    def test_non_oserror_audit_init_also_falls_back(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A non-OSError from AuditWriter.__init__ (e.g.,
        corrupted-chain ValueError, sqlite3.DatabaseError on a
        damaged file) used to crash serve. The fix broadens the
        catch so the same fallback discipline applies."""
        import sys as _sys

        store_path = tmp_path / "store.db"
        _seed_store(store_path)
        captured = _patch_run_stdio_capture(monkeypatch)
        monkeypatch.setenv("FAKE_URL", "postgresql+psycopg://fake/serve")

        writer_mod = _sys.modules["schemabrain.audit.writer"]

        def _explode(*args: object, **kwargs: object) -> object:
            raise ValueError("mcp_audit row 7 has corrupted chain_hash (16 bytes)")

        monkeypatch.setattr(writer_mod, "AuditWriter", _explode)

        exit_code = cli_main(
            [
                "serve",
                "--url-env",
                "FAKE_URL",
                "--store-path",
                str(store_path),
                "--no-events",
            ]
        )
        assert exit_code == 0
        assert captured["audit_writer"] is None
        stderr = capsys.readouterr().err
        assert "cannot initialise audit writer" in stderr
        assert "ValueError" in stderr
        assert "corrupted chain_hash" in stderr
