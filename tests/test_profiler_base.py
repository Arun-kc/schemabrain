"""Tests for the Profiler Protocol contract."""

from __future__ import annotations

from schemabrain.profiler.base import Profiler
from schemabrain.profiler.postgres import PostgresProfiler


class TestProfilerProtocol:
    def test_postgres_profiler_satisfies_protocol(self) -> None:
        # `runtime_checkable` lets us isinstance-check duck typing.
        # The shipped PostgresProfiler must satisfy the Protocol so any
        # caller programming against `Profiler` can accept it.
        # We only check the type; constructing the profiler would open a
        # SQLAlchemy engine we don't need here.
        assert issubclass(PostgresProfiler, Profiler)

    def test_minimal_implementation_satisfies_protocol(self) -> None:
        class StubProfiler:
            def profile_table(self, table):
                return {}

        assert isinstance(StubProfiler(), Profiler)

    def test_class_without_profile_table_is_not_a_profiler(self) -> None:
        class NotAProfiler:
            def something_else(self) -> None:
                pass

        assert not isinstance(NotAProfiler(), Profiler)
