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
from schemabrain.core.entity import (
    DbtOwnedEntityError,
    Entity,
    SingleTableBinding,
)
from schemabrain.core.example_query import ExampleQuery
from schemabrain.core.join import CanonicalJoin, JoinColumnPair
from schemabrain.core.metric import (
    DbtOwnedMetricError,
    Metric,
    MetricMeasure,
)
from schemabrain.core.models import Column, ForeignKey, IncomingForeignKey, Table

# Re-export so existing `from schemabrain.core.store import
# DbtOwnedEntityError` imports keep working. The exceptions now live
# in `core.entity` / `core.metric` (Protocol-contract concerns, not
# SQLite concerns) but the historical import path stays valid.
__all__ = [
    "DbtOwnedEntityError",
    "DbtOwnedMetricError",
    "SQLiteStore",
    "SchemaVersionMismatchError",
]

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
#   "6" → added `example_queries` table for tool #5 `get_example_queries`,
#         the v0.5 read primitive behind real-SQL surfacing per
#         `docs/adr/0001-audit-row-and-pii-taxonomy.md`. SQL-layer
#         CHECK constraints enforce the closed `source` and
#         `sensitivity` enums; `pii_categories` is stored as a sorted
#         comma-separated string with `''` for the empty set, mirroring
#         the storage shape adopted for downstream PII bookkeeping.
#         Empty on v0.5; populated when query log mining lands.
#   "7" → added `idx_example_queries_unique` UNIQUE INDEX on
#         `(source_connection_id, schema_name, table_name, sql_text)`.
#         Load-bearing for `write_example_queries`'s UPSERT: ON CONFLICT
#         needs a unique target. Re-mining the same pg_stat_statements
#         row updates `observation_count`, `last_seen_at`,
#         `sensitivity`, and `pii_categories` while preserving
#         `first_seen_at` so the original observation timestamp
#         survives.
#   "8" → added `entities` table for the semantic-layer
#         foundation. PRIMARY KEY is `(source_connection_id, name)`
#         so point lookups by source are a single B-tree seek. FK
#         CASCADE on `tables` so `delete_table` sweeps every entity
#         bound to the dropped table. SQL-layer CHECK constraint on
#         `origin` mirrors the `Origin` Literal in
#         `schemabrain.core.entity`. The dbt-owned write guard is
#         enforced in `write_entity` (Python-side) rather than at the
#         SQL layer because "manual cannot overwrite dbt_import" is
#         a row-vs-row predicate SQLite triggers don't express
#         cleanly.
#   "9" → added `canonical_joins` table for the canonical-join
#         graph. PRIMARY KEY is `(source_connection_id, name)` — name
#         is human-meaningful and unique per source; multiple canonical
#         joins between the same `(source_entity, target_entity)` pair
#         are explicitly supported (billing/shipping address case).
#         FK CASCADE on `entities` for BOTH source_entity + target_entity
#         so deleting an entity sweeps every canonical join that
#         referenced it. SQL-layer CHECK on `origin` includes
#         `dbt_import` from day one (reserved at the — producer lands
#         wk-15) for schema symmetry with `entities.origin`. `on_columns`
#         stored as JSON-serialised list of `[src_col, tgt_col]` pairs
#         (same shape pattern as dbt-import's upstream-sources storage).
#         Index `idx_canonical_joins_by_pair` keyed
#         `(source_connection_id, target_entity, source_entity, name)`
#         covers BOTH arms of `resolve_canonical_joins`'s `UNION ALL`
#         direction-insensitive lookup as 3-column point seeks; the
#         self-join refusal in the dataclass keeps `UNION ALL` safe
#         without DISTINCT.
#  "10" → added `cardinality TEXT` (nullable) on `canonical_joins`
#         + added `metrics` table for the metric model. Cardinality is
#         consumed by the metric compiler to decide fan-out warnings;
#         NULL is permitted for back-compat with joins authored before
#         the column existed (compiler treats NULL as `many_to_many`
#         worst-case). `metrics` mirrors `entities` in shape — `(source_
#         connection_id, name)` PK, FK CASCADE on the anchor `entities`
#         row, SQL-layer CHECK on `origin` and `measure_agg`. Time grains
#         storage uses the canonical-sorted comma-separated convention
#         from `example_queries.pii_categories`; empty string when
#         `time_dimension IS NULL`. `idx_metrics_by_entity` covers the
#         `WHERE entity = ?` lookup the future audit layer will hit.
#   "11" → added `mcp_audit` table + 2 triggers (no-update, no-delete)
#         + 2 indexes (occurred_at, fingerprint) per ADR 0001. The DDL
#         lives in `schemabrain.audit.ddl` and is applied here so the
#         single source of truth for the on-disk shape stays the
#         `_SCHEMA_VERSION` constant. Empty on first open; every
#         `@instrument`-wrapped tool call writes one row at runtime.
# Older stores raise SchemaVersionMismatchError; pre-alpha users re-create.
_SCHEMA_VERSION = "11"
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
    # Tool #5 `get_example_queries` storage. SQL-layer CHECK
    # constraints enforce the closed `source` and `sensitivity` enums
    # — Phase B ADR §1. `pii_categories` is a sorted comma-separated
    # frozenset; default `''` means no PII categories. FK CASCADE on
    # `tables` so a `delete_table` sweeps out the example rows along
    # with everything else for that table.
    """
    CREATE TABLE IF NOT EXISTS example_queries (
        id INTEGER PRIMARY KEY,
        source_connection_id TEXT NOT NULL,
        schema_name TEXT NOT NULL,
        table_name TEXT NOT NULL,
        sql_text TEXT NOT NULL,
        observation_count INTEGER NOT NULL DEFAULT 1,
        first_seen_at INTEGER NOT NULL,
        last_seen_at INTEGER NOT NULL,
        source TEXT NOT NULL
            CHECK (source IN ('pg_stat_statements', 'curated')),
        sensitivity TEXT NOT NULL DEFAULT 'public'
            CHECK (sensitivity IN ('public', 'internal', 'confidential', 'pii')),
        pii_categories TEXT NOT NULL DEFAULT '',
        FOREIGN KEY (schema_name, table_name, source_connection_id)
            REFERENCES tables (schema_name, name, source_connection_id)
            ON DELETE CASCADE
    )
    """,
    # Covering index for `list_example_queries`. Leads with the three
    # equality predicates `(source_connection_id, schema_name,
    # table_name)` so a typical lookup is a point seek, then carries
    # the ORDER BY columns so the LIMIT result comes straight out of
    # the index without a filesort. Including the ORDER BY columns is
    # the cheap move at v0.5 (the table is empty for most stores);
    # once mining writes rows, retrofitting them would require DROP +
    # CREATE plus a schema bump, so we pay the small index width now.
    # SQLite supports descending index columns since 3.25 (2018).
    """
    CREATE INDEX IF NOT EXISTS idx_example_queries_table_src
        ON example_queries (
            source_connection_id,
            schema_name,
            table_name,
            observation_count DESC,
            last_seen_at DESC,
            id ASC
        )
    """,
    # UNIQUE index that defines the UPSERT conflict target for
    # `write_example_queries`. Mining writes the same (source, schema,
    # table, sql_text) tuple every run; ON CONFLICT lifts the existing
    # row's `first_seen_at` while updating count/recency/PII fields.
    # Storage cost is modest at v0.5 scale (pg_stat_statements caps at
    # ~5000 entries by default; the sql_text values average a few
    # hundred chars).
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_example_queries_unique
        ON example_queries (
            source_connection_id,
            schema_name,
            table_name,
            sql_text
        )
    """,
    # Semantic-layer foundation. `name` is the entity's
    # domain-level identifier (e.g. "customer"); `(source_connection_id,
    # name)` is the PK so point lookups by source are a single B-tree
    # seek. `binding_schema` + `binding_table` carry the entity's
    # single_table binding split into the FK-shaped column pair —
    # this lets the SQLite FK to `tables` enforce that an entity can
    # only bind to an indexed physical table. The `origin` CHECK
    # constraint mirrors `Origin` in `schemabrain.core.entity`; the
    # dbt-owned write guard ("manual cannot overwrite dbt_import")
    # is enforced in `write_entity` rather than via a trigger because
    # it's a row-vs-row predicate.
    """
    CREATE TABLE IF NOT EXISTS entities (
        source_connection_id TEXT NOT NULL,
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        binding_schema TEXT NOT NULL,
        binding_table TEXT NOT NULL,
        identity TEXT NOT NULL,
        origin TEXT NOT NULL
            CHECK (origin IN ('manual', 'suggested', 'dbt_import')),
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        PRIMARY KEY (source_connection_id, name),
        FOREIGN KEY (binding_schema, binding_table, source_connection_id)
            REFERENCES tables (schema_name, name, source_connection_id)
            ON DELETE CASCADE
    )
    """,
    # Canonical-join graph — the substrate. `(source_connection_id,
    # name)` is the PK so name is the public handle (e.g.
    # `order_billing_address`). The `(source_entity, target_entity)` pair
    # is NOT unique — multiple canonical joins between the same pair are
    # explicit (the billing/shipping case). Both entity references FK
    # back to `entities`; CASCADE on entity deletion sweeps every join
    # that touched the dropped entity. CHECK on `origin` covers all three
    # values including `dbt_import` reserved for wk-15.
    #
    # `on_columns_json` carries the equi-join column pairs as JSON to
    # support composite-key joins without a side table — same pattern
    # dbt-import uses for `upstream_sources`. No SQL-level JSON queries
    # exist at v1/v2; a future "which canonical joins reference column
    # X?" query would need either `json_each` or a side-table refactor.
    #
    # `idx_canonical_joins_by_pair` covers BOTH arms of the direction-
    # insensitive `UNION ALL` in `resolve_canonical_joins`. Column order
    # `(source_connection_id, target_entity, source_entity, name)`
    # serves: (a) the seek arm `target_entity = a AND source_entity = b`
    # directly, and (b) the symmetric arm `target_entity = b AND
    # source_entity = a` after a swap of values at query time — both
    # arms are 3-column point seeks. Symmetry is provided by the
    # `UNION ALL` rewrite at the query layer, not by double-indexing.
    """
    CREATE TABLE IF NOT EXISTS canonical_joins (
        source_connection_id TEXT NOT NULL,
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        source_entity TEXT NOT NULL,
        target_entity TEXT NOT NULL,
        on_columns_json TEXT NOT NULL,
        origin TEXT NOT NULL
            CHECK (origin IN ('manual', 'suggested', 'dbt_import')),
        cardinality TEXT
            CHECK (cardinality IS NULL OR cardinality IN (
                'one_to_one', 'one_to_many', 'many_to_one', 'many_to_many'
            )),
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        PRIMARY KEY (source_connection_id, name),
        FOREIGN KEY (source_connection_id, source_entity)
            REFERENCES entities (source_connection_id, name)
            ON DELETE CASCADE,
        FOREIGN KEY (source_connection_id, target_entity)
            REFERENCES entities (source_connection_id, name)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_canonical_joins_by_pair
        ON canonical_joins (
            source_connection_id,
            target_entity,
            source_entity,
            name
        )
    """,
    # Metrics — the value side of the semantic layer. Mirrors `entities`
    # in shape: `(source_connection_id, name)` PK so name is the public
    # handle, FK CASCADE on the anchor entity row so a delete sweeps
    # every metric that depended on it. CHECK on `measure_agg` mirrors
    # the closed `AggFunction` Literal in `schemabrain.core.metric`;
    # CHECK on `origin` mirrors `entities.origin`. `time_grains` stored
    # as a canonical-sorted comma-separated string (`"day,week,month"`),
    # empty string when `time_dimension IS NULL` — the parallel-emptiness
    # invariant is enforced by the dataclass on read AND by upstream
    # writers that already validated the dataclass before calling
    # `write_metric`. `idx_metrics_by_entity` covers the future audit
    # layer's `WHERE entity = ?` lookup the metric → entity drift check
    # will drive.
    """
    CREATE TABLE IF NOT EXISTS metrics (
        source_connection_id TEXT NOT NULL,
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        entity TEXT NOT NULL,
        measure_agg TEXT NOT NULL
            CHECK (measure_agg IN (
                'sum', 'count', 'count_distinct', 'avg', 'min', 'max'
            )),
        measure_column TEXT NOT NULL,
        time_dimension TEXT,
        time_grains TEXT NOT NULL DEFAULT '',
        origin TEXT NOT NULL
            CHECK (origin IN ('manual', 'suggested', 'dbt_import')),
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        PRIMARY KEY (source_connection_id, name),
        FOREIGN KEY (source_connection_id, entity)
            REFERENCES entities (source_connection_id, name)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_metrics_by_entity
        ON metrics (source_connection_id, entity, name)
    """,
)


def _row_to_entity(row: sqlite3.Row) -> Entity:
    """Reconstruct an `Entity` from an `entities` row.

    The store splits the entity's qualified table across two columns
    (`binding_schema`, `binding_table`) for FK integrity, then rejoins
    them on read. The `Entity` constructor re-runs identifier and
    origin-enum validation defensively — a corrupt row would otherwise
    surface much later in MCP-tool callers.
    """
    return Entity(
        name=row["name"],
        description=row["description"],
        binding=SingleTableBinding(
            qualified_table=f"{row['binding_schema']}.{row['binding_table']}"
        ),
        identity=row["identity"],
        origin=row["origin"],
    )


def _row_to_example_query(row: sqlite3.Row) -> ExampleQuery:
    """Reconstruct an `ExampleQuery` from a row.

    Shared by `list_example_queries` (the per-table reader) and
    `list_all_example_queries` (the bulk reader the mining
    pipeline drives).
    """
    return ExampleQuery(
        schema_name=row["schema_name"],
        table_name=row["table_name"],
        sql_text=row["sql_text"],
        observation_count=row["observation_count"],
        first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"],
        source=row["source"],
        sensitivity=row["sensitivity"],
        pii_categories=_decode_pii_categories(row["pii_categories"]),
    )


def _row_to_canonical_join(row: sqlite3.Row) -> CanonicalJoin:
    """Reconstruct a `CanonicalJoin` from a `canonical_joins` row.

    `on_columns_json` is a JSON list of `[src, tgt]` pairs. The
    `JoinColumnPair` constructor re-validates identifier shape on each
    pair so a corrupt row surfaces here, not in an MCP-tool caller
    several layers up.

    Wraps `json.JSONDecodeError` / `TypeError` / `ValueError` from a
    corrupt or hand-edited `on_columns_json` blob into a structured
    `RuntimeError` that names the join so an operator debugging "why
    is `internal_error` showing up on `resolve_join`?" can trace the
    bad row directly. Without this wrap, the JSON error would surface
    as a bare exception in the MCP server's `_wrap_internal_error`
    catch-all with no row identifier in the message.
    """
    try:
        raw_pairs = json.loads(row["on_columns_json"])
        on_pairs = tuple(
            JoinColumnPair(source_column=src, target_column=tgt) for src, tgt in raw_pairs
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise RuntimeError(
            f"corrupt canonical_joins row for join {row['name']!r}: "
            f"on_columns_json is not a valid [[src, tgt], ...] array ({exc})"
        ) from exc
    return CanonicalJoin(
        name=row["name"],
        description=row["description"],
        source_entity=row["source_entity"],
        target_entity=row["target_entity"],
        on=on_pairs,
        origin=row["origin"],
        cardinality=row["cardinality"],
    )


def _row_to_metric(row: sqlite3.Row) -> Metric:
    """Reconstruct a `Metric` from a `metrics` row.

    `time_grains` is stored as a sorted comma-separated string with `''`
    meaning the empty set (the same shape pattern as
    `example_queries.pii_categories`). The `Metric` constructor
    re-runs all invariants (identifier shape, paired-emptiness on the
    time fields, canonical grain order, enum membership) — a corrupt
    row surfaces here, not in an MCP-tool caller several layers up.
    """
    stored_grains = row["time_grains"]
    time_grains = tuple(stored_grains.split(",")) if stored_grains else ()
    return Metric(
        name=row["name"],
        description=row["description"],
        entity=row["entity"],
        measure=MetricMeasure(
            agg=row["measure_agg"],
            column=row["measure_column"],
        ),
        time_dimension=row["time_dimension"],
        time_grains=time_grains,  # type: ignore[arg-type]
        origin=row["origin"],
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


def _decode_pii_categories(stored: str) -> frozenset[str]:
    """Inverse of the storage shape for `example_queries.pii_categories`.

    Storage is a sorted comma-separated string with `''` meaning the
    empty set. Splitting `''` on `','` yields `['']` (one empty
    element), which would be a category named "" — wrong. Short-
    circuit the empty case explicitly.

    Returns `frozenset[str]` rather than `frozenset[PIICategory]`
    because the typing is informational at this layer — the closed
    enumeration is enforced at the SQL CHECK layer (for the
    `sensitivity` column) and by mining-side validation (for the
    multi-value `pii_categories` column when mining lands).
    """
    if not stored:
        return frozenset()
    return frozenset(stored.split(","))


def _encode_pii_categories(categories: frozenset[str]) -> str:
    """Inverse of `_decode_pii_categories` — frozenset → sorted CSV.

    Canonicalising to sorted order makes the storage shape
    deterministic across callers: two mining runs that produce the
    same conceptual category set always write the same byte string,
    so the UPSERT's idempotency claim holds regardless of how the
    caller computed the set.
    """
    if not categories:
        return ""
    return ",".join(sorted(categories))


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
        `column_descriptions`, `column_embeddings`, and
        `example_queries`. No-op if the row is absent.
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

    def write_example_queries(
        self,
        rows: list[ExampleQuery],
        *,
        source_connection_id: str,
    ) -> int:
        """UPSERT a batch of `example_queries` rows under one source.

        Conflict target is `(source_connection_id, schema_name,
        table_name, sql_text)` — the UNIQUE index added at schema v7.
        On conflict, `observation_count`, `last_seen_at`,
        `sensitivity`, and `pii_categories` are updated; `first_seen_at`
        is preserved so the original observation timestamp survives
        re-mining.

        Atomic across the batch: any insert/update that fails (FK
        violation, CHECK violation) rolls back every row in the call —
        callers see either the full batch landed or nothing did.

        Returns the number of `executemany` row writes — equal to
        `len(rows)` on the happy path. Empty input returns 0 without
        opening a transaction.
        """
        if not rows:
            return 0
        conn = self._require_conn()
        payload = [
            (
                source_connection_id,
                row.schema_name,
                row.table_name,
                row.sql_text,
                row.observation_count,
                row.first_seen_at,
                row.last_seen_at,
                row.source,
                row.sensitivity,
                _encode_pii_categories(row.pii_categories),
            )
            for row in rows
        ]
        with conn:
            conn.executemany(
                "INSERT INTO example_queries "
                "(source_connection_id, schema_name, table_name, sql_text, "
                "observation_count, first_seen_at, last_seen_at, source, "
                "sensitivity, pii_categories) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (source_connection_id, schema_name, table_name, sql_text) "
                "DO UPDATE SET "
                # MAX(...) preserves the higher of the two counts so a
                # `pg_stat_statements_reset()` between mining runs (the
                # source counter is cumulative since last reset) cannot
                # silently overwrite a higher historical count with a
                # smaller post-reset value. The observation count is
                # advisory ("how often have we seen this pattern?") and
                # taking the max is the honest answer for that question.
                "observation_count = MAX(excluded.observation_count, example_queries.observation_count), "
                "last_seen_at = excluded.last_seen_at, "
                "sensitivity = excluded.sensitivity, "
                "pii_categories = excluded.pii_categories",
                payload,
            )
        return len(rows)

    def list_all_example_queries(
        self,
        *,
        source_connection_id: str,
    ) -> list[ExampleQuery]:
        """Return every example query row for `source_connection_id`.

        Returns the FULL row set — duplicates expected when a single
        SQL statement touched multiple indexed tables (the schema
        carries one row per `(sql_text, table_touched)` pair). Callers
        that want distinct statements dedupe in memory by `sql_text`.

        Ordered alphabetically by `(schema_name, table_name, sql_text)`
        for stable iteration across runs. Empty result is the right
        answer for a store that has not been mined yet — the joins
        suggester falls back to FK evidence alone in that case.

        Symmetric with `list_all_foreign_keys` (bulk reader for
        graph-construction callers). Unlike `list_example_queries`,
        there is no `limit` parameter — the v0.5 pg_stat_statements
        cap (~5000 statements x ~4 tables/statement = ~20k rows max)
        is small enough that pagination is not yet warranted.
        """
        conn = self._require_conn()
        rows = conn.execute(
            "SELECT schema_name, table_name, sql_text, observation_count, "
            "first_seen_at, last_seen_at, source, sensitivity, pii_categories "
            "FROM example_queries "
            "WHERE source_connection_id = ? "
            "ORDER BY schema_name, table_name, sql_text",
            (source_connection_id,),
        ).fetchall()
        return [_row_to_example_query(row) for row in rows]

    def list_example_queries(
        self,
        schema: str,
        table: str,
        *,
        source_connection_id: str,
        limit: int,
    ) -> list[ExampleQuery]:
        """Return observed example SQL for `(schema, table)` under
        `source_connection_id`, most-popular first.

        Ranking (descending):
          1. `observation_count` — how many times the same SQL pattern
             was observed.
          2. `last_seen_at` — recency tiebreak for equally-popular
             patterns.
          3. `id` ascending — deterministic last tiebreak so two rows
             tied on count + recency rank by insertion order.

        Empty result is the right answer when the table has no
        recorded examples — a caller (the MCP tool) translates that
        into a charter-compliant `status="empty"` envelope rather than
        an error.

        Raises `ValueError` if `limit < 1`; mirrors the `k` contract
        on `search_embeddings_topk`.
        """
        if limit < 1:
            raise ValueError(f"limit must be >= 1, got {limit}")
        conn = self._require_conn()
        rows = conn.execute(
            "SELECT schema_name, table_name, sql_text, observation_count, "
            "first_seen_at, last_seen_at, source, sensitivity, pii_categories "
            "FROM example_queries "
            "WHERE source_connection_id = ? "
            "AND schema_name = ? "
            "AND table_name = ? "
            "ORDER BY observation_count DESC, last_seen_at DESC, id ASC "
            "LIMIT ?",
            (source_connection_id, schema, table, limit),
        ).fetchall()
        return [_row_to_example_query(row) for row in rows]

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

    # ----- Entities ------------------------------------------------
    #
    # Semantic-layer foundation. The read side is the substrate for
    # the `list_entities` / `describe_entity` MCP tools; the write
    # side is the substrate for `schemabrain entities apply <yaml>`
    # and a future dbt-import write-path.

    def write_entity(self, entity: Entity, *, source_connection_id: str) -> None:
        """Idempotent upsert of one Entity.

        Refuses to overwrite a `dbt_import` row with a non-`dbt_import`
        entity — raises `DbtOwnedEntityError`. The check is a SELECT
        + branch inside the same connection that does the write; the
        outer `with conn:` block makes the read + write atomic. Note
        that `sqlite3` connection-context-manager commits on success
        and rolls back on exception; the dbt-guard exception triggers
        the rollback path, which is fine because no INSERT has run
        yet.

        Single-statement UPSERT — preserves `created_at` across
        re-writes by letting `excluded.created_at` lose to the
        existing row, while `updated_at` advances every write.
        """
        schema_name, table_name = entity.binding.qualified_table.split(".", 1)
        conn = self._require_conn()
        with conn:
            existing = conn.execute(
                "SELECT origin FROM entities WHERE source_connection_id = ? AND name = ?",
                (source_connection_id, entity.name),
            ).fetchone()
            if (
                existing is not None
                and existing["origin"] == "dbt_import"
                and entity.origin != "dbt_import"
            ):
                raise DbtOwnedEntityError(
                    f"entity {entity.name!r} is owned by dbt import "
                    f"(origin='dbt_import'); manual / suggested writes refused. "
                    f"Edit the upstream dbt model and re-run "
                    f"`schemabrain import dbt`."
                )
            now = int(time.time())
            # NB: `created_at` is deliberately omitted from the UPDATE
            # set so the original insertion timestamp survives every
            # UPSERT. Only `updated_at` advances; the asymmetry is the
            # whole reason both columns exist.
            conn.execute(
                "INSERT INTO entities ("
                "source_connection_id, name, description, "
                "binding_schema, binding_table, identity, origin, "
                "created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (source_connection_id, name) DO UPDATE SET "
                "description = excluded.description, "
                "binding_schema = excluded.binding_schema, "
                "binding_table = excluded.binding_table, "
                "identity = excluded.identity, "
                "origin = excluded.origin, "
                "updated_at = excluded.updated_at",
                (
                    source_connection_id,
                    entity.name,
                    entity.description,
                    schema_name,
                    table_name,
                    entity.identity,
                    entity.origin,
                    now,
                    now,
                ),
            )

    def get_entity(self, name: str, *, source_connection_id: str) -> Entity | None:
        conn = self._require_conn()
        row = conn.execute(
            "SELECT name, description, binding_schema, binding_table, "
            "identity, origin FROM entities "
            "WHERE source_connection_id = ? AND name = ?",
            (source_connection_id, name),
        ).fetchone()
        if row is None:
            return None
        return _row_to_entity(row)

    def list_entities(self, *, source_connection_id: str | None = None) -> list[Entity]:
        """Return all entities, ordered alphabetically by name.

        `source_connection_id=None` lists across sources; ordering is
        `(name, source_connection_id)` so the result is deterministic
        across both the filtered and unfiltered call shapes. The MCP
        `list_entities` tool depends on the alpha order so the agent
        sees a stable list across calls.
        """
        conn = self._require_conn()
        if source_connection_id is None:
            rows = conn.execute(
                "SELECT name, description, binding_schema, binding_table, "
                "identity, origin FROM entities "
                "ORDER BY name, source_connection_id"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT name, description, binding_schema, binding_table, "
                "identity, origin FROM entities "
                "WHERE source_connection_id = ? ORDER BY name",
                (source_connection_id,),
            ).fetchall()
        return [_row_to_entity(row) for row in rows]

    # ----- Canonical joins ------------------------------------------
    #
    # The the semantic-layer substrate. Naming + persistence shape
    # match `Entity` deliberately so future surfaces (audit log, drift
    # detection, multi-hop query planning at v2) compose against a
    # consistent shape.

    def write_canonical_join(self, join: CanonicalJoin, *, source_connection_id: str) -> None:
        """Idempotent upsert of one `CanonicalJoin`.

        Both `source_entity` and `target_entity` MUST already exist in
        the `entities` table for this source — the FK constraints
        enforce that at SQLite layer. Callers (the suggest pipeline and
        `joins apply` CLI) surface a guided error if either entity is
        missing.

        Single-statement UPSERT keyed on `(source_connection_id, name)`
        — preserves `created_at` across re-writes while `updated_at`
        advances every write.

        Composite-key joins (multiple `on` pairs) serialise the `on`
        list to JSON; the dataclass guarantees non-empty + identifier-
        shaped column names, so the persisted JSON is always
        round-trippable.

        Note: unlike `write_entity`, this method does NOT yet enforce
        a dbt-import ownership guard. No `dbt_import` join producer
        exists at this release — the dbt-relationships importer lands at
        v1 wk-15. When that producer lands, add a `DbtOwnedJoinError`
        guard symmetric with `DbtOwnedEntityError` so manual/suggested
        writes can't overwrite dbt-imported joins.
        """
        on_columns_json = json.dumps([[pair.source_column, pair.target_column] for pair in join.on])
        conn = self._require_conn()
        with conn:
            now = int(time.time())
            conn.execute(
                "INSERT INTO canonical_joins ("
                "source_connection_id, name, description, "
                "source_entity, target_entity, on_columns_json, origin, "
                "cardinality, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (source_connection_id, name) DO UPDATE SET "
                "description = excluded.description, "
                "source_entity = excluded.source_entity, "
                "target_entity = excluded.target_entity, "
                "on_columns_json = excluded.on_columns_json, "
                "origin = excluded.origin, "
                "cardinality = excluded.cardinality, "
                "updated_at = excluded.updated_at",
                (
                    source_connection_id,
                    join.name,
                    join.description,
                    join.source_entity,
                    join.target_entity,
                    on_columns_json,
                    join.origin,
                    join.cardinality,
                    now,
                    now,
                ),
            )

    def get_canonical_join(self, name: str, *, source_connection_id: str) -> CanonicalJoin | None:
        """Return the canonical join named `name`, or `None` if absent."""
        conn = self._require_conn()
        row = conn.execute(
            "SELECT name, description, source_entity, target_entity, "
            "on_columns_json, origin, cardinality FROM canonical_joins "
            "WHERE source_connection_id = ? AND name = ?",
            (source_connection_id, name),
        ).fetchone()
        if row is None:
            return None
        return _row_to_canonical_join(row)

    def list_canonical_joins(
        self, *, source_connection_id: str | None = None
    ) -> list[CanonicalJoin]:
        """Return all canonical joins, ordered alphabetically by name.

        `source_connection_id=None` lists across sources; the CLI
        `joins list` command depends on the alpha order so output is
        stable across runs.
        """
        conn = self._require_conn()
        if source_connection_id is None:
            rows = conn.execute(
                "SELECT name, description, source_entity, target_entity, "
                "on_columns_json, origin, cardinality FROM canonical_joins "
                "ORDER BY name, source_connection_id"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT name, description, source_entity, target_entity, "
                "on_columns_json, origin, cardinality FROM canonical_joins "
                "WHERE source_connection_id = ? ORDER BY name",
                (source_connection_id,),
            ).fetchall()
        return [_row_to_canonical_join(row) for row in rows]

    def resolve_canonical_joins(
        self,
        entity_a: str,
        entity_b: str,
        *,
        source_connection_id: str,
    ) -> list[CanonicalJoin]:
        """Return every canonical join between entities `a` and `b`.

        Direction-insensitive: matches rows where `(source_entity,
        target_entity)` is either `(a, b)` or `(b, a)`. The caller
        (`resolve_join` MCP tool) implements the ambiguity-refusal
        logic against this list — 0 rows → no canonical join,
        2+ rows → ambiguous (name-disambiguator path).

        Each row preserves its STORED direction; the caller does not
        flip columns based on the input order. This keeps the
        agent-facing `sql_skeleton` aligned with how the join was
        originally confirmed (the user picked the direction at
        `joins apply` time and that direction is canonical).

        Ordered alphabetically by `name` for determinism across runs.

        Implementation: `UNION ALL` of two 3-column point seeks against
        `idx_canonical_joins_by_pair`. The single-OR form attempted in
        an earlier revision degraded to a range scan over all joins
        for the source under SQLite 3.x's OR planner — `EXPLAIN QUERY
        PLAN` confirmed it. The `UNION ALL` rewrite hits the index
        directly for both arms. Safe to use without `DISTINCT` because
        the `CanonicalJoin` dataclass refuses self-joins, so a single
        stored row cannot match both arms.
        """
        conn = self._require_conn()
        select_cols = (
            "SELECT name, description, source_entity, target_entity, "
            "on_columns_json, origin, cardinality FROM canonical_joins "
        )
        rows = conn.execute(
            f"{select_cols}"
            "WHERE source_connection_id = ? "
            "  AND source_entity = ? AND target_entity = ? "
            "UNION ALL "
            f"{select_cols}"
            "WHERE source_connection_id = ? "
            "  AND source_entity = ? AND target_entity = ? "
            "ORDER BY name",
            (
                source_connection_id,
                entity_a,
                entity_b,
                source_connection_id,
                entity_b,
                entity_a,
            ),
        ).fetchall()
        return [_row_to_canonical_join(row) for row in rows]

    # ----- Metrics --------------------------------------------------
    #
    # The value side of the semantic layer. Same shape pattern as
    # `Entity` / `CanonicalJoin` — name-keyed, FK-anchored on entities,
    # closed-grammar agg + grain enforced at the SQL CHECK layer and
    # re-checked on read by the dataclass. The dbt-owned write guard
    # mirrors `write_entity`'s behaviour: a manual or LLM-suggested
    # write over a `dbt_import` row raises `DbtOwnedMetricError`.

    def write_metric(self, metric: Metric, *, source_connection_id: str) -> None:
        """Idempotent upsert of one `Metric`.

        The anchor entity (`metric.entity`) MUST already exist in the
        `entities` table for this source — the FK constraint enforces
        that at the SQLite layer. Callers (the apply pipeline + CLI)
        surface a guided error if the entity is missing.

        dbt-owned write guard: writing a manual / suggested metric over
        an existing `dbt_import` row raises `DbtOwnedMetricError`. Same
        check shape as `write_entity` — a SELECT-then-INSERT against
        the existing row's origin. Re-importing the same metric via
        `dbt_import → dbt_import` is the intended idempotent path and
        is permitted.

        Single-statement UPSERT keyed on `(source_connection_id, name)`
        — preserves `created_at` across re-writes while `updated_at`
        advances every write.

        `time_grains` serialises to a sorted comma-separated string;
        the dataclass guarantees canonical sort + uniqueness so the
        persisted shape is deterministic and round-trips byte-identical.
        """
        stored_grains = ",".join(metric.time_grains) if metric.time_grains else ""
        conn = self._require_conn()
        with conn:
            existing = conn.execute(
                "SELECT origin FROM metrics WHERE source_connection_id = ? AND name = ?",
                (source_connection_id, metric.name),
            ).fetchone()
            if (
                existing is not None
                and existing["origin"] == "dbt_import"
                and metric.origin != "dbt_import"
            ):
                raise DbtOwnedMetricError(
                    f"metric {metric.name!r} is owned by dbt import "
                    f"(origin='dbt_import'); manual / suggested writes refused. "
                    f"Edit the upstream dbt model and re-run "
                    f"`schemabrain import dbt --include-metrics`."
                )
            now = int(time.time())
            # `created_at` is deliberately omitted from the UPDATE set
            # so the original insertion timestamp survives every UPSERT
            # — same asymmetry as `write_entity` / `write_canonical_join`.
            # `entity` is deliberately omitted from the DO UPDATE SET.
            # The anchor entity is structurally part of what a metric
            # IS — a `total_revenue` metric re-anchored from `order` to
            # `customer` is a different metric, not an update. A
            # caller wanting to change the anchor must delete the
            # metric and re-create. (Mirrors the discipline used for
            # `entities.name` and `canonical_joins.name`, which are
            # part of the PK and cannot mutate either.)
            conn.execute(
                "INSERT INTO metrics ("
                "source_connection_id, name, description, "
                "entity, measure_agg, measure_column, "
                "time_dimension, time_grains, origin, "
                "created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (source_connection_id, name) DO UPDATE SET "
                "description = excluded.description, "
                "measure_agg = excluded.measure_agg, "
                "measure_column = excluded.measure_column, "
                "time_dimension = excluded.time_dimension, "
                "time_grains = excluded.time_grains, "
                "origin = excluded.origin, "
                "updated_at = excluded.updated_at",
                (
                    source_connection_id,
                    metric.name,
                    metric.description,
                    metric.entity,
                    metric.measure.agg,
                    metric.measure.column,
                    metric.time_dimension,
                    stored_grains,
                    metric.origin,
                    now,
                    now,
                ),
            )

    def get_metric(self, name: str, *, source_connection_id: str) -> Metric | None:
        """Return the metric named `name`, or `None` if absent."""
        conn = self._require_conn()
        row = conn.execute(
            "SELECT name, description, entity, measure_agg, measure_column, "
            "time_dimension, time_grains, origin FROM metrics "
            "WHERE source_connection_id = ? AND name = ?",
            (source_connection_id, name),
        ).fetchone()
        if row is None:
            return None
        return _row_to_metric(row)

    def list_metrics(self, *, source_connection_id: str | None = None) -> list[Metric]:
        """Return all metrics, ordered alphabetically by name.

        `source_connection_id=None` lists across sources; ordering is
        `(name, source_connection_id)` so cross-source results are
        deterministic. The CLI `metrics list` command depends on the
        alphabetical order for stable output across runs.
        """
        conn = self._require_conn()
        if source_connection_id is None:
            rows = conn.execute(
                "SELECT name, description, entity, measure_agg, measure_column, "
                "time_dimension, time_grains, origin FROM metrics "
                "ORDER BY name, source_connection_id"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT name, description, entity, measure_agg, measure_column, "
                "time_dimension, time_grains, origin FROM metrics "
                "WHERE source_connection_id = ? ORDER BY name",
                (source_connection_id,),
            ).fetchall()
        return [_row_to_metric(row) for row in rows]

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _init_schema(self) -> None:
        # `mcp_audit` DDL is owned by the audit module so the audit
        # surface stays co-located with its writer + verifier. The
        # import is local to avoid an `audit -> core -> audit` cycle
        # at module-load time; runtime cost is one-time per process.
        from schemabrain.audit.ddl import ensure_audit_schema

        conn = self._require_conn()
        # Single atomic block: core DDL + version stamp + audit DDL all
        # commit together. A crash mid-block rolls back, so the version
        # stamp never lands without the `mcp_audit` table that the
        # stamp implies exists.
        with conn:
            for stmt in _DDL_STATEMENTS:
                conn.execute(stmt)
            conn.execute(
                "INSERT OR IGNORE INTO schemabrain_meta (key, value) VALUES (?, ?)",
                ("schema_version", _SCHEMA_VERSION),
            )
            ensure_audit_schema(conn)
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
