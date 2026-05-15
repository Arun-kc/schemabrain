"""sqlglot wrapper for query log mining.

`extract_table_references(sql)` is the parser primitive. It takes a
raw SQL string — as emitted by `pg_stat_statements`, including its
`$1`/`$2` parameter placeholders — and returns the set of
`(schema, table)` pairs the statement touches. The mining pipeline
uses the result to filter mined rows down to tables that are
actually in the Schema Brain index.
"""

from __future__ import annotations

import logging

import sqlglot
from sqlglot import expressions as exp
from sqlglot.errors import SqlglotError

_logger = logging.getLogger(__name__)

# DML statement types we mine. Anything else (DDL, transaction
# control, SET, SHOW, EXPLAIN, …) returns the empty set so the
# pipeline never tries to write a "CREATE TABLE foo" row into
# example_queries. The set is closed at the five DML kinds Postgres
# supports; widening is a deliberate decision.
_DML_TYPES = (exp.Select, exp.Insert, exp.Update, exp.Delete, exp.Merge)


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
