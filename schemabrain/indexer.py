"""Cache-aware indexing workflow.

`index()` is the single function that ties together a `DataSource`, a
`Profiler`, an optional `EnrichmentPipeline`, and a `Store` (the
Protocol; `SQLiteStore` is the v1 concrete) to introspect a database,
diff against cached fingerprints, profile + enrich only what changed,
and persist the new state.

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

import asyncio
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
from schemabrain.core.store_protocol import Store
from schemabrain.enrichment.embeddings import Embedder, embedding_for
from schemabrain.enrichment.pipeline import EnrichmentPipeline
from schemabrain.enrichment.prompts import PROMPT_VERSION
from schemabrain.pii import classify_column, classify_pii_confidence
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

    def summary(self, *, dry_run: bool = False) -> str:
        """Render a one-line human summary of the run.

        `dry_run=True` rewrites the verb tense and prefixes "Estimated"
        on the LLM/embeddings sub-clauses so the reader can't confuse a
        preview with a real run. Counts and dollar amounts are the same
        — only the framing changes.
        """
        verb = "Would index" if dry_run else "Indexed"
        base = (
            f"{verb} {self.tables_seen} table(s): "
            f"{self.tables_changed} changed, "
            f"{self.tables_unchanged} unchanged, "
            f"{self.tables_removed} removed. "
            f"Columns: +{self.columns_added}/~{self.columns_changed}/-{self.columns_removed}"
        )
        if self.descriptions_generated > 0:
            # Happy path: descriptions generated this run. Show the
            # cost and count. NOTE: `llm_cost_usd` carries the
            # cumulative ledger total (pipeline initialises it from
            # the persistent ledger), so on a partial run this
            # number is run-cumulative — not run-delta. That's
            # acceptable when descriptions > 0 because the user
            # actively spent some of it this run.
            llm_label = "Estimated LLM" if dry_run else "LLM"
            base += (
                f". {llm_label}: {self.descriptions_generated} descriptions "
                f"(${self.llm_cost_usd:.4f})"
            )
        elif not dry_run and self.llm_cost_usd > 0 and self.tables_changed > 0:
            # Anomaly: tables changed (so enrichment was attempted)
            # AND spend is non-zero AND no descriptions landed. The
            # cache-hit case is excluded by the `tables_changed > 0`
            # guard — a clean re-index has tables_changed == 0 and
            # stays silent on the LLM line. What's left is the rare
            # path where LLM calls billed but the pipeline didn't
            # produce a usable description; the operator needs a
            # breadcrumb to investigate, not silent success.
            base += f". LLM: 0 descriptions generated (spend ${self.llm_cost_usd:.4f} — check logs)"
        if self.embeddings_generated > 0:
            embed_label = "Estimated embeddings" if dry_run else "Embeddings"
            base += f". {embed_label}: {self.embeddings_generated}"
        if dry_run:
            base += ". No changes made to the store."
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
    store: Store,
    source_connection_id: str,
    pipeline: EnrichmentPipeline | None = None,
    embedder: Embedder | None = None,
    reporter: IndexReporter | None = None,
    no_pii_classify: bool = False,
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

    If `no_pii_classify=True`, the heuristic PII classifier does NOT
    run for re-profiled tables, and any existing `column_pii_tags`
    rows for those tables are wiped (leaving stale tags would be
    worse than no tags). Tables in cache that did not change this
    run retain their existing tags. The CLI surfaces this via the
    `--no-pii-classify` flag with a one-shot stderr warning.
    """
    if reporter is None:
        reporter = NullReporter()

    current_tables = source.list_tables()
    cached_tables = store.list_tables(source_connection_id=source_connection_id)

    reporter.on_start(total_tables=len(current_tables))

    # Tables present in cache but absent from source: drop them. The
    # cascade on `tables` removes their columns, FKs, fingerprints, AND
    # descriptions in one shot. `column_pii_tags` has no FK to `tables`
    # (qualified_table form differs from the split form `tables`
    # uses), so we wipe its rows for the dropped table explicitly.
    removed_tables = set(cached_tables) - set(current_tables)
    for schema, name in removed_tables:
        store.delete_table(schema, name, source_connection_id=source_connection_id)
        store.write_column_pii_tags(
            source_connection_id=source_connection_id,
            qualified_table=f"{schema}.{name}",
            tags={},
        )
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

        # Enrich BEFORE writing fingerprints. If enrichment raises
        # (CostCapExceeded, network error, etc.) we want the cache to
        # remain in its pre-table state so the next run's diff still
        # marks this table as changed and retries. Profiling the source
        # again is cheap; losing the description-cache invariant is not.
        #
        # Async batch via `enrich_columns_async`: all columns for this
        # table run concurrently (bounded by the pipeline's per-tier
        # semaphores). For a 30-column table at ~600ms per LLM call,
        # this turns ~18s of serial latency into ~3s wall-clock under
        # default Haiku concurrency=8. `asyncio.run` opens and closes
        # a fresh event loop per table — fine here because tables are
        # processed sequentially in the outer loop, and a per-table
        # loop scope is the cleanest place to cancel pending tasks if
        # one column raises.
        descriptions: dict[str, ColumnDescription] = {}
        if pipeline is not None:
            stats_by_col = dict(stats)
            fk_targets_by_col = {
                col.name: fk_targets_for_column(table, col.name) for col in table.columns
            }
            descriptions = asyncio.run(
                pipeline.enrich_columns_async(
                    table=table,
                    columns=list(table.columns),
                    stats_by_col=stats_by_col,
                    fk_targets_by_col=fk_targets_by_col,
                )
            )
            descriptions_generated += len(descriptions)
            for col in table.columns:
                if col.name in descriptions:
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

        # Capture the cheap row-count estimate (pg_class.reltuples; None
        # when the backend can't estimate or the table is never-analyzed)
        # only for tables we're about to (re)write — the unchanged tables
        # `continue`d above, so this adds no catalog query to a no-op
        # re-index. The estimate refreshes on every structural change.
        estimated_row_count = source.estimated_row_count(name, schema)

        # Now persist. write_table cascades to delete any stale
        # fingerprints/descriptions/embeddings; the writes that follow
        # repopulate them.
        store.write_table(
            table,
            source_connection_id=source_connection_id,
            estimated_row_count=estimated_row_count,
        )
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

        # PII tags. With `--no-pii-classify`, we still write an empty
        # mapping so any existing heuristic tags for the table are
        # cleared atomically — never leaving stale tags from a prior
        # classifier run. With classification enabled (the default),
        # the heuristic produces (sensitivity, categories) per column
        # from name + shape signal and the bulk write replaces all
        # rows for the table.
        qualified_table = f"{schema}.{name}"
        if no_pii_classify:
            store.write_column_pii_tags(
                source_connection_id=source_connection_id,
                qualified_table=qualified_table,
                tags={},
            )
        else:
            classified = {
                col.name: classify_column(
                    col.name,
                    column_type=col.data_type,
                    is_primary_key=col.is_primary_key,
                    table_name=table.name,
                    shape_patterns=stats[col.name].shape_patterns if col.name in stats else (),
                )
                for col in table.columns
            }
            # ADR 0009: derive the advisory PII-matrix confidence band
            # from the matched categories + the same raw value-shape
            # signal, at index time while the shapes are still in
            # memory. Keyed by the same column names as `classified` so
            # the store writes both in one atomic swap.
            confidence = {
                col.name: classify_pii_confidence(
                    classified[col.name][1],
                    stats[col.name].shape_patterns if col.name in stats else (),
                )
                for col in table.columns
            }
            store.write_column_pii_tags(
                source_connection_id=source_connection_id,
                qualified_table=qualified_table,
                tags=classified,
                confidence=confidence,
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
    # Silent-failure protection for the `--no-pii-classify` partial
    # state: classifier is off for re-profiled tables but unchanged
    # tables retain whatever heuristic tags exist from a prior run.
    # Without this warning the partial mix is invisible.
    if no_pii_classify and tables_unchanged > 0:
        import sys as _sys

        print(
            f"warning: --no-pii-classify wiped tags for re-profiled tables, "
            f"but {tables_unchanged} unchanged table(s) retain any PII tags "
            f"from prior runs. Re-index those tables explicitly (e.g. by "
            f"forcing a column change) to wipe their tags too.",
            file=_sys.stderr,
        )
    reporter.on_finish(result=result)
    return result


# Measured anchor for per-column LLM cost on Haiku 4.5, taken at
# PROMPT_VERSION 2026-05-11-1. (The 2026-06-08-1 PII sample-gate only
# drops sample-value lines for classifier-flagged columns, so the real
# per-column cost is unchanged or slightly lower than this anchor.) Two
# real-world samples produced near-identical per-column costs:
#   - ecommerce fixture: $0.0074 / 24 cols = $0.000308/col
#   - Pagila (partition-filtered): $0.0299 / 87 cols = $0.000344/col
# Rounded to one significant figure both for honest "approximate"
# framing and so a minor prompt tweak doesn't immediately invalidate
# the constant. If `--enable-sonnet` is on, real cost will exceed this
# (Sonnet is ~5x Haiku) — dry-run currently ignores tier routing and
# reports the Haiku estimate. Documented as a known under-estimate
# in the CLI help for `--dry-run`.
_AVG_COST_PER_COLUMN_USD = 0.0003


def dry_run_index(
    *,
    source: DataSource,
    store: Store,
    source_connection_id: str,
    will_enrich: bool,
    will_embed: bool,
    reporter: IndexReporter | None = None,
    avg_cost_per_column_usd: float = _AVG_COST_PER_COLUMN_USD,
) -> IndexResult:
    """Compute what `index()` would do, without doing it.

    Walks the same diff loop as `index()` — introspects the source,
    reads cached fingerprints, computes per-column structural deltas —
    but skips every side effect:
      - no `profiler.profile_table` (no sample queries against source)
      - no `pipeline.enrich_column` (no LLM calls, no cost incurred)
      - no embedder (no fastembed ONNX init)
      - no `store.write_table*` and no `store.delete_table` (the store
        is read-only by discipline during dry-run; callers don't need
        to swap to a read-only handle)

    Reporter lifecycle callbacks fire exactly as they would in a real
    run, so the same `RichReporter` renders progress. `on_column_enriched`
    fires with a CUMULATIVE estimated cost (descriptions_so_far * rate),
    matching the shape the cost ticker expects in real runs.

    Returns an `IndexResult` with `descriptions_generated` and
    `embeddings_generated` populated as if `index()` had run, and
    `llm_cost_usd` populated from the measured per-column constant.
    Caller renders with `result.summary(dry_run=True)` to make the
    estimated-not-actual framing explicit.

    `will_enrich` / `will_embed` are the real-run flags — caller
    derives them from `--no-enrich` / `--no-embed` in the CLI. They
    drive the cost estimate (will_enrich=False → 0 cost) and the
    reported description / embedding counts.
    """
    if reporter is None:
        reporter = NullReporter()

    current_tables = source.list_tables()
    cached_tables = store.list_tables(source_connection_id=source_connection_id)

    reporter.on_start(total_tables=len(current_tables))

    # Count tables that WOULD be removed — but don't actually delete.
    # The diff logic mirrors `index()`'s removed_tables computation so
    # the reported count matches what a real run would do.
    removed_tables = set(cached_tables) - set(current_tables)
    reporter.on_tables_removed(removed=len(removed_tables))

    tables_changed = 0
    tables_unchanged = 0
    columns_added = 0
    columns_changed = 0
    columns_removed = 0
    descriptions_would_generate = 0
    embeddings_would_generate = 0

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

        columns_to_enrich = len(table.columns) if will_enrich else 0
        reporter.on_table_start(schema=schema, name=name, columns_to_enrich=columns_to_enrich)

        if will_enrich:
            for col in table.columns:
                descriptions_would_generate += 1
                estimated_spent = descriptions_would_generate * avg_cost_per_column_usd
                reporter.on_column_enriched(
                    schema=schema,
                    name=name,
                    column=col.name,
                    spent_usd=estimated_spent,
                )

        if will_enrich and will_embed:
            embeddings_would_generate += len(table.columns)

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
        descriptions_generated=descriptions_would_generate,
        llm_cost_usd=descriptions_would_generate * avg_cost_per_column_usd,
        embeddings_generated=embeddings_would_generate,
    )
    reporter.on_finish(result=result)
    return result
