"""Unit tests for the `pg_stat_statements` reader.

The failure-mode contract is load-bearing: when the view can't be
read because of source-side configuration (extension missing,
permission denied), the reader converts the SQLAlchemy
`ProgrammingError` to `PgStatStatementsUnavailable` so the mining
pipeline can soft-skip. Real failures — connection loss
(`OperationalError`), unexpected DB errors — must propagate
unchanged so a retry-capable caller sees them as themselves rather
than as a misleading "fix your extension" signal.

These unit tests fake the engine + raise specific SQLAlchemy errors
to exercise both paths. Integration coverage against a real
`pg_stat_statements`-enabled Postgres lives in
`tests/test_mining_integration.py`.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import DatabaseError, OperationalError, ProgrammingError

from schemabrain.mining.pg_stat_statements import (
    PgStatRow,
    PgStatStatementsUnavailable,
    fetch_statements,
)


def _engine_raising(exc: Exception) -> Any:
    """Build a fake engine whose `connect().__enter__().execute()` raises `exc`."""
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    conn.execute.side_effect = exc
    engine = MagicMock()
    engine.connect.return_value = conn
    return engine


def _engine_returning(rows: list[Any]) -> Any:
    """Build a fake engine whose query returns the given row sequence."""
    result = MagicMock()
    result.fetchall.return_value = rows
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    conn.execute.return_value = result
    engine = MagicMock()
    engine.connect.return_value = conn
    return engine


class TestSoftSkipConversion:
    """`ProgrammingError` — Postgres SQLSTATE 42P01 (undefined_table),
    42501 (insufficient_privilege), 42883 (undefined_function) — is
    the operator-config soft-skip case. The reader converts it to
    `PgStatStatementsUnavailable`.
    """

    def test_programming_error_converts_to_unavailable(self) -> None:
        engine = _engine_raising(
            ProgrammingError('relation "pg_stat_statements" does not exist', {}, Exception())
        )
        with pytest.raises(PgStatStatementsUnavailable):
            fetch_statements(engine)

    def test_unavailable_message_carries_the_underlying_cause(self) -> None:
        engine = _engine_raising(
            ProgrammingError("permission denied for view pg_stat_statements", {}, Exception())
        )
        with pytest.raises(PgStatStatementsUnavailable) as exc_info:
            fetch_statements(engine)
        assert "permission denied" in str(exc_info.value)


class TestRealFailuresPropagate:
    """Connection failures, unexpected DB errors, and other
    SQLAlchemy errors must NOT be silenced into the soft-skip path —
    they signal real problems the operator can't fix by reconfiguring
    the extension. Per the v0.5 review.
    """

    def test_operational_error_propagates_unchanged(self) -> None:
        engine = _engine_raising(OperationalError("connection lost", {}, Exception()))
        with pytest.raises(OperationalError):
            fetch_statements(engine)

    def test_database_error_propagates_unchanged(self) -> None:
        engine = _engine_raising(DatabaseError("server error", {}, Exception()))
        with pytest.raises(DatabaseError):
            fetch_statements(engine)


class TestHappyPath:
    def test_returns_mapped_pgstatrows(self) -> None:
        row1 = MagicMock(query="SELECT 1 FROM t", calls=10)
        row2 = MagicMock(query="SELECT 2 FROM t", calls=5)
        engine = _engine_returning([row1, row2])
        result = fetch_statements(engine)
        assert result == [
            PgStatRow(query="SELECT 1 FROM t", calls=10),
            PgStatRow(query="SELECT 2 FROM t", calls=5),
        ]


class TestSizeCap:
    """Per the v0.5 security review: rows whose `query` exceeds
    `_MAX_SQL_BYTES` are skipped at this boundary. Bounds peak memory
    under adversarial / misconfigured sources where
    `pg_stat_statements.track_activity_query_size` is raised above
    the default 1024 bytes.
    """

    def test_oversized_query_is_dropped(self) -> None:
        too_big = "x" * 200_000  # 200 KB; above the 100 KB cap
        small = "SELECT 1 FROM t"
        rows = [
            MagicMock(query=small, calls=1),
            MagicMock(query=too_big, calls=999),
        ]
        engine = _engine_returning(rows)
        result = fetch_statements(engine)
        # Only the small one survives; the oversized row is silently
        # dropped at the reader boundary.
        assert len(result) == 1
        assert result[0].query == small

    def test_empty_query_is_dropped(self) -> None:
        # Defensive: the SQL itself has `WHERE query IS NOT NULL` but
        # the Python-side filter pins the contract regardless of
        # whether a future operator weakens the SQL filter.
        rows = [
            MagicMock(query="", calls=5),
            MagicMock(query="SELECT 1", calls=3),
        ]
        engine = _engine_returning(rows)
        result = fetch_statements(engine)
        assert len(result) == 1
        assert result[0].query == "SELECT 1"
