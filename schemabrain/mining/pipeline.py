"""Query log mining pipeline — read, parse, filter, UPSERT.

`mine_queries` is the single entry point. It:

  1. Asks the reader for raw `pg_stat_statements` rows. If the reader
     signals unavailability (`PgStatStatementsUnavailable`), returns a
     report with `skipped_unavailable=True` and no writes.
  2. For each row, parses the SQL with `sqlglot` to extract the set
     of `(schema, table)` pairs the statement touches.
  3. Resolves unqualified table references against the indexed-tables
     list from the store. Unambiguous matches survive; ambiguous
     references (same table name in multiple indexed schemas) are
     skipped along with the whole statement (we don't want to record
     a row under the wrong schema).
  4. For each surviving (statement, resolved table) pair, builds an
     `ExampleQuery` row with `source='pg_stat_statements'` and the
     v0.5-safe PII defaults (`sensitivity='public'`,
     `pii_categories=frozenset()`). The PII classifier ships later;
     a future re-mining will reclassify via the UPSERT.
  5. Writes the batch via `store.write_example_queries`. UPSERT
     semantics preserve `first_seen_at` across re-runs.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.engine import Engine

from schemabrain.core.example_query import ExampleQuery
from schemabrain.core.store_protocol import Store
from schemabrain.mining.pg_stat_statements import (
    PgStatRow,
    PgStatStatementsUnavailable,
)
from schemabrain.mining.pg_stat_statements import fetch_statements as _default_fetch
from schemabrain.mining.sql_parse import extract_table_references

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MineReport:
    """Result of one mining run.

    `statements_read` counts every row received from
    `pg_stat_statements`. `statements_used` counts the subset that
    produced at least one `example_queries` row. `rows_written` is
    the sum across statements — a JOIN over three indexed tables in
    one statement contributes 3 to `rows_written` but 1 to
    `statements_used`.

    `skipped_unavailable=True` means the reader couldn't reach the
    view at all (extension missing, permission denied, etc.). The
    other counts are zero in that case.
    """

    statements_read: int
    statements_used: int
    rows_written: int
    skipped_unavailable: bool = False


_FetchFn = Callable[[Engine | None], list[PgStatRow]]


def mine_queries(
    *,
    engine: Engine | None,
    store: Store,
    source_connection_id: str,
    fetch_statements: _FetchFn = _default_fetch,
    now_unix: int | None = None,
) -> MineReport:
    """Run one mining pass.

    `engine` is a SQLAlchemy engine connected to the source database.
    The pipeline never touches it directly — only `fetch_statements`
    does — so test code can inject `None` alongside a fake
    `fetch_statements` callable without violating the contract.

    `now_unix` overrides the timestamp written into `first_seen_at`
    and `last_seen_at` on inserts (UPSERT preserves an existing
    `first_seen_at`). Tests use this to make the rewrite-preserves-
    first-seen-at invariant deterministic; production leaves it
    `None` and `time.time()` provides the value.

    Returns a `MineReport`. Never raises on routine pipeline issues
    (parse failures, table not in index, ambiguous unqualified
    references); does propagate unexpected exceptions raised by the
    store (those signal corruption or a programming error, not
    operator-correctable input).
    """
    if now_unix is None:
        now_unix = int(time.time())

    try:
        raw_rows = fetch_statements(engine)
    except PgStatStatementsUnavailable as exc:
        _logger.warning("pg_stat_statements unavailable: %s", exc)
        return MineReport(
            statements_read=0,
            statements_used=0,
            rows_written=0,
            skipped_unavailable=True,
        )

    # Build the indexed-tables lookup. Two views:
    #   - `indexed_set`: exact (schema, table) match for qualified refs
    #   - `unqualified_map`: name → list of indexed schemas; one entry
    #     means an unqualified reference resolves unambiguously; >1
    #     means ambiguous (skip the reference and the statement).
    indexed_pairs = store.list_tables(source_connection_id=source_connection_id)
    indexed_set: set[tuple[str, str]] = set(indexed_pairs)
    unqualified_map: dict[str, list[str]] = {}
    for schema, name in indexed_pairs:
        unqualified_map.setdefault(name, []).append(schema)

    statements_used = 0
    rows_to_write: list[ExampleQuery] = []

    for raw in raw_rows:
        refs = extract_table_references(raw.query)
        if not refs:
            continue
        resolved = _resolve_references(refs, indexed_set, unqualified_map)
        if not resolved:
            continue
        statements_used += 1
        for schema, table in resolved:
            rows_to_write.append(
                ExampleQuery(
                    schema_name=schema,
                    table_name=table,
                    sql_text=raw.query,
                    observation_count=raw.calls,
                    first_seen_at=now_unix,
                    last_seen_at=now_unix,
                    source="pg_stat_statements",
                    # v0.5 defaults; PII classifier reclassifies later
                    # via re-mining UPSERT.
                    sensitivity="public",
                    pii_categories=frozenset(),
                )
            )

    rows_written = store.write_example_queries(
        rows_to_write, source_connection_id=source_connection_id
    )
    return MineReport(
        statements_read=len(raw_rows),
        statements_used=statements_used,
        rows_written=rows_written,
        skipped_unavailable=False,
    )


def _resolve_references(
    refs: frozenset[tuple[str | None, str]],
    indexed_set: set[tuple[str, str]],
    unqualified_map: dict[str, list[str]],
) -> set[tuple[str, str]]:
    """Resolve each `(schema, table)` reference against the indexed set.

    Qualified references must exactly match an indexed pair to
    survive. Unqualified references (`schema is None`) survive only
    when the table name appears in exactly one indexed schema —
    multiple matches make the reference ambiguous, and we drop it
    rather than guess.
    """
    resolved: set[tuple[str, str]] = set()
    for schema, table in refs:
        if schema is None:
            schemas = unqualified_map.get(table, [])
            if len(schemas) == 1:
                resolved.add((schemas[0], table))
            elif len(schemas) > 1:
                # Ambiguous unqualified ref. We refuse to guess so
                # the row doesn't land under the wrong schema. DEBUG
                # so a contributor debugging "why are my queries
                # missing?" can see the resolution attempt without
                # flooding stderr on a busy mining run.
                _logger.debug(
                    "ambiguous unqualified table ref %r matches schemas %s — skipping",
                    table,
                    schemas,
                )
            # len(schemas) == 0 → table just isn't indexed; not a
            # diagnostic-worthy signal at any severity.
            continue
        if (schema, table) in indexed_set:
            resolved.add((schema, table))
    return resolved
