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
then `store.write_table_embeddings()` runs in four separate SQLite
transactions. If the process is killed between (1) and (2), the table
row exists with no fingerprints/descriptions/embeddings; the next run
sees an empty fingerprint set, treats every column as added, and
re-profiles + re-enriches + re-embeds — correct recovery, just not
free. The asymmetric failure window is between (3) and (4): if the
process dies after descriptions are persisted but before embeddings,
the next run sees a fingerprint hit and skips the table — descriptions
exist, embeddings don't, gap is permanent until a real schema change
triggers re-enrichment. Local ONNX embedder calls are essentially
infallible so this window is theoretical; if remote embedders are ever
wired in, revisit. Acceptable for v0; if it ever matters, merge into a
single store-level method that opens one transaction.

**Cost-cap behavior:** the per-table flow runs `profile → enrich (all
columns) → embed (all columns) → write_table → write_fingerprints →
write_descriptions → write_embeddings` in that order, so a
`CostCapExceeded` raised during enrichment leaves the cache exactly as
it was before this table started — nothing is persisted for the
in-flight table. The next run will see the table as still-changed
(cache miss) and retry. Tables fully enriched before the cap tripped
keep their descriptions, fingerprints, and embeddings.

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
from typing import Protocol

from schemabrain.connectors.base import DataSource
from schemabrain.core.description import ColumnDescription
from schemabrain.core.embedding import ColumnEmbedding
from schemabrain.core.fingerprint import (
    column_semantic_fingerprint,
    column_structural_fingerprint,
    fk_targets_for_column,
)
from schemabrain.core.store import SQLiteStore
from schemabrain.enrichment.embeddings import Embedder, embedding_for
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

    `embeddings_generated` is the count of NEW local-embedding calls
    made this run (zero if no embedder was supplied or no tables
    changed). Local embeddings are free at runtime so there's no cost
    field.
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
    embeddings_generated: int = 0

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
        if self.embeddings_generated > 0:
            base += f". Embeddings: {self.embeddings_generated}"
        return base


class IndexReporter(Protocol):
    """Observer hook into `index()` execution for progress UIs.

    Implementations receive lifecycle callbacks in this order:
      on_start → on_tables_removed → (per table: on_table_unchanged
      OR on_table_start → 0..N on_column_enriched → on_table_done)
      → on_finish.

    `close()` is the unconditional teardown hook. The CLI calls it in a
    `finally` after `index()` regardless of whether the run finished
    cleanly, raised `CostCapExceeded`, was interrupted by Ctrl-C, or
    failed for any other reason. Implementations MUST treat `close()`
    as idempotent — repeated calls are a no-op — and MUST tolerate
    being called when `on_start` was never reached (e.g. a connection
    error before `index()` got that far).

    Reporters MUST be cheap — they're called on the hot path. They MUST
    NOT raise; an exception from a reporter aborts the entire indexing
    run. They should also be safe to drop on the floor: any reporter
    must remain compatible with `index()` calls that skip some
    callbacks (e.g. no `on_column_enriched` when `pipeline is None`).
    """

    def on_start(self, *, total_tables: int) -> None: ...
    def on_tables_removed(self, *, removed: int) -> None: ...
    def on_table_unchanged(self, *, schema: str, name: str) -> None: ...
    def on_table_start(self, *, schema: str, name: str, columns_to_enrich: int) -> None: ...
    def on_column_enriched(
        self, *, schema: str, name: str, column: str, spent_usd: float
    ) -> None: ...
    def on_table_done(self, *, schema: str, name: str) -> None: ...
    def on_finish(self, *, result: IndexResult) -> None: ...
    def close(self) -> None: ...


class NullReporter:
    """No-op IndexReporter. Default when no UI is wired up."""

    def on_start(self, *, total_tables: int) -> None:
        pass

    def on_tables_removed(self, *, removed: int) -> None:
        pass

    def on_table_unchanged(self, *, schema: str, name: str) -> None:
        pass

    def on_table_start(self, *, schema: str, name: str, columns_to_enrich: int) -> None:
        pass

    def on_column_enriched(self, *, schema: str, name: str, column: str, spent_usd: float) -> None:
        pass

    def on_table_done(self, *, schema: str, name: str) -> None:
        pass

    def on_finish(self, *, result: IndexResult) -> None:
        pass

    def close(self) -> None:
        pass


