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
import struct
import time
from pathlib import Path

from schemabrain.core.description import ColumnDescription
from schemabrain.core.embedding import ColumnEmbedding
from schemabrain.core.models import Column, ForeignKey, IncomingForeignKey, Table

# Bumped to "2" in Slice 4-B when `column_embeddings` was added. Older
# stores raise SchemaVersionMismatchError; pre-alpha users re-create.
_SCHEMA_VERSION = "2"
_MEMORY_PATH = ":memory:"

# float32 = 4 bytes per dim. BLOBs are little-endian packed via struct.
_FLOAT32_FORMAT = "<f"
_FLOAT32_BYTES = 4


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
    """
    CREATE TABLE IF NOT EXISTS column_fingerprints (
        schema_name TEXT NOT NULL,
        table_name TEXT NOT NULL,
        column_name TEXT NOT NULL,
        source_connection_id TEXT NOT NULL,
        structural_hash TEXT NOT NULL,
        semantic_hash TEXT NOT NULL,
        fingerprinted_at INTEGER NOT NULL,
        PRIMARY KEY (schema_name, table_name, column_name, source_connection_id),
        FOREIGN KEY (schema_name, table_name, source_connection_id)
            REFERENCES tables (schema_name, name, source_connection_id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS column_descriptions (
        schema_name TEXT NOT NULL,
        table_name TEXT NOT NULL,
        column_name TEXT NOT NULL,
        source_connection_id TEXT NOT NULL,
        description TEXT NOT NULL,
        model TEXT NOT NULL,
        prompt_version TEXT NOT NULL,
        input_tokens INTEGER NOT NULL,
        cached_input_tokens INTEGER NOT NULL,
        output_tokens INTEGER NOT NULL,
        cost_usd REAL NOT NULL,
        generated_at INTEGER NOT NULL,
        PRIMARY KEY (schema_name, table_name, column_name, source_connection_id),
        FOREIGN KEY (schema_name, table_name, source_connection_id)
            REFERENCES tables (schema_name, name, source_connection_id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS column_embeddings (
        schema_name TEXT NOT NULL,
        table_name TEXT NOT NULL,
        column_name TEXT NOT NULL,
        source_connection_id TEXT NOT NULL,
        model TEXT NOT NULL,
        dimension INTEGER NOT NULL,
        vector BLOB NOT NULL,
        embedded_at INTEGER NOT NULL,
        PRIMARY KEY (schema_name, table_name, column_name, source_connection_id),
        FOREIGN KEY (schema_name, table_name, source_connection_id)
            REFERENCES tables (schema_name, name, source_connection_id)
            ON DELETE CASCADE
    )
    """,
)


