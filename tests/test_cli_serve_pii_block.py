"""CLI tests for `schemabrain serve --pii-block CATEGORIES`."""

from __future__ import annotations

from pathlib import Path

import pytest

from schemabrain.cli import main as cli_main
from schemabrain.core.store import SQLiteStore


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
        audit_writer=None,
        pii_block=frozenset(),
        tracer=None,
    ) -> None:
        captured["pii_block"] = pii_block

    monkeypatch.setattr("schemabrain.cli.run_stdio", _capture)
    return captured


class TestPiiBlockArgparse:
    def test_default_is_empty_set(
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
                "--no-events",
                "--no-audit",
            ]
        )
        assert exit_code == 0
        assert captured["pii_block"] == frozenset()

    def test_valid_categories_parsed(
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
                "--pii-block",
                "contact,health",
            ]
        )
        assert captured["pii_block"] == frozenset({"contact", "health"})

    def test_whitespace_in_csv_tolerated(
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
                "--pii-block",
                " contact , health ",
            ]
        )
        assert captured["pii_block"] == frozenset({"contact", "health"})

    def test_unknown_category_aborts_with_listing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        store_path = tmp_path / "store.db"
        _seed_store(store_path)
        _patch_run_stdio_capture(monkeypatch)
        monkeypatch.setenv("FAKE_URL", "postgresql+psycopg://fake/serve")
        exit_code = cli_main(
            [
                "serve",
                "--url-env",
                "FAKE_URL",
                "--store-path",
                str(store_path),
                "--no-events",
                "--no-audit",
                "--pii-block",
                "contact,nonsense",
            ]
        )
        assert exit_code == 2
        stderr = capsys.readouterr().err
        assert "nonsense" in stderr
        # The valid-list mention helps the operator self-correct.
        assert "contact" in stderr  # part of valid set
        assert "Valid categories" in stderr


class TestPiiBlockNoAuditCoexistenceWarning:
    def test_both_set_emits_warning(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        store_path = tmp_path / "store.db"
        _seed_store(store_path)
        _patch_run_stdio_capture(monkeypatch)
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
                "--pii-block",
                "contact",
            ]
        )
        stderr = capsys.readouterr().err
        assert "--pii-block active with --no-audit" in stderr

    def test_pii_block_with_audit_no_warning(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        store_path = tmp_path / "store.db"
        _seed_store(store_path)
        _patch_run_stdio_capture(monkeypatch)
        monkeypatch.setenv("FAKE_URL", "postgresql+psycopg://fake/serve")
        cli_main(
            [
                "serve",
                "--url-env",
                "FAKE_URL",
                "--store-path",
                str(store_path),
                "--no-events",
                # --no-audit absent → audit on; warning should NOT fire.
                "--pii-block",
                "contact",
            ]
        )
        stderr = capsys.readouterr().err
        assert "--pii-block active with --no-audit" not in stderr
