"""Cache-aware indexing workflow.

`index()` is the single function that ties together a `DataSource`, a
`Profiler`, and a `SQLiteStore` to introspect a database, diff against
cached fingerprints, profile only what changed, and persist the new state.

The central invariant: re-running `index()` on a schema that hasn't
changed performs zero profile queries (the profiler is never called for
unchanged tables). This is what makes re-indexing free — both in
warehouse query cost and in the future Week 3 LLM enrichment cost.

A table is considered unchanged iff:
  - its column set is identical to the cached one, AND
  - every column's structural fingerprint matches the cached one.

Otherwise the whole table is re-profiled and re-fingerprinted.

`ordinal_position` is part of the structural fingerprint, so a column
reorder triggers a re-profile even with no name or type change. That is
deliberate — Postgres lays out the table differently and positional
queries (`SELECT column1, column2 FROM ...`) behave differently.

**Atomicity caveat:** the per-table flow `store.write_table()` then
`store.write_table_fingerprints()` runs in two separate SQLite
transactions. If the process is killed between them, the table row
exists with no fingerprint rows. The next `index()` run sees an empty
fingerprint set, treats every column as added, and re-profiles — which
is correct recovery, just not free. Acceptable for v0; if it ever
matters, merge both writes into a single store-level method that opens
one transaction.

**Semantic-fingerprint comparison gap:** today the diff loop compares
ONLY structural fingerprints. The semantic fingerprint (which embeds
`PROMPT_VERSION`) is computed and stored at write time, so the cached
hash always reflects the prompt version at the time of writing. But a
bare `PROMPT_VERSION` bump on an unchanged schema does NOT yet trigger
re-work — the indexer skips structural-match tables entirely. Wiring
the semantic-fp comparison into the diff (and the corresponding
"re-enrich without re-profile" path) lands with the LLM enrichment
pipeline in the next slice; today's behavior would otherwise re-profile
from Postgres on every prompt bump, which we don't want.
"""

from __future__ import annotations

from dataclasses import dataclass

from schemabrain.connectors.base import DataSource
from schemabrain.core.fingerprint import (
    column_semantic_fingerprint,
    column_structural_fingerprint,
    fk_targets_for_column,
)
from schemabrain.core.store import SQLiteStore
from schemabrain.enrichment.prompts import PROMPT_VERSION
from schemabrain.profiler.base import Profiler


@dataclass(frozen=True)
class IndexResult:
    """Outcome of a single `index()` call.

    `tables_seen` = `tables_changed + tables_unchanged`. `tables_removed`
    is reported separately because removed tables aren't in the source
    iteration.
    """

    tables_seen: int
    tables_changed: int
    tables_unchanged: int
    tables_removed: int
    columns_added: int
    columns_changed: int
    columns_removed: int

    def summary(self) -> str:
        return (
            f"Indexed {self.tables_seen} table(s): "
            f"{self.tables_changed} changed, "
            f"{self.tables_unchanged} unchanged, "
            f"{self.tables_removed} removed. "
            f"Columns: +{self.columns_added}/~{self.columns_changed}/-{self.columns_removed}"
        )


def index(
    *,
    source: DataSource,
    profiler: Profiler,
    store: SQLiteStore,
    source_connection_id: str,
) -> IndexResult:
    """Perform one cache-aware indexing pass and return the diff summary."""
    current_tables = source.list_tables()
    cached_tables = store.list_tables(source_connection_id=source_connection_id)

    # Tables present in cache but absent from source: drop them. The
    # cascade on `tables` removes their columns, FKs, and fingerprints.
    removed_tables = set(cached_tables) - set(current_tables)
    for schema, name in removed_tables:
        store.delete_table(schema, name, source_connection_id=source_connection_id)

    tables_changed = 0
    tables_unchanged = 0
    columns_added = 0
    columns_changed = 0
    columns_removed = 0

    for schema, name in current_tables:
        table = source.get_table(name, schema)
        cached_fps = store.get_table_fingerprints(
            schema, name, source_connection_id=source_connection_id
        )
        cached_structural = {col_name: fps[0] for col_name, fps in cached_fps.items()}

        new_structural = {
            col.name: column_structural_fingerprint(col, fk_targets_for_column(table, col.name))
            for col in table.columns
        }

        col_added = set(new_structural) - set(cached_structural)
        col_removed = set(cached_structural) - set(new_structural)
        col_changed = {
            col_name
            for col_name in new_structural
            if col_name in cached_structural
            and new_structural[col_name] != cached_structural[col_name]
        }

        if not (col_added or col_removed or col_changed):
            tables_unchanged += 1
            continue

        # Something changed — re-profile the WHOLE table. Per-column
        # profile granularity is a possible future optimization; for v0
        # the table-level cost is acceptable and the code stays simple.
        stats = profiler.profile_table(table)
        store.write_table(table, source_connection_id=source_connection_id)
        new_fingerprints = {
            col.name: (
                new_structural[col.name],
                column_semantic_fingerprint(
                    col,
                    fk_targets_for_column(table, col.name),
                    stats.get(col.name),
                    PROMPT_VERSION,
                ),
            )
            for col in table.columns
        }
        store.write_table_fingerprints(
            schema,
            name,
            source_connection_id=source_connection_id,
            fingerprints=new_fingerprints,
        )

        tables_changed += 1
        columns_added += len(col_added)
        columns_changed += len(col_changed)
        columns_removed += len(col_removed)

    return IndexResult(
        tables_seen=len(current_tables),
        tables_changed=tables_changed,
        tables_unchanged=tables_unchanged,
        tables_removed=len(removed_tables),
        columns_added=columns_added,
        columns_changed=columns_changed,
        columns_removed=columns_removed,
    )
