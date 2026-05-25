"""sqlglot wrapper for query log mining.

`extract_table_references(sql)` is the parser primitive. It takes a
raw SQL string — as emitted by `pg_stat_statements`, including its
`$1`/`$2` parameter placeholders — and returns the set of
`(schema, table)` pairs the statement touches. The mining pipeline
uses the result to filter mined rows down to tables that are
actually in the SchemaBrain index.
"""

from __future__ import annotations

import logging
import re

import sqlglot
from sqlglot import expressions as exp
from sqlglot.errors import SqlglotError

_logger = logging.getLogger(__name__)

# sqlglot warns at WARNING level for every statement it can't fit
# into its grammar (`SHOW transaction isolation level`,
# `CREATE EXTENSION ...`, etc.). pg_stat_statements surfaces several
# such statements from SchemaBrain's own connection setup; mining a
# real batch logs one "Falling back to parsing as a 'Command'." line
# per non-DML statement, which is pure noise — the pipeline already
# drops non-DML via `_DML_TYPES`. Raising sqlglot's logger to ERROR
# silences the noise without hiding genuine failures (parse errors
# still surface as exceptions that `extract_table_references` catches
# and logs at DEBUG via the module's own logger).
logging.getLogger("sqlglot").setLevel(logging.ERROR)

# DML statement types we mine. Anything else (DDL, transaction
# control, SET, SHOW, EXPLAIN, …) returns the empty set so the
# pipeline never tries to write a "CREATE TABLE foo" row into
# example_queries. The set is closed at the five DML kinds Postgres
# supports; widening is a deliberate decision.
_DML_TYPES = (exp.Select, exp.Insert, exp.Update, exp.Delete, exp.Merge)

# Profiler-query signatures. These match SchemaBrain's own SELECT
# statements emitted by `schemabrain.profiler.postgres` during
# `schemabrain index`. After indexing, `pg_stat_statements` surfaces
# these alongside real user workload; without this filter,
# `mine-queries` writes them into `example_queries` and
# `get_example_queries` returns SchemaBrain's own profiling chatter
# to agents asking "show me example queries for this table".
#
# Both signatures are deliberately narrow. The counts pattern keys
# on `AS nn_<digit>` positional aliases — a string no human writes
# in real SQL. The sampler pattern requires THREE co-occurring
# markers from `profiler/postgres.py:199-201`: `SELECT DISTINCT`,
# `::text AS v`, and `IS NOT NULL`. Requiring all three rules out
# realistic user-written shapes (a developer poking at an enum with
# `SELECT DISTINCT status::text AS v FROM orders LIMIT 100` lacks
# the `IS NOT NULL` and is preserved).
_PROFILER_COUNTS_PATTERN = re.compile(r"\bAS\s+nn_\d+\b", re.IGNORECASE)
_PROFILER_SAMPLER_PATTERN = re.compile(
    r"SELECT\s+DISTINCT\s+.+?::\s*text\s+AS\s+v\b.+?\bIS\s+NOT\s+NULL\b",
    re.IGNORECASE | re.DOTALL,
)


def extract_table_references(sql: str) -> frozenset[tuple[str | None, str]]:
    """Return the set of `(schema, table)` pairs `sql` references.

    Behaviour:
      - Empty / whitespace-only input → empty set.
      - Parse failure → empty set (logged at DEBUG; one
        unparseable pg_stat_statements row must not blow up the
        batch).
      - Non-DML top-level (DDL, transaction control, SET, SHOW,
        EXPLAIN, …) → empty set.
      - DML: every `Table` node in the AST contributes one tuple.
        Unqualified table references return `(None, name)` so the
        pipeline layer can resolve them against the indexed-tables
        list (the parser does NOT assume any default schema).

    CTE aliases and view names surface as ordinary `Table` references
    when used in the body — pipeline-side filtering against the
    indexed-tables list drops anything that isn't a real table.
    """
    if not sql or not sql.strip():
        return frozenset()
    try:
        parsed = sqlglot.parse_one(sql, read="postgres")
    except SqlglotError as exc:
        # `SqlglotError` is the base class for ParseError +
        # UnsupportedError, the two cases pg_stat_statements actually
        # surfaces. Narrowing past `Exception` means a future sqlglot
        # API break (e.g., signature change → AttributeError on the
        # call site) surfaces as a real failure instead of being
        # silently logged at DEBUG and the row dropped.
        _logger.debug("sqlglot failed to parse statement: %s", exc)
        return frozenset()
    if parsed is None:
        return frozenset()
    if not isinstance(parsed, _DML_TYPES):
        return frozenset()
    result: set[tuple[str | None, str]] = set()
    for table in parsed.find_all(exp.Table):
        # `table.db` is the schema part of `schema.table`; empty
        # string when unqualified. Normalise to None so callers can
        # distinguish "unqualified — resolve against indexed set"
        # from "explicit `'' .table'`" (which would be malformed
        # SQL anyway).
        schema = table.db if table.db else None
        name = table.name
        if name:  # defensive: skip Table nodes with no name
            result.add((schema, name))
    return frozenset(result)


def is_profiler_query(sql: str) -> bool:
    """Return True if `sql` matches SchemaBrain's own profiler signatures.

    Two shapes, both emitted by `schemabrain.profiler.postgres`:

      1. Counts query — null/distinct counts for every column on a
         table. Signature: positional aliases `AS nn_0`, `AS nn_1`,
         etc., which no human-written SQL uses.
      2. Sampler query — distinct value samples for one column.
         Signature: `::text AS v` (single-letter alias on an explicit
         text cast in a `SELECT DISTINCT`), which is similarly
         specific to the profiler.

    Used by `mine_queries` to skip SchemaBrain's own indexing
    chatter that `pg_stat_statements` captures during a `schemabrain
    index` run. Returns False on empty/whitespace input so callers
    can chain it with the parser without an extra emptiness check.

    The match is regex-based, not AST-based, on purpose:
      - The pg_stat_statements normalised form still contains the
        signatures verbatim (only literals get $-substituted).
      - sqlglot occasionally fails to parse profiler-shaped SQL when
        the quoted-identifier escaping interacts with its dialect
        heuristics, and we want the filter to survive parse failure.
    """
    if not sql or not sql.strip():
        return False
    return bool(_PROFILER_COUNTS_PATTERN.search(sql) or _PROFILER_SAMPLER_PATTERN.search(sql))
