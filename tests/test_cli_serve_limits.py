"""Tests for `--statement-timeout-ms` + `--max-rows-per-result` on
`schemabrain serve`.

The two flags are operator-facing caps on `get_metric` execution:

  - `--statement-timeout-ms` is injected into the connect_args
    options string at engine-construct time. The cap fires at the
    Postgres source role and CAN'T be relaxed via URL query params.
  - `--max-rows-per-result` is enforced inside ``EngineMetricExecutor``
    after the SQL executes — a payload-size guard, not a query-cost
    guard.

These tests stub out `run_stdio` to capture the constructed engine
+ executor so we can assert what got wired without actually opening
a database connection.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from schemabrain.cli import main as cli_main
from schemabrain.core.store import SQLiteStore
from schemabrain.mcp.metric_executor import EngineMetricExecutor


def _seed_store(path: Path) -> None:
    SQLiteStore(path).close()


def _patch_run_stdio_capture(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Stub `run_stdio` and record the metric_executor + the engine's
    connect_args (which is where statement_timeout lives)."""
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
        captured["metric_executor"] = metric_executor
        # The engine is a private attribute on the executor — fine
        # to reach into for tests since both live in the same package
        # and we're asserting on the engine's bind config.
        if isinstance(metric_executor, EngineMetricExecutor):
            captured["engine"] = metric_executor._engine
            captured["max_rows"] = metric_executor._max_rows

    monkeypatch.setattr("schemabrain.cli.run_stdio", _capture)
    return captured


class TestServeStatementTimeoutMs:
    def test_omitted_uses_default_options_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without `--statement-timeout-ms` the engine's connect_args
        carries only the read-only options string — no timeout."""
        import schemabrain.cli as cli_module

        store_path = tmp_path / "store.db"
        _seed_store(store_path)
        monkeypatch.setenv("FAKE_URL", "postgresql+psycopg://fake/serve")

        captured_connect_args: dict[str, object] = {}
        real_create_engine = cli_module.sqlalchemy.create_engine

        def fake_create_engine(*args: object, **kwargs: object) -> object:
            if "connect_args" in kwargs:
                captured_connect_args.update(kwargs["connect_args"])  # type: ignore[arg-type]
            return real_create_engine(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(cli_module.sqlalchemy, "create_engine", fake_create_engine)
        captured = _patch_run_stdio_capture(monkeypatch)

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
        options = captured_connect_args.get("options", "")
        assert isinstance(options, str)
        # Read-only is the only option set by default.
        assert "default_transaction_read_only=on" in options
        assert "statement_timeout" not in options
        # And max_rows defaults to None on the executor.
        assert captured["max_rows"] is None

    def test_flag_injects_statement_timeout_into_options(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With `--statement-timeout-ms 5000` the engine's connect_args
        options string carries `-c statement_timeout=5000` alongside
        the read-only setting. Captured by patching create_engine so
        we can inspect the args without opening a real connection."""
        import schemabrain.cli as cli_module

        store_path = tmp_path / "store.db"
        _seed_store(store_path)
        monkeypatch.setenv("FAKE_URL", "postgresql+psycopg://fake/serve")

        captured_connect_args: dict[str, object] = {}
        real_create_engine = cli_module.sqlalchemy.create_engine

        def fake_create_engine(*args: object, **kwargs: object) -> object:
            if "connect_args" in kwargs:
                captured_connect_args.update(kwargs["connect_args"])  # type: ignore[arg-type]
            return real_create_engine(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(cli_module.sqlalchemy, "create_engine", fake_create_engine)
        _patch_run_stdio_capture(monkeypatch)

        cli_main(
            [
                "serve",
                "--url-env",
                "FAKE_URL",
                "--store-path",
                str(store_path),
                "--no-events",
                "--no-audit",
                "--statement-timeout-ms",
                "5000",
            ]
        )
        options = captured_connect_args.get("options", "")
        assert isinstance(options, str)
        assert "default_transaction_read_only=on" in options
        assert "statement_timeout=5000" in options


class TestServeMaxRowsPerResult:
    def test_omitted_passes_max_rows_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
        assert captured["max_rows"] is None

    def test_flag_passes_int_to_executor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
                "--max-rows-per-result",
                "250",
            ]
        )
        assert captured["max_rows"] == 250
