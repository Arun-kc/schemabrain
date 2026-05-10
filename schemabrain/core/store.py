"""Local SQLite store for Schema Brain models.

Single-file persistence for `Table`, `Column`, `ForeignKey`. Idempotent
upserts keyed on `(schema_name, name, source_connection_id)`. The store
holds one connection per instance and must be closed when done.

Concurrency: the underlying sqlite3 connection is opened with
`check_same_thread=False` so it can be used from any thread (e.g. an
async event loop that may resume on different threads). However, sqlite3
does NOT serialise concurrent writes — callers using this store from
multiple threads MUST serialise writes themselves with a lock.

Backups: WAL mode is enabled for file-backed stores. Backups must copy
the `*.db`, `*.db-wal`, and `*.db-shm` files atomically (use
`sqlite3 .backup` or `VACUUM INTO`). Avoid placing the DB inside a
cloud-sync directory (Dropbox, iCloud Drive) that may sync these files
independently — that can corrupt the DB.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from schemabrain.core.models import Column, ForeignKey, Table

_SCHEMA_VERSION = "1"
_MEMORY_PATH = ":memory:"


class SchemaVersionMismatchError(RuntimeError):
    """Raised when an existing store file's schema version does not match
    the version this code expects.

    Indicates the store was created (or last migrated) by a different
    Schema Brain release. Re-create the store, or implement a migration.
    """


_DDL_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS schemabrain_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tables (
        schema_name TEXT NOT NULL,
        name TEXT NOT NULL,
        source_connection_id TEXT NOT NULL,
        indexed_at INTEGER NOT NULL,
        PRIMARY KEY (schema_name, name, source_connection_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS columns (
        schema_name TEXT NOT NULL,
        table_name TEXT NOT NULL,
        name TEXT NOT NULL,
        source_connection_id TEXT NOT NULL,
        data_type TEXT NOT NULL,
        nullable INTEGER NOT NULL,
        ordinal_position INTEGER NOT NULL,
        default_expr TEXT,
        is_primary_key INTEGER NOT NULL,
        PRIMARY KEY (schema_name, table_name, name, source_connection_id),
        FOREIGN KEY (schema_name, table_name, source_connection_id)
            REFERENCES tables (schema_name, name, source_connection_id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS foreign_keys (
        source_schema TEXT NOT NULL,
        source_table TEXT NOT NULL,
        name TEXT NOT NULL,
        source_connection_id TEXT NOT NULL,
        source_columns TEXT NOT NULL,
        target_schema TEXT NOT NULL,
        target_table TEXT NOT NULL,
        target_columns TEXT NOT NULL,
        PRIMARY KEY (source_schema, source_table, name, source_connection_id),
        FOREIGN KEY (source_schema, source_table, source_connection_id)
            REFERENCES tables (schema_name, name, source_connection_id)
            ON DELETE CASCADE
    )
    """,
)