def index(
    *,
    source: DataSource,
    profiler: Profiler,
    store: SQLiteStore,
    source_connection_id: str,
    pipeline: EnrichmentPipeline | None = None,
    embedder: Embedder | None = None,
    reporter: IndexReporter | None = None,
) -> IndexResult:
    """Perform one cache-aware indexing pass and return the diff summary.

    If `pipeline` is supplied, every column in a re-profiled table is
    enriched with an LLM-generated description and the result persisted
    to `column_descriptions`. If `pipeline` is `None`, profiling and
    fingerprinting still happen but no LLM calls are made.

    If both `pipeline` and `embedder` are supplied, every column whose
    description is generated this run is also embedded into a local
    vector and persisted to `column_embeddings`. An `embedder` without a
    `pipeline` is a no-op for embeddings (there are no descriptions to
    embed) — backfill of embeddings for previously-enriched columns is a
    future slice.

    If `reporter` is supplied, lifecycle callbacks are emitted as
    indexing progresses; see `IndexReporter`. Default is a no-op.
    """
    if reporter is None:
        reporter = NullReporter()

    current_tables = source.list_tables()
    cached_tables = store.list_tables(source_connection_id=source_connection_id)

    reporter.on_start(total_tables=len(current_tables))

    # Tables present in cache but absent from source: drop them. The
    # cascade on `tables` removes their columns, FKs, fingerprints, AND
    # descriptions in one shot.
    removed_tables = set(cached_tables) - set(current_tables)
    for schema, name in removed_tables:
        store.delete_table(schema, name, source_connection_id=source_connection_id)
    reporter.on_tables_removed(removed=len(removed_tables))

    tables_changed = 0
    tables_unchanged = 0
    columns_added = 0
    columns_changed = 0
    columns_removed = 0
    descriptions_generated = 0
    embeddings_generated = 0

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
            reporter.on_table_unchanged(schema=schema, name=name)
            continue

        columns_to_enrich = len(table.columns) if pipeline is not None else 0
        reporter.on_table_start(schema=schema, name=name, columns_to_enrich=columns_to_enrich)

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
                reporter.on_column_enriched(
                    schema=schema,
                    name=name,
                    column=col.name,
                    spent_usd=pipeline.spent_usd,
                )

        # Embed AFTER all descriptions for the table are in hand. Done in
        # this order so a partial failure inside enrichment never leaves
        # half-embedded tables on disk — the same atomic-table guarantee
        # we already give for descriptions+fingerprints. Embedder calls
        # are local + free; if a future remote embedder is wired in,
        # revisit whether each call should also check a cost-cap.
        embeddings: dict[str, ColumnEmbedding] = {}
        if embedder is not None and descriptions:
            for col_name, desc in descriptions.items():
                embeddings[col_name] = embedding_for(desc.text, embedder=embedder)
                embeddings_generated += 1

        # Now persist. write_table cascades to delete any stale
        # fingerprints/descriptions/embeddings; the writes that follow
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
        if embeddings:
            store.write_table_embeddings(
                schema,
                name,
                source_connection_id=source_connection_id,
                embeddings=embeddings,
            )

        tables_changed += 1
        columns_added += len(col_added)
        columns_changed += len(col_changed)
        columns_removed += len(col_removed)
        reporter.on_table_done(schema=schema, name=name)

    result = IndexResult(
        tables_seen=len(current_tables),
        tables_changed=tables_changed,
        tables_unchanged=tables_unchanged,
        tables_removed=len(removed_tables),
        columns_added=columns_added,
        columns_changed=columns_changed,
        columns_removed=columns_removed,
        descriptions_generated=descriptions_generated,
        llm_cost_usd=pipeline.spent_usd if pipeline is not None else 0.0,
        embeddings_generated=embeddings_generated,
    )
    reporter.on_finish(result=result)
    return result
