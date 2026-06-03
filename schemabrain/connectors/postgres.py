"""PostgreSQL DataSource backed by SQLAlchemy 2.0 reflection."""

from __future__ import annotations

import warnings
from typing import Any

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import NoSuchTableError, SAWarning
from sqlalchemy.pool import NullPool

from schemabrain.connectors._url import safe_engine_url
from schemabrain.connectors.errors import TableNotFoundError
from schemabrain.core.models import Column, ForeignKey, Table

_SYSTEM_SCHEMA_PREFIX = "pg_"
_SYSTEM_SCHEMAS = frozenset({"information_schema"})


class PostgresDataSource:
    """Read-only DataSource for PostgreSQL.

    Skips system schemas (`pg_*` and `information_schema`) when listing
    tables across all schemas. Also filters out declarative partition
    children from bulk listing — querying the partitioned parent fans out
    to all children automatically, so enriching each child duplicates work
    on identical column structure. `get_table()` remains permissive: if a
    caller asks for a partition child by name, they still get it. Uses one
    SQLAlchemy `Engine` per instance; `close()` disposes it and is
    idempotent.
    """

    def __init__(self, url: str) -> None:
        # `default_transaction_read_only=on` is a session-level Postgres
        # parameter that rejects INSERT/UPDATE/DELETE/DROP/TRUNCATE/CREATE
        # at the server. SECURITY.md ("Security Posture Today") states
        # the source-database connection is read-only; this connect_arg
        # enforces that at the wire instead of relying on code review.
        # A future profiler PR that accidentally issues a write would
        # fail with a clear Postgres error rather than silently mutating
        # a customer's production database.
        #
        # `NullPool` opens a fresh connection per request and discards it
        # on close. This eliminates the pool-state-pollution attack
        # surface that would otherwise become live the moment any future
        # feature issues a `SET` or `SET LOCAL` on a pooled connection.
        # Cost: a few ms per call; `index`/`serve` are short-running per
        # process so connection reuse buys nothing observable here.
        # `safe_engine_url` filters smuggled session-config params out of
        # the URL query string before SQLAlchemy sees them; library
        # callers get the filter automatically without remembering to
        # call a CLI-side helper.
        self._engine: Engine | None = create_engine(
            safe_engine_url(url),
            poolclass=NullPool,
            connect_args={"options": "-c default_transaction_read_only=on"},
        )

    def __enter__(self) -> PostgresDataSource:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def list_tables(self, schema: str | None = None) -> list[tuple[str, str]]:
        engine = self._require_engine()
        inspector = inspect(engine)
        partition_children = self._get_partition_children()
        if schema is not None:
            return [
                (schema, name)
                for name in inspector.get_table_names(schema=schema)
                if (schema, name) not in partition_children
            ]
        result: list[tuple[str, str]] = []
        for sch in inspector.get_schema_names():
            if self._is_system_schema(sch):
                continue
            for table_name in inspector.get_table_names(schema=sch):
                if (sch, table_name) in partition_children:
                    continue
                result.append((sch, table_name))
        return result

    def _get_partition_children(self) -> frozenset[tuple[str, str]]:
        """Return ``(schema, name)`` of every declarative partition child.

        Postgres ``pg_class.relispartition`` is ``true`` for partition
        children of a ``PARTITION BY`` parent, and ``false`` for the parent
        itself and for ordinary tables. Children share their parent's
        column structure, so indexing each separately is purely wasted work.
        """
        engine = self._require_engine()
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT n.nspname, c.relname "
                    "FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE c.relispartition = true"
                )
            ).fetchall()
        return frozenset((str(row[0]), str(row[1])) for row in rows)

    def _get_partition_child_fks(self, parent_schema: str, parent_name: str) -> list[ForeignKey]:
        """Return every FK that lives on any partition child of the parent.

        Bypasses SQLAlchemy's `inspector.get_foreign_keys()` because that
        helper sets `referred_schema=None` whenever the target is in the
        Postgres default search_path (`public`), making cross-schema FKs
        indistinguishable from same-schema ones at the reflection layer.
        `pg_constraint` joined to `pg_class` resolves the actual target
        schema by oid — no ambiguity.

        Result is de-duped at the SQL layer by FK identity (source
        columns + target schema + target table + target columns). Postgres
        auto-names FKs per-child even when the same logical FK is added
        to every partition (the Pagila pattern), so a constraint-name-
        based GROUP BY would miss the dedup; the identity tuple catches it.
        """
        engine = self._require_engine()
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT DISTINCT "
                    "  src_cols.cols AS source_columns, "
                    "  tgt_n.nspname AS target_schema, "
                    "  tgt.relname AS target_table, "
                    "  tgt_cols.cols AS target_columns "
                    "FROM pg_constraint con "
                    "JOIN pg_class src ON src.oid = con.conrelid "
                    "JOIN pg_inherits inh ON inh.inhrelid = con.conrelid "
                    "JOIN pg_class parent ON parent.oid = inh.inhparent "
                    "JOIN pg_namespace pn ON pn.oid = parent.relnamespace "
                    "JOIN pg_class tgt ON tgt.oid = con.confrelid "
                    "JOIN pg_namespace tgt_n ON tgt_n.oid = tgt.relnamespace "
                    "JOIN LATERAL ("
                    "  SELECT array_agg(a.attname ORDER BY u.ord) AS cols "
                    "  FROM unnest(con.conkey) WITH ORDINALITY AS u(att, ord) "
                    "  JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = u.att"
                    ") AS src_cols ON true "
                    "JOIN LATERAL ("
                    "  SELECT array_agg(a.attname ORDER BY u.ord) AS cols "
                    "  FROM unnest(con.confkey) WITH ORDINALITY AS u(att, ord) "
                    "  JOIN pg_attribute a ON a.attrelid = con.confrelid AND a.attnum = u.att"
                    ") AS tgt_cols ON true "
                    "WHERE con.contype = 'f' "
                    "AND parent.relname = :parent_name "
                    "AND pn.nspname = :parent_schema "
                    "AND src.relispartition = true "
                    "ORDER BY tgt_n.nspname, tgt.relname"
                ),
                {"parent_name": parent_name, "parent_schema": parent_schema},
            ).fetchall()
        out: list[ForeignKey] = []
        for row in rows:
            source_columns = tuple(str(c) for c in row[0])
            target_schema = str(row[1])
            target_table = str(row[2])
            target_columns = tuple(str(c) for c in row[3])
            # Synthesise a parent-scoped FK name — per-child auto-names
            # would leak partition-specific identifiers onto the parent's
            # surface, where they would later confuse downstream join
            # inference about which table the FK actually anchors on.
            out.append(
                ForeignKey(
                    name=f"fk_{parent_name}_{'_'.join(source_columns)}",
                    source_columns=source_columns,
                    target_schema=target_schema,
                    target_table=target_table,
                    target_columns=target_columns,
                )
            )
        return out

    def get_table(self, name: str, schema: str) -> Table:
        engine = self._require_engine()
        inspector = inspect(engine)
        # SAWarning "Did not recognize type 'xml' of column ..." fires when
        # SQLAlchemy lacks a Python-side equivalent for a Postgres type
        # (xml, tsvector, geometric types, etc.). The connector reads
        # `data_type` from the raw inspector dict — the unrecognized type
        # falls back to NullType but the textual data_type survives — so
        # we already handle the case the warning is warning about. Under
        # `filterwarnings = error` the suite would otherwise crash on any
        # schema with an xml column. Suppress at the narrowest scope:
        # only this inspector call, only the specific message pattern.
        try:
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message=r"Did not recognize type",
                    category=SAWarning,
                )
                cols_info = inspector.get_columns(name, schema=schema)
        except NoSuchTableError as e:
            raise TableNotFoundError(f"Table {schema}.{name} not found") from e

        pk_info = inspector.get_pk_constraint(name, schema=schema)
        pk_set = set(pk_info.get("constrained_columns") or [])

        columns = tuple(
            self._build_column(idx, raw, name, schema, pk_set) for idx, raw in enumerate(cols_info)
        )

        fk_info = inspector.get_foreign_keys(name, schema=schema)
        foreign_keys = tuple(
            self._build_foreign_key(raw, owning_table=name, owning_schema=schema) for raw in fk_info
        )

        # Pagila pattern: declarative partition parents whose FKs sit on
        # the children instead of the parent. Postgres does NOT propagate
        # child-only FKs upward, so `get_foreign_keys(parent)` returns []
        # and the parent looks like a relationship-less island to the
        # downstream entity / join inference. `_get_partition_child_fks`
        # peeks at every child's FKs via `pg_constraint` (one round-trip,
        # SQL-side DISTINCT), bypassing SQLAlchemy's `referred_schema=None`
        # ambiguity for cross-schema targets. De-dup against the parent's
        # own FK set so cleanly-built schemas (FKs declared on the parent
        # and auto-propagated by Postgres) don't double-count.
        child_fks = self._get_partition_child_fks(schema, name)
        if child_fks:
            seen: set[tuple[tuple[str, ...], str, str, tuple[str, ...]]] = {
                self._fk_identity(fk) for fk in foreign_keys
            }
            extra: list[ForeignKey] = []
            for fk in child_fks:
                ident = self._fk_identity(fk)
                if ident in seen:
                    continue
                seen.add(ident)
                extra.append(fk)
            foreign_keys = foreign_keys + tuple(extra)

        return Table(
            name=name,
            schema_name=schema,
            columns=columns,
            foreign_keys=foreign_keys,
        )

    def estimated_row_count(self, name: str, schema: str) -> int | None:
        """Cheap, non-locking row-count estimate from `pg_class.reltuples`.

        Reads the planner's cached estimate — a single catalog lookup, no
        scan and no lock on the user table (unlike `COUNT(*)`), so it is
        safe to call at index time against production tables of any size.

        Returns `None` when the estimate is unavailable rather than
        guessing:
          - the relation isn't found (dropped between listing and here);
          - `reltuples` is negative, Postgres' "never analyzed / unknown"
            sentinel (-1 on PG14+). A table that has never been
            ANALYZE'd or autovacuum'd has no trustworthy estimate, and a
            fabricated 0 would render as a real "0 rows" in the UI.
        A non-negative estimate is rounded to the nearest integer
        (`reltuples` is a float4).
        """
        engine = self._require_engine()
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT c.reltuples AS estimate "
                    "FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = :schema AND c.relname = :name "
                    "AND c.relkind IN ('r', 'p')"
                ),
                {"schema": schema, "name": name},
            ).fetchone()
        if row is None or row[0] is None or row[0] < 0:
            return None
        # `reltuples` is a float4; round to the nearest whole row. A
        # bare `round()` returns int, so no redundant int() wrap.
        return round(float(row[0]))

    def close(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None

    @staticmethod
    def _is_system_schema(name: str) -> bool:
        return name.startswith(_SYSTEM_SCHEMA_PREFIX) or name in _SYSTEM_SCHEMAS

    @staticmethod
    def _build_column(
        index: int,
        raw: dict[str, Any],
        table_name: str,
        schema_name: str,
        pk_columns: set[str],
    ) -> Column:
        return Column(
            name=raw["name"],
            table_name=table_name,
            schema_name=schema_name,
            data_type=str(raw["type"]),
            nullable=bool(raw["nullable"]),
            ordinal_position=index + 1,
            default=raw.get("default"),
            is_primary_key=raw["name"] in pk_columns,
        )

    @staticmethod
    def _build_foreign_key(
        raw: dict[str, Any],
        *,
        owning_table: str,
        owning_schema: str,
    ) -> ForeignKey:
        source_columns = tuple(raw["constrained_columns"])
        return ForeignKey(
            name=raw.get("name") or f"fk_{owning_table}_{'_'.join(source_columns)}",
            source_columns=source_columns,
            target_schema=raw.get("referred_schema") or owning_schema,
            target_table=raw["referred_table"],
            target_columns=tuple(raw["referred_columns"]),
        )

    @staticmethod
    def _fk_identity(fk: ForeignKey) -> tuple[tuple[str, ...], str, str, tuple[str, ...]]:
        """Identity tuple for de-duping FKs surfaced from both parent
        and child partitions. Excludes `name` because parent-declared FKs
        get auto-named per-child by Postgres, so the same logical FK can
        carry a different name on the parent vs. on a child."""
        return (fk.source_columns, fk.target_schema, fk.target_table, fk.target_columns)

    def _require_engine(self) -> Engine:
        if self._engine is None:
            raise RuntimeError("PostgresDataSource is closed")
        return self._engine