def _pack_vector(vector: tuple[float, ...]) -> bytes:
    """Serialize a float vector to a little-endian float32 BLOB.

    Float32 is the native dtype of fastembed's ONNX output, so this is
    the right precision to persist. Higher precision would bloat the
    store without changing retrieval results (cosine similarity in
    float32 is more than sufficient for 384-dim sentence embeddings).
    """
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack_vector(blob: bytes, dimension: int) -> tuple[float, ...]:
    """Inverse of `_pack_vector`. Validates blob length matches dimension.

    Defensive: a corrupt or truncated BLOB would otherwise produce a
    silently wrong-length vector, which would then explode much later in
    a cosine similarity computation with a confusing error.
    """
    expected_bytes = dimension * _FLOAT32_BYTES
    if len(blob) != expected_bytes:
        raise ValueError(
            f"vector BLOB length {len(blob)} does not match expected "
            f"{expected_bytes} bytes for dimension {dimension}"
        )
    return struct.unpack(f"<{dimension}f", blob)


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

    def delete_table(self, schema_name: str, name: str, *, source_connection_id: str) -> None:
        """Idempotent: delete the table row and cascade to all child
        tables — `columns`, `foreign_keys`, `column_fingerprints`,
        `column_descriptions`, and `column_embeddings`. No-op if the
        row is absent.
        """
        conn = self._require_conn()
        with conn:
            conn.execute(
                "DELETE FROM tables WHERE schema_name = ? AND name = ? "
                "AND source_connection_id = ?",
                (schema_name, name, source_connection_id),
            )

    def get_table_fingerprints(
        self, schema_name: str, name: str, *, source_connection_id: str
    ) -> dict[str, tuple[str, str]]:
        """Return `{column_name: (structural_hash, semantic_hash)}` for the
        table. Empty dict if no fingerprints have been written yet.
        """
        # Pure read — no `with conn:` transaction wrap. Writers need the
        # transaction guard; readers don't, and the wrap reads as
        # misleading ("what mutation does this need to protect?").
        conn = self._require_conn()
        rows = conn.execute(
            "SELECT column_name, structural_hash, semantic_hash "
            "FROM column_fingerprints "
            "WHERE schema_name = ? AND table_name = ? "
            "AND source_connection_id = ?",
            (schema_name, name, source_connection_id),
        ).fetchall()
        return {row["column_name"]: (row["structural_hash"], row["semantic_hash"]) for row in rows}

    def write_table_fingerprints(
        self,
        schema_name: str,
        name: str,
        *,
        source_connection_id: str,
        fingerprints: dict[str, tuple[str, str]],
    ) -> None:
        """Replace all fingerprints for the table atomically.

        DELETE existing rows, INSERT every entry from `fingerprints`. The
        caller is responsible for having written the parent `tables` row
        first (via `write_table`); without it the FK on
        `column_fingerprints` will reject the inserts.
        """
        conn = self._require_conn()
        now = int(time.time())
        with conn:
            conn.execute(
                "DELETE FROM column_fingerprints "
                "WHERE schema_name = ? AND table_name = ? "
                "AND source_connection_id = ?",
                (schema_name, name, source_connection_id),
            )
            conn.executemany(
                "INSERT INTO column_fingerprints "
                "(schema_name, table_name, column_name, source_connection_id, "
                "structural_hash, semantic_hash, fingerprinted_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        schema_name,
                        name,
                        col_name,
                        source_connection_id,
                        structural,
                        semantic,
                        now,
                    )
                    for col_name, (structural, semantic) in fingerprints.items()
                ],
            )

    def get_table_descriptions(
        self, schema_name: str, name: str, *, source_connection_id: str
    ) -> dict[str, ColumnDescription]:
        """Return `{column_name: ColumnDescription}` for the table.

        Empty dict if no descriptions have been generated yet.
        """
        conn = self._require_conn()
        rows = conn.execute(
            "SELECT column_name, description, model, prompt_version, "
            "input_tokens, cached_input_tokens, output_tokens, cost_usd "
            "FROM column_descriptions "
            "WHERE schema_name = ? AND table_name = ? "
            "AND source_connection_id = ?",
            (schema_name, name, source_connection_id),
        ).fetchall()
        return {
            row["column_name"]: ColumnDescription(
                text=row["description"],
                model=row["model"],
                prompt_version=row["prompt_version"],
                input_tokens=row["input_tokens"],
                cached_input_tokens=row["cached_input_tokens"],
                output_tokens=row["output_tokens"],
                cost_usd=row["cost_usd"],
            )
            for row in rows
        }

    def write_table_descriptions(
        self,
        schema_name: str,
        name: str,
        *,
        source_connection_id: str,
        descriptions: dict[str, ColumnDescription],
    ) -> None:
        """Replace all descriptions for the table atomically.

        Caller must have written the parent `tables` row first; otherwise
        the FK on `column_descriptions` will reject the inserts.
        """
        conn = self._require_conn()
        now = int(time.time())
        with conn:
            conn.execute(
                "DELETE FROM column_descriptions "
                "WHERE schema_name = ? AND table_name = ? "
                "AND source_connection_id = ?",
                (schema_name, name, source_connection_id),
            )
            conn.executemany(
                "INSERT INTO column_descriptions "
                "(schema_name, table_name, column_name, source_connection_id, "
                "description, model, prompt_version, input_tokens, "
                "cached_input_tokens, output_tokens, cost_usd, generated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        schema_name,
                        name,
                        col_name,
                        source_connection_id,
                        desc.text,
                        desc.model,
                        desc.prompt_version,
                        desc.input_tokens,
                        desc.cached_input_tokens,
                        desc.output_tokens,
                        desc.cost_usd,
                        now,
                    )
                    for col_name, desc in descriptions.items()
                ],
            )

    def get_foreign_keys_targeting(
        self,
        target_schema: str,
        target_table: str,
        target_column: str,
        *,
        source_connection_id: str,
    ) -> list[IncomingForeignKey]:
        """Return all FKs that reference `target_schema.target_table.target_column`.

        Used by `describe_column` to surface back-references — "which
        other tables join into me here?". Match is on
        `(target_schema, target_table)` plus membership of
        `target_column` in the FK's `target_columns` list, so composite
        FKs match correctly when the column appears in any position.

        A composite FK targeting `(org_id, user_id)` is returned when
        querying for either column — callers see the full FK row and
        can inspect `target_columns` to understand the full key shape.

        Filtering on the JSON `target_columns` happens in Python, not
        SQL, since SQLite's JSON1 extension isn't a guaranteed dep —
        the per-source FK row count is small (~hundreds even on a
        large schema), so a Python filter is cheaper than carrying
        another extension dependency.
        """
        conn = self._require_conn()
        rows = conn.execute(
            "SELECT name, source_schema, source_table, source_columns, target_columns "
            "FROM foreign_keys "
            "WHERE source_connection_id = ? AND target_schema = ? AND target_table = ? "
            "ORDER BY source_schema, source_table, name",
            (source_connection_id, target_schema, target_table),
        ).fetchall()
        results: list[IncomingForeignKey] = []
        for row in rows:
            target_cols = tuple(json.loads(row["target_columns"]))
            if target_column not in target_cols:
                continue
            results.append(
                IncomingForeignKey(
                    name=row["name"],
                    source_qualified_name=f"{row['source_schema']}.{row['source_table']}",
                    source_columns=tuple(json.loads(row["source_columns"])),
                    target_columns=target_cols,
                )
            )
        return results

    def list_all_foreign_keys(
        self, *, source_connection_id: str
    ) -> list[tuple[str, str, ForeignKey]]:
        """Return every FK in the source as `(source_schema, source_table, fk)`.

        Bulk reader for graph-building consumers (`suggest_joins`) that
        would otherwise pay N round-trips iterating `list_tables` +
        `get_table`. Order is deterministic: `(source_schema,
        source_table, fk_name)` ascending so BFS tiebreaks reproduce.
        """
        conn = self._require_conn()
        rows = conn.execute(
            "SELECT source_schema, source_table, name, source_columns, "
            "target_schema, target_table, target_columns "
            "FROM foreign_keys WHERE source_connection_id = ? "
            "ORDER BY source_schema, source_table, name",
            (source_connection_id,),
        ).fetchall()
        return [
            (
                row["source_schema"],
                row["source_table"],
                ForeignKey(
                    name=row["name"],
                    source_columns=tuple(json.loads(row["source_columns"])),
                    target_schema=row["target_schema"],
                    target_table=row["target_table"],
                    target_columns=tuple(json.loads(row["target_columns"])),
                ),
            )
            for row in rows
        ]

    def get_table_embeddings(
        self, schema_name: str, name: str, *, source_connection_id: str
    ) -> dict[str, ColumnEmbedding]:
        """Return `{column_name: ColumnEmbedding}` for the table.

        Empty dict if no embeddings have been stored yet.
        """
        conn = self._require_conn()
        rows = conn.execute(
            "SELECT column_name, model, dimension, vector "
            "FROM column_embeddings "
            "WHERE schema_name = ? AND table_name = ? "
            "AND source_connection_id = ?",
            (schema_name, name, source_connection_id),
        ).fetchall()
        return {
            row["column_name"]: ColumnEmbedding(
                vector=_unpack_vector(row["vector"], row["dimension"]),
                model=row["model"],
                dimension=row["dimension"],
            )
            for row in rows
        }

    def write_table_embeddings(
        self,
        schema_name: str,
        name: str,
        *,
        source_connection_id: str,
        embeddings: dict[str, ColumnEmbedding],
    ) -> None:
        """Replace all embeddings for the table atomically.

        Caller must have written the parent `tables` row first; otherwise
        the FK on `column_embeddings` will reject the inserts.
        """
        conn = self._require_conn()
        now = int(time.time())
        with conn:
            conn.execute(
                "DELETE FROM column_embeddings "
                "WHERE schema_name = ? AND table_name = ? "
                "AND source_connection_id = ?",
                (schema_name, name, source_connection_id),
            )
            conn.executemany(
                "INSERT INTO column_embeddings "
                "(schema_name, table_name, column_name, source_connection_id, "
                "model, dimension, vector, embedded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        schema_name,
                        name,
                        col_name,
                        source_connection_id,
                        emb.model,
                        emb.dimension,
                        _pack_vector(emb.vector),
                        now,
                    )
                    for col_name, emb in embeddings.items()
                ],
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
                f"expected {_SCHEMA_VERSION!r}. Schema Brain is pre-alpha and "
                f"does not yet provide migrations — delete or move the store "
                f"file (path passed to SQLiteStore) and re-run `schemabrain "
                f"index` to rebuild from scratch."
            )

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("SQLiteStore is closed")
        return self._conn
