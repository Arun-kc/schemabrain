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

import contextlib
import json
import math
import sqlite3
import struct
import time
from pathlib import Path

import numpy as np

from schemabrain.core.description import ColumnDescription
from schemabrain.core.embedding import ColumnEmbedding
from schemabrain.core.models import Column, ForeignKey, IncomingForeignKey, Table

# Bump the schema version when the on-disk DDL set changes.
# History:
#   "2" → added `column_embeddings`.
#   "3" → added `cost_ledger` for cross-process spend persistence.
#   "4" → added `idx_col_emb_src` covering index on `column_embeddings`.
#   "5" → added `idx_col_desc_src` covering index on `column_descriptions`
#         so `get_table_descriptions(schema, table, source_connection_id)`
#         is a point seek instead of a partial-PK range scan. The PK
#         orders columns as `(schema, table, column, source_connection_id)`
#         — `source_connection_id` is the 4th column, so a `WHERE schema=?
#         AND table=? AND source_connection_id=?` predicate misses the
#         PK tail. The sibling index reorders so the predicate is
#         a true point seek.
# Older stores raise SchemaVersionMismatchError; pre-alpha users re-create.
_SCHEMA_VERSION = "5"
_MEMORY_PATH = ":memory:"

# 1 USD = 1_000_000 micros. Storing the ledger as INTEGER micros avoids
# float accumulation drift over many small adds (1000 x $0.001 must
# equal exactly $1.00). Sub-micro precision (<$0.000001) is rounded
# away at the add boundary — well below any per-call LLM cost today.
_MICROS_PER_USD = 1_000_000

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
    # Cost ledger: cumulative USD spent per `source_connection_id`.
    # Lives outside the `tables` FK graph because spend persists across
    # delete_table operations — a user who deletes a table and re-runs
    # `index` has STILL paid for the prior enrichment.
    # `spent_usd_micros` is an INTEGER (1 USD = 1_000_000 micros); see
    # `_MICROS_PER_USD` for rationale.
    """
    CREATE TABLE IF NOT EXISTS cost_ledger (
        source_connection_id TEXT PRIMARY KEY,
        spent_usd_micros INTEGER NOT NULL DEFAULT 0,
        last_updated INTEGER NOT NULL
    )
    """,
    # Covering index for `search_embeddings_topk`. The PRIMARY KEY on
    # `column_embeddings` is `(schema_name, table_name, column_name,
    # source_connection_id)` — `source_connection_id` is the 4th
    # column, so a `WHERE source_connection_id = ?` predicate cannot
    # use the PK index. This index puts `source_connection_id` first
    # and includes the ORDER BY columns, so the bulk-fetch SELECT
    # becomes a single B-tree seek + in-index range scan with no
    # filesort. Without this, retrieval is O(M) scan + O(M log M)
    # sort, and the < 100ms-at-10k-columns retrieval target is at risk.
    """
    CREATE INDEX IF NOT EXISTS idx_col_emb_src
        ON column_embeddings
            (source_connection_id, schema_name, table_name, column_name)
    """,
    # Sibling covering index for `get_table_descriptions`. Same
    # structural issue as `column_embeddings`: the PK leads with
    # `(schema, table, column)` so a `WHERE schema=? AND table=? AND
    # source_connection_id=?` query — which is what
    # `find_relevant_tables` issues for each of its top-`limit` hits —
    # can only prefix-match on `(schema, table)` and then scans every
    # column row for the table, filtering on `source_connection_id`.
    # At 100 cols/table x 10 hits per call that's a thousand
    # superfluous comparisons per `find_relevant_tables` invocation.
    # This index reorders so the predicate becomes a clean point seek
    # over `(source_connection_id, schema, table)`.
    """
    CREATE INDEX IF NOT EXISTS idx_col_desc_src
        ON column_descriptions
            (source_connection_id, schema_name, table_name, column_name)
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

    def __init__(self, path: Path | str, *, writer_lock: bool = False) -> None:
        """Open a SQLite store at `path`.

        `writer_lock=True` is the opt-in flag for callers that need
        cross-process write serialisation. Today, only the cost-ledger
        write path (`add_spend_usd`) honours this flag with
        `BEGIN IMMEDIATE`; the other write methods rely on Python's
        sqlite3 module GIL-serialised writes through a single
        connection. The contention test in
        `tests/test_core_store_protocol.py` verifies the underlying
        SQLite mechanism.

        Connection-level PRAGMA hardening tunes the store for the
        realistic Schema Brain workload. The WAL group
        (`journal_mode=WAL` + `synchronous=NORMAL`) only applies to
        file-backed stores; in-memory stores have no fsync to schedule
        and SQLite forces `journal_mode=MEMORY` regardless, so we
        leave `synchronous` at SQLite's in-memory default there:

          - `foreign_keys=ON`     — FK cascades drive `delete_table`
                                    (universal)
          - `journal_mode=WAL`    — concurrent readers + single writer
                                    (file-backed only)
          - `synchronous=NORMAL`  — WAL-recommended pairing; FULL is
                                    the default and overkill on WAL
                                    (file-backed only)
          - `temp_store=MEMORY`   — faster sorts/joins for retrieval
                                    (universal)
          - `mmap_size=256 MB`    — cache speedup for 10k-col stores;
                                    SQLite may cap below this on a
                                    platform-specific basis
                                    (universal; no-op on memory)
          - `busy_timeout=5000`   — graceful wait when another
                                    connection holds BEGIN IMMEDIATE
                                    (universal)
        """
        self.writer_lock = writer_lock
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
            # WAL group: only meaningful when there's a real file to
            # journal against. In-memory stores get SQLite's default
            # synchronous; setting it would be a misleading invariant.
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.execute("PRAGMA temp_store = MEMORY")
        self._conn.execute("PRAGMA mmap_size = 268435456")
        self._conn.execute("PRAGMA busy_timeout = 5000")
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

        Rejects any embedding whose vector contains a non-finite value
        (NaN or +/-inf). A NaN-containing vector at retrieval time
        produces NaN cosine scores, which are not caught by
        `EmbeddingRetriever`'s `score <= 0.0` filter (NaN comparisons
        always return False) — the NaN can then rank as a top result
        with no warning. Reject at the write boundary so the store
        cannot hold a poisoned vector.
        """
        for col_name, emb in embeddings.items():
            for i, v in enumerate(emb.vector):
                if not math.isfinite(v):
                    raise ValueError(
                        f"column_embeddings: vector for {col_name!r} contains "
                        f"non-finite value at index {i}: {v!r}. NaN or inf "
                        f"floats poison cosine scoring — reject at the write "
                        f"boundary."
                    )
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

    # ----- Cost ledger ---------------------------------------------
    #
    # Persists cumulative USD spent per
    # `source_connection_id` so a re-run after a crash sees the prior
    # total and refuses the next call if it has already breached the
    # cap. Without this layer, a crash mid-enrichment would let the
    # next run start from $0 — the user has already been billed.
    #
    # Storage is INTEGER micros (1 USD = 1_000_000 micros) to avoid
    # float accumulation drift. Sub-micro precision is rounded away at
    # the `add_spend_usd` boundary — well below any per-call LLM cost
    # today.
    #
    # Atomicity: `add_spend_usd`'s SELECT-then-INSERT-OR-REPLACE is
    # wrapped in `BEGIN IMMEDIATE` when `writer_lock=True` so two
    # processes operating on the same DB file cannot lose updates. In
    # the single-process case, sqlite3's connection-level serialisation
    # is enough.

    def get_spend_usd(self, *, source_connection_id: str) -> float:
        """Return cumulative USD spent on `source_connection_id`.

        Returns `0.0` when no row exists for the source — callers do
        not need to special-case "first call." Returning `None` would
        force every consumer into `(get_spend(...) or 0.0)` boilerplate
        forever.
        """
        conn = self._require_conn()
        row = conn.execute(
            "SELECT spent_usd_micros FROM cost_ledger WHERE source_connection_id = ?",
            (source_connection_id,),
        ).fetchone()
        if row is None:
            return 0.0
        return row["spent_usd_micros"] / _MICROS_PER_USD

    def add_spend_usd(self, *, source_connection_id: str, amount_usd: float) -> float:
        """Atomically add `amount_usd` to the ledger; return the new total.

        Validates `amount_usd >= 0` and `math.isfinite(amount_usd)` —
        defence-in-depth against a buggy adapter or a direct caller
        bypassing the pipeline's runtime guard.

        When `writer_lock=True`, the SELECT-then-INSERT-OR-REPLACE is
        wrapped in `BEGIN IMMEDIATE` so a concurrent process cannot
        slip a write between the read and the write. Without
        `writer_lock`, the implicit transaction model is sufficient
        for single-process concurrency (sqlite3 serialises writes on
        one connection via the GIL).
        """
        if not math.isfinite(amount_usd):
            raise ValueError(
                f"amount_usd must be finite, got {amount_usd!r}. "
                "Cost values must be finite real numbers; pipeline's "
                "runtime guard catches this earlier — this check is "
                "defence-in-depth for direct ledger callers."
            )
        if amount_usd < 0:
            raise ValueError(
                f"amount_usd must be non-negative, got {amount_usd!r}. "
                "The ledger is a monotonic increment counter; refunds "
                "or reversals are out of scope (use reset semantics in "
                "a future commit if needed)."
            )
        # Round to nearest micro. `round()` ties-to-even is fine — any
        # per-call cost above ~$0.000001 (1 micro) is preserved exactly;
        # anything below that is rounded which is the expected loss.
        # `round(x)` returns `int` when called with no second arg — no
        # explicit cast needed.
        scaled = amount_usd * _MICROS_PER_USD
        if not math.isfinite(scaled):
            # `amount_usd` was finite per the guard above, but the
            # multiplication by 1e6 can overflow to `inf` for finite
            # inputs above ~1.8e302. `round(inf)` raises OverflowError
            # which would surface as an undocumented exception type.
            # Reject the input cleanly via the same ValueError shape
            # the rest of the validators use.
            raise ValueError(
                f"amount_usd * {_MICROS_PER_USD} overflows to non-finite: "
                f"{amount_usd!r}. No legitimate per-call LLM cost is this large; "
                f"reject as a likely buggy-adapter signal."
            )
        amount_micros = round(scaled)
        conn = self._require_conn()
        # Toggle isolation_level to manage the transaction explicitly.
        # The default (`""` = deferred BEGIN) auto-starts a transaction
        # on the first write statement, but doesn't let us issue an
        # explicit `BEGIN IMMEDIATE` before the SELECT — which is the
        # whole point of writer_lock. Restoring isolation_level in the
        # `finally` block keeps the rest of the store's `with conn:`
        # paths working as before.
        #
        # **Thread safety:** This toggle is NOT thread-safe. Two
        # threads calling `add_spend_usd` on the same `SQLiteStore`
        # instance would race on the save/restore — Thread B's save
        # captures Thread A's `None`, then Thread A's `finally`
        # restores the original, then Thread B's `finally` restores
        # `None` — leaving the connection in autocommit mode
        # permanently. The module docstring's "callers MUST serialise
        # writes themselves" contract covers this; current call sites
        # (asyncio Lock in `EnrichmentPipeline._record_spend`,
        # single-threaded indexer) all serialise.
        saved_isolation = conn.isolation_level
        conn.isolation_level = None
        try:
            begin_stmt = "BEGIN IMMEDIATE" if self.writer_lock else "BEGIN"
            conn.execute(begin_stmt)
            try:
                row = conn.execute(
                    "SELECT spent_usd_micros FROM cost_ledger WHERE source_connection_id = ?",
                    (source_connection_id,),
                ).fetchone()
                current_micros = row["spent_usd_micros"] if row is not None else 0
                new_micros = current_micros + amount_micros
                conn.execute(
                    "INSERT OR REPLACE INTO cost_ledger "
                    "(source_connection_id, spent_usd_micros, last_updated) "
                    "VALUES (?, ?, ?)",
                    (source_connection_id, new_micros, int(time.time())),
                )
                conn.execute("COMMIT")
                return new_micros / _MICROS_PER_USD
            except BaseException:
                # Rollback might itself fail if no transaction is
                # active (e.g., BEGIN IMMEDIATE raised on a held lock)
                # or the connection is in a degraded state
                # (ProgrammingError on closed conn, InterfaceError on
                # binding mismatch). Suppress the WHOLE
                # `sqlite3.DatabaseError` family so the original
                # exception — the load-bearing signal — is preserved.
                # Narrower `OperationalError` would let
                # `ProgrammingError` replace the root cause.
                with contextlib.suppress(sqlite3.DatabaseError):
                    conn.execute("ROLLBACK")
                raise
        finally:
            conn.isolation_level = saved_isolation

    def search_embeddings_topk(
        self,
        query_vector: list[float],
        *,
        source_connection_id: str,
        k: int,
    ) -> list[tuple[str, str, str, float]]:
        """Return top-`k` `(schema, table, column, cosine_score)` for the query.

        Single bulk SELECT across `column_embeddings` filtered by
        `source_connection_id`, decoded into a float32 NumPy matrix
        (M x D), cosine via matmul + per-row norm, sorted descending by
        score with deterministic alphabetic tiebreak across
        (schema, table, column). Returns at most `k` rows; fewer if the
        store holds fewer embeddings.

        Provides callers with a bulk-fetch alternative to the per-table
        N+1 SQL + Python-loop cosine pattern. Empty store short-circuits
        before any dimension validation — that path must not raise on
        a fresh store.

        Validation:
          - `k >= 1` (caller-side bug if not).
          - `query_vector` non-empty, all-finite, non-zero norm.
            Zero-norm has no direction so cosine is undefined; fail loud.
          - `query_vector` dimension matches stored embedding dimension
            (raises with "dimension" in the message — `EmbeddingRetriever`
            relies on that substring to surface a model-swap hint).

        Zero-norm STORED vectors are scored 0.0 (not NaN). Filtering
        zero-score rows is the caller's job; `EmbeddingRetriever` drops
        them when aggregating to per-table MAX.
        """
        if k < 1:
            raise ValueError(f"k must be >= 1, got {k}")
        if not query_vector:
            raise ValueError("query_vector must be non-empty")
        q = np.asarray(query_vector, dtype=np.float32)
        if not bool(np.all(np.isfinite(q))):
            raise ValueError("query_vector entries must be finite (no NaN/inf)")
        q_norm = float(np.linalg.norm(q))
        if q_norm == 0.0:
            raise ValueError(
                "query_vector must have non-zero norm — a zero vector has no "
                "direction and cosine similarity is undefined"
            )

        conn = self._require_conn()
        rows = conn.execute(
            "SELECT schema_name, table_name, column_name, dimension, vector "
            "FROM column_embeddings "
            "WHERE source_connection_id = ? "
            "ORDER BY schema_name, table_name, column_name",
            (source_connection_id,),
        ).fetchall()
        if not rows:
            # Empty store: skip dimension validation. A caller poking an
            # empty store with the wrong-dim query must NOT raise — the
            # store has no "expected dimension" yet.
            return []

        # All stored vectors should share a dimension under one
        # `source_connection_id` (one embedder model per source), but
        # don't trust SQL — validate per-row.
        expected_dim = int(q.shape[0])
        stored_dim = int(rows[0]["dimension"])
        if stored_dim != expected_dim:
            raise ValueError(
                f"dimension mismatch: query has {expected_dim}, stored has "
                f"{stored_dim}. The embedder used at retrieval time differs "
                f"from the one used at index time. Wipe the store and "
                f"re-index with the new embedder."
            )

        m = len(rows)
        expected_blob_len = expected_dim * _FLOAT32_BYTES
        matrix = np.empty((m, expected_dim), dtype=np.float32)
        for i, row in enumerate(rows):
            row_dim = int(row["dimension"])
            if row_dim != expected_dim:
                # Mixed dimensions within one source — possible if the
                # embedder was swapped mid-index (rare; guarded here for
                # defense-in-depth, not as a normal path). Include
                # `source_connection_id` so a multi-source store doesn't
                # require row-by-row triage to find the bad data.
                raise ValueError(
                    f"dimension mismatch within store: row "
                    f"({row['schema_name']!r}, {row['table_name']!r}, "
                    f"{row['column_name']!r}) under "
                    f"source {source_connection_id!r} has dimension "
                    f"{row_dim}, expected {expected_dim}"
                )
            blob = row["vector"]
            if len(blob) != expected_blob_len:
                # Blob length disagrees with declared dimension. The
                # per-table read path (`get_table_embeddings` →
                # `_unpack_vector`) catches this, but `np.frombuffer`
                # silently truncates — without this guard, a 512-float
                # blob declared at dim 384 would read the first 384
                # floats and produce wrong scores.
                raise ValueError(
                    f"column_embeddings: blob length {len(blob)} for row "
                    f"({row['schema_name']!r}, {row['table_name']!r}, "
                    f"{row['column_name']!r}) does not match expected "
                    f"{expected_blob_len} bytes for dimension {expected_dim}"
                )
            matrix[i] = np.frombuffer(blob, dtype="<f4")

        # Cosine = (M @ q) / (||row|| * ||q||). Replace zero-norm rows'
        # divisor with 1.0 so we don't divide by zero, then overwrite
        # those scores to 0.0 — a zero-norm row has no direction so the
        # only safe answer is "no alignment".
        row_norms = np.linalg.norm(matrix, axis=1)
        safe_norms = np.where(row_norms == 0.0, 1.0, row_norms)
        raw_scores = matrix @ q
        scores = raw_scores / (safe_norms * q_norm)
        scores = np.where(row_norms == 0.0, 0.0, scores)
        # Defense in depth: even with write-side finiteness validation
        # in `write_table_embeddings`, a NaN/inf could in principle
        # arrive via a bypass (manual SQL poke, future store backend
        # that skipped the write guard). NaN scores propagate silently
        # through `EmbeddingRetriever`'s `score <= 0.0` filter (NaN
        # comparisons always return False) and can rank as the top
        # result. Clamp non-finite scores to 0.0 so they're treated as
        # "no signal" instead of poisoning the ranking.
        scores = np.where(np.isfinite(scores), scores, 0.0)

        # Full sort by (-score, schema, table, column) for deterministic
        # tiebreaks at the k boundary. M is bounded by the store's
        # embedding count; full sort is a few ms in practice at v1 scale
        # (<10k columns), enforced by the pytest-benchmark perf gate.
        # `np.argpartition` is a possible optimisation when scales grow.
        indexed = [
            (
                rows[i]["schema_name"],
                rows[i]["table_name"],
                rows[i]["column_name"],
                float(scores[i]),
            )
            for i in range(m)
        ]
        indexed.sort(key=lambda r: (-r[3], r[0], r[1], r[2]))
        return indexed[:k]

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
