"""Test-internal helpers for the `example_queries` storage primitive.

Two helpers, shared across the store-side and MCP-side test files:

  - `seed_table_for_examples` — register a minimal parent table so
    the `example_queries` FK is satisfied.
  - `insert_example_query` — inject one row via direct SQL.

The mining writer doesn't exist yet in v0.5; these helpers are the
substitute. Once mining lands, this module becomes redundant and the
test files migrate to the real writer — but until then, keeping one
shared definition prevents drift between the two test files.
"""

from __future__ import annotations

from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore


def seed_table_for_examples(store: SQLiteStore, *, source_id: str, schema: str, table: str) -> None:
    """Register a one-column parent table so the `example_queries`
    FK is satisfied. The schema/column shape is intentionally minimal
    — these tests exercise the example-queries surface, not the table
    metadata surface.
    """
    col = Column(
        name="id",
        table_name=table,
        schema_name=schema,
        data_type="INTEGER",
        nullable=False,
        ordinal_position=1,
    )
    store.write_table(
        Table(name=table, schema_name=schema, columns=(col,)),
        source_connection_id=source_id,
    )


def insert_example_query(
    store: SQLiteStore,
    *,
    source_id: str,
    schema: str,
    table: str,
    sql_text: str,
    observation_count: int = 1,
    first_seen_at: int = 1_700_000_000,
    last_seen_at: int = 1_700_000_000,
    source: str = "pg_stat_statements",
    sensitivity: str = "public",
    pii_categories: str = "",
) -> int:
    """Inject one example_queries row directly via SQL; return the new row id."""
    conn = store._require_conn()
    cursor = conn.execute(
        "INSERT INTO example_queries "
        "(source_connection_id, schema_name, table_name, sql_text, "
        "observation_count, first_seen_at, last_seen_at, source, "
        "sensitivity, pii_categories) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            source_id,
            schema,
            table,
            sql_text,
            observation_count,
            first_seen_at,
            last_seen_at,
            source,
            sensitivity,
            pii_categories,
        ),
    )
    conn.commit()
    return cursor.lastrowid
