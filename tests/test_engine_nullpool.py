"""Tests pinning `NullPool` on every Postgres engine Schema Brain
creates against a source database.

SQLAlchemy's default `QueuePool` recycles connections without
`DISCARD ALL`. Today this is unreachable (no untrusted SQL touches
these engines), but the moment any future feature reuses a
long-lived connection, pool-state escape becomes live without code
changes.

The fix is structural: pin `poolclass=NullPool` on every engine that
points at the source database. `index` and `serve` are short-running
per-process, so connection reuse buys us nothing observable; the cost
is a few milliseconds per profile call.

These tests inspect the engine's `.pool` attribute without connecting,
so they run as unit tests and don't need a real Postgres.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.pool import NullPool

from schemabrain.connectors.postgres import PostgresDataSource
from schemabrain.profiler.postgres import PostgresProfiler

# A URL that SQLAlchemy will parse cleanly without trying to connect.
# `create_engine` is lazy — the connection isn't opened until the first
# `connect()` call, so we can inspect the pool class from a fresh engine.
_DUMMY_URL = "postgresql+psycopg://user:pw@localhost:5432/dummy"


class TestPostgresDataSourcePool:
    def test_engine_uses_nullpool(self) -> None:
        source = PostgresDataSource(_DUMMY_URL)
        try:
            engine = source._require_engine()
            assert isinstance(engine.pool, NullPool), (
                f"PostgresDataSource must use NullPool to prevent pool-state "
                f"escape (NullPool defense-in-depth contract); got {type(engine.pool).__name__}"
            )
        finally:
            source.close()


class TestPostgresProfilerPool:
    def test_engine_uses_nullpool(self) -> None:
        profiler = PostgresProfiler(_DUMMY_URL)
        try:
            engine = profiler._require_engine()
            assert isinstance(engine.pool, NullPool), (
                f"PostgresProfiler must use NullPool to prevent pool-state "
                f"escape (NullPool defense-in-depth contract); got {type(engine.pool).__name__}"
            )
        finally:
            profiler.close()


class TestMineQueriesEngine:
    """The `mine-queries` CLI subcommand creates its own engine inline.
    Patch `sqlalchemy.create_engine` to capture the kwargs and assert
    `NullPool` was passed.
    """

    def test_cli_passes_nullpool(
        self,
        tmp_path: object,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Stub the pipeline so we don't need a real Postgres or store.
        from schemabrain.mining.pipeline import MineReport

        fake_mine = MagicMock(
            return_value=MineReport(
                statements_read=0,
                statements_used=0,
                rows_written=0,
                skipped_unavailable=True,
            )
        )
        monkeypatch.setattr("schemabrain.cli.mine_queries", fake_mine)

        captured: dict[str, object] = {}

        def _capture_create_engine(url: str, **kwargs: object) -> MagicMock:
            captured["url"] = url
            captured["kwargs"] = kwargs
            return MagicMock()  # engine.dispose() is called in finally

        with patch("sqlalchemy.create_engine", _capture_create_engine):
            from schemabrain.cli import main

            store_path = str(tmp_path / "schemabrain.db")  # type: ignore[attr-defined]
            main(
                [
                    "mine-queries",
                    "--source",
                    _DUMMY_URL,
                    "--store-path",
                    store_path,
                ]
            )

        assert captured.get("kwargs", {}).get("poolclass") is NullPool, (
            "mine-queries CLI must build its engine with poolclass=NullPool "
            "(NullPool defense-in-depth contract)"
        )
