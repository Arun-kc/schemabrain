"""Protocol contract for profiler implementations.

Mirrors the `DataSource` Protocol pattern in `connectors/base.py`. The
indexer depends on this Protocol, not on the concrete `PostgresProfiler`,
so test fakes can plug in without touching SQLAlchemy or psycopg.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from schemabrain.core.models import Table
from schemabrain.profiler.stats import ColumnStats


@runtime_checkable
class Profiler(Protocol):
    """Compute per-column profiling statistics for a table."""

    def profile_table(self, table: Table) -> dict[str, ColumnStats]:
        """Return one `ColumnStats` per column of `table`, keyed by column name."""
        ...
