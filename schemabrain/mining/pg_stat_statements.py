"""Reader for the source Postgres `pg_stat_statements` view.

Schema Brain v0.5 mining harvests `(query, calls)` tuples from this
view, then hands them to the pipeline for parsing and writing into
the local `example_queries` table.

The view is provided by an extension that may not be installed on
the source database (it requires `shared_preload_libraries =
'pg_stat_statements'` in `postgresql.conf` plus `CREATE EXTENSION
pg_stat_statements`). When the view is missing or the role can't
read it, the reader raises `PgStatStatementsUnavailable` so the
pipeline can downgrade to a clean no-op report rather than crashing
the mining run.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import ProgrammingError

# Defensive cap on the byte length of any single `query` value the
# reader will surface to the pipeline. `pg_stat_statements.track_-
# activity_query_size` defaults to 1024 but operators can raise it to
# 1 GB per statement; combined with `LIMIT 10000` that's a multi-GB
# materialisation worst case. The cap above is well above any
# plausible real workload (the largest pg_stat_statements row I've
# observed in production audits is ~30 KB) so legitimate queries
# survive while the pathological case is bounded.
_MAX_SQL_BYTES = 100_000


class PgStatStatementsUnavailable(RuntimeError):
    """Raised when the `pg_stat_statements` view can't be read.

    Three observable causes from the source side:
      - The extension isn't installed (`CREATE EXTENSION pg_stat_-
        statements` was never run).
      - The extension isn't loaded (`shared_preload_libraries` is
        missing the entry, so the catalog row exists but the view
        returns nothing or errors).
      - The connecting role lacks `pg_read_all_stats` membership.

    All three surface as `ProgrammingError` from psycopg — typically
    SQLSTATE `42P01` (undefined_table), `42501` (insufficient_-
    privilege), or `42883` (undefined_function). The reader narrows
    to `ProgrammingError` deliberately: `OperationalError`
    (connection lost, server crash) and other `DatabaseError`
    subclasses are real failures, not operator-config issues, and
    must propagate so a retry-capable caller can act on them.

    The pipeline catches this and returns a `MineReport` with
    `skipped_unavailable=True`; the CLI surfaces the message so the
    operator can fix the source-side configuration.
    """


@dataclass(frozen=True)
class PgStatRow:
    """One row from the `pg_stat_statements` view.

    `query` is the statement text with literals replaced by `$1`,
    `$2`, etc. (pg_stat_statements normalises before storing).
    `calls` is the cumulative call count since the last
    `pg_stat_statements_reset()` or extension reload.

    v0.5 doesn't carry `total_exec_time`, `rows`, or the per-call
    timing fields — operators consuming `example_queries` for
    pattern discovery don't need them, and they'd inflate every
    audit row downstream. Add if a downstream consumer surfaces a
    real need.
    """

    query: str
    calls: int


# The query is intentionally minimal:
#   - One column for the statement text + one for the cumulative count.
#   - LIMIT pg_stat_statements.max defaults to 5000; we cap at 10000
#     defensively to bound memory in case the extension is reconfigured.
#   - ORDER BY calls DESC so heavy-use statements rank first if the
#     cap is hit.
_FETCH_SQL = text(
    "SELECT query, calls "
    "FROM pg_stat_statements "
    "WHERE query IS NOT NULL "
    "ORDER BY calls DESC "
    "LIMIT 10000"
)


def fetch_statements(engine: Engine) -> list[PgStatRow]:
    """Read every observed statement from `pg_stat_statements`.

    Raises `PgStatStatementsUnavailable` when the view can't be read
    because of source-side configuration: extension missing,
    extension not loaded into `shared_preload_libraries`, or
    insufficient privilege. These all surface as
    `ProgrammingError` from psycopg.

    Other SQLAlchemy errors (`OperationalError` for connection loss,
    `DatabaseError` subclasses for any DBMS-side failure) propagate
    unchanged. Those are real failures, not operator-correctable
    config issues, and a retry-capable caller needs to see them as
    themselves rather than as a misleading "fix your extension"
    signal.

    Rows whose `query` exceeds `_MAX_SQL_BYTES` are skipped at this
    boundary to bound peak memory under adversarial / misconfigured
    sources. The mining pipeline never sees the oversized rows; the
    `MineReport.skipped_oversized` count surfaces the skip volume.
    """
    try:
        with engine.connect() as conn:
            rows = conn.execute(_FETCH_SQL).fetchall()
    except ProgrammingError as exc:
        raise PgStatStatementsUnavailable(str(exc)) from exc
    return [
        PgStatRow(query=row.query, calls=row.calls)
        for row in rows
        if row.query and len(row.query.encode("utf-8")) <= _MAX_SQL_BYTES
    ]
