"""PostgreSQL-backed profiler.

Issues exactly N+1 SQL statements per table — one batched stats query
returning total / non-null / distinct counts for every column at once, plus
one sample query per column. Cost stays linear in column count and avoids a
COUNT(*) per column.

PII redaction runs on every sample value before it leaves this module, so
emails, SSNs, and credit-card-shaped substrings never reach the cache or
any downstream LLM call.

Output is fully deterministic: sample queries use `ORDER BY 1` so two
profiles of an unchanged table produce byte-identical `ColumnStats`. This
is a hard requirement for the content-addressable fingerprint cache built
on top of these stats.
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.pool import NullPool
from sqlalchemy.sql.compiler import IdentifierPreparer

from schemabrain.connectors._url import safe_engine_url
from schemabrain.connectors.errors import TableNotFoundError
from schemabrain.core.models import Column, Table
from schemabrain.profiler.stats import (
    SAMPLE_TRUNCATE_AT,
    ColumnStats,
    redact_pii,
    top_shapes,
)

_DEFAULT_SAMPLE_SIZE = 5
# Cap raw values before regex / truncation so a 10MB JSON column doesn't make
# `redact_pii` linear in the column's worst case. Set well above any realistic
# PII length (max ~30 chars) so PII anywhere in the captured window is fully
# present for the regex pass. Anything past this cap is dropped before display
# truncation, so it cannot leak.
_SAMPLE_RAW_CAP = 1000

# Postgres types that have no built-in equality operator. `COUNT(DISTINCT col)`
# against any of these raises SQLSTATE 42883 (UndefinedFunction). The list is
# narrow and well-defined; expand only when a real schema surfaces a new gap.
# Documented as a 2026-05-18 production-DB smoke finding against AdventureWorks
# (xml columns in humanresources.jobcandidate.resume etc.).
#
# `"null"` is included to cover SQLAlchemy's `NullType` fallback: when the
# dialect doesn't have a Python-side equivalent for a Postgres type (xml,
# tsvector, custom types, etc.) it falls back to `NullType`, which `str()`s
# to "NULL". Without this entry, the connector reports `data_type="NULL"`,
# the profiler thinks the column supports DISTINCT, and the query crashes
# at runtime. Treating NullType as no-equality is the conservative call:
# we genuinely don't know if the underlying type supports equality, so
# don't try.
_NO_EQUALITY_TYPES: frozenset[str] = frozenset(
    {
        "null",
        "xml",
        "point",
        "line",
        "lseg",
        "box",
        "path",
        "polygon",
        "circle",
        "tsvector",
    }
)


def _supports_distinct(data_type: str) -> bool:
    """Return False for column types that don't support `COUNT(DISTINCT)`.

    Strips the parameterised-type suffix so `character varying(255)` ->
    `character varying` and `numeric(5, 2)` -> `numeric` resolve cleanly.
    Case-insensitive match against `_NO_EQUALITY_TYPES`.
    """
    base = data_type.split("(")[0].strip().lower()
    return base not in _NO_EQUALITY_TYPES


class PostgresProfiler:
    """Profile Postgres tables into per-column `ColumnStats`.

    Owns its own SQLAlchemy `Engine`. `close()` disposes it and is
    idempotent. Use as a context manager to guarantee cleanup.
    """

    def __init__(self, url: str, *, sample_size: int = _DEFAULT_SAMPLE_SIZE) -> None:
        if sample_size < 1:
            raise ValueError(f"sample_size must be >= 1, got {sample_size}")
        self._sample_size = sample_size
        # See `PostgresDataSource.__init__` for the rationale: server-side
        # enforcement of read-only access on the source database. The
        # profiler only ever issues SELECT, but a defense-in-depth flag
        # at session level prevents a future regression from silently
        # mutating customer data. `NullPool` eliminates the latent
        # pool-state-pollution surface that would otherwise become live
        # the moment any future feature issues a `SET` on a pooled
        # connection.
        self._engine: Engine | None = create_engine(
            safe_engine_url(url),
            poolclass=NullPool,
            connect_args={"options": "-c default_transaction_read_only=on"},
        )

    def __enter__(self) -> PostgresProfiler:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def profile_table(self, table: Table) -> dict[str, ColumnStats]:
        """Return a mapping of column name -> `ColumnStats` for every column.

        Raises `TableNotFoundError` if the table does not exist on the
        underlying Postgres at call time.
        """
        engine = self._require_engine()
        if not table.columns:
            return {}

        preparer = engine.dialect.identifier_preparer
        quoted_table = f"{preparer.quote_schema(table.schema_name)}.{preparer.quote(table.name)}"
        col_names = [c.name for c in table.columns]

        counts = self._fetch_counts(engine, quoted_table, table.columns, preparer, table)
        result: dict[str, ColumnStats] = {}
        for col_name in col_names:
            total, non_null, distinct = counts[col_name]
            null_count = total - non_null
            display_samples, raw_truncated = self._fetch_samples(
                engine, quoted_table, col_name, preparer
            )
            result[col_name] = ColumnStats(
                column_name=col_name,
                total_rows=total,
                null_count=null_count,
                distinct_count=distinct,
                sample_values=display_samples,
                shape_patterns=top_shapes(raw_truncated) if raw_truncated else (),
            )
        return result

    def close(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None

    def _require_engine(self) -> Engine:
        if self._engine is None:
            raise RuntimeError("PostgresProfiler is closed")
        return self._engine

    @staticmethod
    def _fetch_counts(
        engine: Engine,
        quoted_table: str,
        columns: tuple[Column, ...],
        preparer: IdentifierPreparer,
        table: Table,
    ) -> dict[str, tuple[int, int, int]]:
        """Run one batched query returning (total, non_null, distinct) per column.

        Aliases use positional indices (`nn_0`, `d_0`, ...) so the column
        name itself never has to satisfy SQL identifier rules.

        For columns whose Postgres type doesn't support equality (see
        `_NO_EQUALITY_TYPES`), `COUNT(DISTINCT col)` is replaced with
        `NULL::bigint`. The result dict gets `distinct = non_null` for those
        columns (worst-case-cardinality assumption — the LLM's cardinality
        hint should err on the side of "lots of distinct values" rather than
        "all the same"). This keeps the indexer running on schemas with
        `xml`, `tsvector`, geometric, etc. columns instead of crashing.

        Translates Postgres "table does not exist" into `TableNotFoundError`
        in one round-trip — no separate existence check, no TOCTOU window.
        """
        select_parts = ["COUNT(*) AS total"]
        for idx, col in enumerate(columns):
            quoted_col = preparer.quote(col.name)
            select_parts.append(f"COUNT({quoted_col}) AS nn_{idx}")
            if _supports_distinct(col.data_type):
                select_parts.append(f"COUNT(DISTINCT {quoted_col}) AS d_{idx}")
            else:
                # Cast to bigint so the result row's type matches the
                # `int(mapping[f"d_{idx}"])` branch's expectation. Without
                # the cast, the NULL would type-infer to text and the
                # int() conversion would raise.
                select_parts.append(f"NULL::bigint AS d_{idx}")
        # Identifier-only f-string: `quoted_table` and every `quoted_col`
        # are produced by SQLAlchemy's `IdentifierPreparer.quote()` /
        # `.quote_schema()`, which dialect-escapes them (wraps in double
        # quotes, doubles any embedded double-quote). `idx` is a loop
        # counter. No user input enters this string. See SECURITY.md
        # for the project's broader SQL-construction policy.
        sql = f"SELECT {', '.join(select_parts)} FROM {quoted_table}"  # nosec B608
        try:
            with engine.connect() as conn:
                # `exec_driver_sql` bypasses SQLAlchemy's parameter
                # binding layer. We can't use `text(sql)` here because
                # psycopg's pyformat paramstyle treats `%` as a
                # parameter marker, so any `%` baked into a column name
                # (e.g. BIRD's `Percent (%) Eligible Free (K-12)`) is
                # double-escaped (`%` -> `%%` -> `%%%%`) and the query
                # dies in Postgres parse. The SQL has zero bind
                # parameters, so going straight to the DBAPI is exactly
                # what we want.
                row = conn.exec_driver_sql(sql).one()
        except ProgrammingError as e:
            # Only `UndefinedTable` (SQLSTATE 42P01) gets translated into a
            # `TableNotFoundError`. Other ProgrammingErrors — typically
            # `UndefinedColumn` (42703) when a stale `Table` references a
            # dropped column — propagate as-is so they don't masquerade as
            # missing tables.
            sqlstate = getattr(getattr(e, "orig", None), "sqlstate", None)
            if sqlstate == "42P01":
                raise TableNotFoundError(f"Table {table.schema_name}.{table.name} not found") from e
            raise
        mapping = row._mapping
        total = int(mapping["total"])
        out: dict[str, tuple[int, int, int]] = {}
        for idx, col in enumerate(columns):
            non_null = int(mapping[f"nn_{idx}"])
            distinct_raw = mapping[f"d_{idx}"]
            # `distinct_raw is None` only happens when `_supports_distinct`
            # returned False for this column (the SELECT emitted
            # `NULL::bigint` instead of `COUNT(DISTINCT col)`). Fall back to
            # `non_null` — see method docstring for why this is the right
            # max-cardinality hedge.
            distinct = non_null if distinct_raw is None else int(distinct_raw)
            out[col.name] = (total, non_null, distinct)
        return out

    def _fetch_samples(
        self,
        engine: Engine,
        quoted_table: str,
        col_name: str,
        preparer: IdentifierPreparer,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Fetch up to `sample_size` distinct non-null values.

        Returns a pair `(display_samples, raw_truncated)`:
        - `display_samples` are PII-redacted and safe to persist or send to
          an LLM. Each sample is at most `SAMPLE_TRUNCATE_AT` chars.
        - `raw_truncated` are truncated-but-unredacted, used only locally
          to compute shape signatures and discarded immediately. They
          never enter `ColumnStats` and never escape this function's caller.

        `ORDER BY 1` makes the result deterministic. Without it Postgres
        returns rows in physical-scan order, which can vary between runs
        and would defeat the content-addressable cache.
        """
        quoted_col = preparer.quote(col_name)
        # Identifier-only f-string: `quoted_table` and `quoted_col`
        # come from SQLAlchemy's `IdentifierPreparer.quote()` /
        # `.quote_schema()` (dialect escaped). `self._sample_size` is
        # an int set at profiler construction. No user input enters
        # this string. See SECURITY.md ("Security Posture Today") for
        # the broader SQL-construction policy. The `# nosec B608` sits
        # on the first f-string line because that's where bandit
        # attaches the finding for multi-line concatenations.
        sql = (
            f"SELECT DISTINCT {quoted_col}::text AS v "  # nosec B608
            f"FROM {quoted_table} "
            f"WHERE {quoted_col} IS NOT NULL "
            f"ORDER BY 1 "
            f"LIMIT {self._sample_size}"
        )
        with engine.connect() as conn:
            # `exec_driver_sql` for the same reason as `_fetch_counts`:
            # `text(sql)` triggers psycopg pyformat `%`-expansion on the
            # quoted column name, which corrupts identifiers containing
            # `%`. SQL has zero bind parameters here too.
            rows = conn.exec_driver_sql(sql).all()
        # The `WHERE col IS NOT NULL` filter guarantees psycopg never
        # returns a NULL row here. Cap raw width before regex/truncation
        # so cost stays bounded; `_SAMPLE_RAW_CAP` is well above any PII
        # length so PII inside the cap is captured fully by `redact_pii`.
        capped = [str(row._mapping["v"])[:_SAMPLE_RAW_CAP] for row in rows]
        # Redact BEFORE truncating to display width. Truncating first lets
        # PII straddle the 50-char boundary and leak partial digits.
        display = tuple(redact_pii(c)[:SAMPLE_TRUNCATE_AT] for c in capped)
        raw_truncated = tuple(c[:SAMPLE_TRUNCATE_AT] for c in capped)
        return display, raw_truncated