class SQLiteStore:
    """File-backed SQLite store for indexed schema metadata.

    Multiple sources can coexist in one store, distinguished by their
    `source_connection_id` (a stable identifier the caller derives from
    the source connection URL).
    """

    def __init__(self, path: Path | str) -> None:
        is_memory = str(path) == _MEMORY_PATH
        if not is_memory:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        connect_target = _MEMORY_PATH if is_memory else str(path)
        self._conn: sqlite3.Connection | None = sqlite3.connect(
            connect_target, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if not is_memory:
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._init_schema()

    def __enter__(self) -> SQLiteStore:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def write_table(self, table: Table, *, source_connection_id: str) -> None:
        """Idempotent upsert of one Table (and its columns + FKs).

        Replaces all existing rows for `(schema, name, source_connection_id)`.
        Atomic: if any insert fails, the entire write rolls back.
        """
        conn = self._require_conn()
        with conn:
            conn.execute(
                "DELETE FROM tables WHERE schema_name = ? AND name = ? "
                "AND source_connection_id = ?",
                (table.schema_name, table.name, source_connection_id),
            )
            conn.execute(
                "INSERT INTO tables "
                "(schema_name, name, source_connection_id, indexed_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    table.schema_name,
                    table.name,
                    source_connection_id,
                    int(time.time()),
                ),
            )
            conn.executemany(
                "INSERT INTO columns "
                "(schema_name, table_name, name, source_connection_id, "
                "data_type, nullable, ordinal_position, default_expr, is_primary_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        col.schema_name,
                        col.table_name,
                        col.name,
                        source_connection_id,
                        col.data_type,
                        int(col.nullable),
                        col.ordinal_position,
                        col.default,
                        int(col.is_primary_key),
                    )
                    for col in table.columns
                ],
            )
            conn.executemany(
                "INSERT INTO foreign_keys "
                "(source_schema, source_table, name, source_connection_id, "
                "source_columns, target_schema, target_table, target_columns) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        table.schema_name,
                        table.name,
                        fk.name,
                        source_connection_id,
                        json.dumps(list(fk.source_columns)),
                        fk.target_schema,
                        fk.target_table,
                        json.dumps(list(fk.target_columns)),
                    )
                    for fk in table.foreign_keys
                ],
            )

    def get_table(self, schema_name: str, name: str, *, source_connection_id: str) -> Table | None:
        conn = self._require_conn()
        with conn:
            present = conn.execute(
                "SELECT 1 FROM tables "
                "WHERE schema_name = ? AND name = ? AND source_connection_id = ?",
                (schema_name, name, source_connection_id),
            ).fetchone()
            if present is None:
                return None
            col_rows = conn.execute(
                "SELECT name, data_type, nullable, ordinal_position, default_expr, is_primary_key "
                "FROM columns "
                "WHERE schema_name = ? AND table_name = ? AND source_connection_id = ? "
                "ORDER BY ordinal_position",
                (schema_name, name, source_connection_id),
            ).fetchall()
            fk_rows = conn.execute(
                "SELECT name, source_columns, target_schema, target_table, target_columns "
                "FROM foreign_keys "
                "WHERE source_schema = ? AND source_table = ? AND source_connection_id = ? "
                "ORDER BY name",
                (schema_name, name, source_connection_id),
            ).fetchall()
        columns = tuple(
            Column(
                name=row["name"],
                table_name=name,
                schema_name=schema_name,
                data_type=row["data_type"],
                nullable=bool(row["nullable"]),
                ordinal_position=row["ordinal_position"],
                default=row["default_expr"],
                is_primary_key=bool(row["is_primary_key"]),
            )
            for row in col_rows
        )
        foreign_keys = tuple(
            ForeignKey(
                name=row["name"],
                source_columns=tuple(json.loads(row["source_columns"])),
                target_schema=row["target_schema"],
                target_table=row["target_table"],
                target_columns=tuple(json.loads(row["target_columns"])),
            )
            for row in fk_rows
        )
        return Table(
            name=name,
            schema_name=schema_name,
            columns=columns,
            foreign_keys=foreign_keys,
        )

    def list_tables(self, *, source_connection_id: str | None = None) -> list[tuple[str, str]]:
        conn = self._require_conn()
        if source_connection_id is None:
            rows = conn.execute(
                "SELECT schema_name, name FROM tables ORDER BY schema_name, name"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT schema_name, name FROM tables "
                "WHERE source_connection_id = ? ORDER BY schema_name, name",
                (source_connection_id,),
            ).fetchall()
        return [(row["schema_name"], row["name"]) for row in rows]

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _init_schema(self) -> None:
        conn = self._require_conn()
        with conn:
            for stmt in _DDL_STATEMENTS:
                conn.execute(stmt)
            conn.execute(
                "INSERT OR IGNORE INTO schemabrain_meta (key, value) VALUES (?, ?)",
                ("schema_version", _SCHEMA_VERSION),
            )
        stored = conn.execute(
            "SELECT value FROM schemabrain_meta WHERE key = ?", ("schema_version",)
        ).fetchone()
        if stored is not None and stored["value"] != _SCHEMA_VERSION:
            raise SchemaVersionMismatchError(
                f"Store schema version {stored['value']!r} does not match "
                f"expected {_SCHEMA_VERSION!r}. Re-create the store or migrate."
            )

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("SQLiteStore is closed")
        return self._conn
