"""Cache-aware indexing workflow.

`index()` is the single function that ties together a `DataSource`, a
`Profiler`, an optional `EnrichmentPipeline`, and a `SQLiteStore` to
introspect a database, diff against cached fingerprints, profile +
enrich only what changed, and persist the new state.

The central invariant: re-running `index()` on a schema that hasn't
changed performs zero profile queries AND zero LLM calls. The profiler
and the pipeline are only invoked for tables whose structural
fingerprint differs from the cache.

A table is considered unchanged iff:
  - its column set is identical to the cached one, AND
  - every column's structural fingerprint matches the cached one.

Otherwise the whole table is re-profiled, re-enriched (if a pipeline
was supplied), and re-persisted.

`ordinal_position` is part of the structural fingerprint, so a column
reorder triggers re-work even with no name or type change.

**Atomicity caveat:** the per-table flow `store.write_table()` then
`store.write_table_fingerprints()` then `store.write_table_descriptions()`
runs in three separate SQLite transactions. If the process is killed
between them, the table row exists with no fingerprints/descriptions.
The next `index()` run sees an empty fingerprint set, treats every
column as added, and re-profiles + re-enriches — correct recovery, just
not free. Acceptable for v0; if it ever matters, merge into a single
store-level method that opens one transaction.

**Cost-cap behavior:** the per-table flow runs `profile → enrich (all
columns) → write_table → write_fingerprints → write_descriptions` in
that order, so a `CostCapExceeded` raised during enrichment leaves the
cache exactly as it was before this table started — nothing is
persisted for the in-flight table. The next run will see the table as
still-changed (cache miss) and retry. Tables fully enriched before the
cap tripped keep their descriptions and fingerprints.

**Semantic-fingerprint comparison gap:** today the diff loop still
compares ONLY structural fingerprints. A bare `PROMPT_VERSION` bump on
an unchanged schema does not yet trigger re-enrichment. Wiring this in
properly requires storing a separate `prompt_version` column so we can
detect drift without re-computing the semantic hash from scratch
(which would require re-profiling). Deferred — sample drift detection
needs the same plumbing.
"""

from __future__ import annotations

from dataclasses import dataclass

from schemabrain.connectors.base import DataSource
from schemabrain.core.description import ColumnDescription
from schemabrain.core.fingerprint import (
    column_semantic_fingerprint,
    column_structural_fingerprint,
    fk_targets_for_column,
)
from schemabrain.core.store import SQLiteStore
from schemabrain.enrichment.pipeline import EnrichmentPipeline
from schemabrain.enrichment.prompts import PROMPT_VERSION
from schemabrain.profiler.base import Profiler


@dataclass(frozen=True)
class IndexResult:
    """Outcome of a single `index()` call.

    `tables_seen` = `tables_changed + tables_unchanged`. `tables_removed`
    is reported separately because removed tables aren't in the source
    iteration.

    `descriptions_generated` is the count of NEW LLM calls made this
    run (zero if no pipeline was supplied or no tables changed).
    `llm_cost_usd` is the cumulative cost of those calls.
    """

    tables_seen: int
    tables_changed: int
    tables_unchanged: int
    tables_removed: int
    columns_added: int
    columns_changed: int
    columns_removed: int
    descriptions_generated: int = 0
    llm_cost_usd: float = 0.0

    def summary(self) -> str:
        base = (
            f"Indexed {self.tables_seen} table(s): "
            f"{self.tables_changed} changed, "
            f"{self.tables_unchanged} unchanged, "
            f"{self.tables_removed} removed. "
            f"Columns: +{self.columns_added}/~{self.columns_changed}/-{self.columns_removed}"
        )
        if self.descriptions_generated > 0 or self.llm_cost_usd > 0:
            base += f". LLM: {self.descriptions_generated} descriptions (${self.llm_cost_usd:.4f})"
        return base


def index(
    *,
    source: DataSource,
    profiler: Profiler,
    store: SQLiteStore,
    source_connection_id: str,
    pipeline: EnrichmentPipeline | None = None,
) -> IndexResult:
    """Perform one cache-aware indexing pass and return the diff summary.

    If `pipeline` is supplied, every column in a re-profiled table is
    enriched with an LLM-generated description and the result persisted
    to `column_descriptions`. If `pipeline` is `None`, profiling and
    fingerprinting still happen but no LLM calls are made.
    """
    current_tables = source.list_tables()
    cached_tables = store.list_tables(source_connection_id=source_connection_id)

    # Tables present in cache but absent from source: drop them. The
    # cascade on `tables` removes their columns, FKs, fingerprints, AND
    # descriptions in one shot.
    removed_tables = set(cached_tables) - set(current_tables)
    for schema, name in removed_tables:
        store.delete_table(schema, name, source_connection_id=source_connection_id)

    tables_changed = 0
    tables_unchanged = 0
    columns_added = 0
    columns_changed = 0
    columns_removed = 0
    descriptions_generated = 0

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

        # Enrich BEFORE writing fingerprints. If `enrich_column` raises
        # (CostCapExceeded, network error, etc.) we want the cache to
        # remain in its pre-table state so the next run's diff still
        # marks this table as changed and retries. Profiling the source
        # again is cheap; losing the description-cache invariant is not.
        descriptions: dict[str, ColumnDescription] = {}
        if pipeline is not None:
            for col in table.columns:
                desc = pipeline.enrich_column(
                    table=table,
                    column=col,
                    stats=stats.get(col.name),
                    fk_targets=fk_targets_for_column(table, col.name),
                )
                descriptions[col.name] = desc
                descriptions_generated += 1

        # Now persist. write_table cascades to delete any stale
        # fingerprints/descriptions; the two writes that follow
        # repopulate them.
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
        if descriptions:
            store.write_table_descriptions(
                schema,
                name,
                source_connection_id=source_connection_id,
                descriptions=descriptions,
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
        descriptions_generated=descriptions_generated,
        llm_cost_usd=pipeline.spent_usd if pipeline is not None else 0.0,
    )
