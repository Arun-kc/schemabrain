"""Connector contract: every backend implements `DataSource`.

Implementations connect to a specific database backend (Postgres, SQLite, ...)
and produce canonical SchemaBrain models. They are read-only with respect to
the source database.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from schemabrain.core.models import Table


@runtime_checkable
class DataSource(Protocol):
    """A read-only schema introspection surface for one source database."""

    def list_tables(self, schema: str | None = None) -> list[tuple[str, str]]:
        """Return `(schema_name, table_name)` for each user-visible table.

        If `schema` is provided, return tables in that schema only — including
        system schemas if explicitly requested. Otherwise return tables across
        every user-visible schema (system schemas are skipped).
        """
        ...

    def get_table(self, name: str, schema: str) -> Table:
        """Return a fully-built `Table` with columns and foreign keys.

        Raises `schemabrain.connectors.errors.TableNotFoundError` (a subclass
        of `ValueError`) if the table does not exist.
        """
        ...

    def estimated_row_count(self, name: str, schema: str) -> int | None:
        """Return a cheap, non-locking row-count estimate, or `None`.

        Captured at index time and cached as `tables.estimated_row_count`
        (v15). Implementations must NOT run a locking `COUNT(*)` — use a
        catalog estimate (e.g. Postgres `pg_class.reltuples`). Return
        `None` whenever no trustworthy estimate is available (the backend
        can't estimate, or the table has never been analyzed) so the
        column stays NULL rather than carrying a fabricated count.
        """
        ...

    def close(self) -> None:
        """Release any underlying resources. Must be idempotent."""
        ...
