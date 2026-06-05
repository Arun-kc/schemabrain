"""Store Protocol — the universal persistence boundary.

Every layer that reads or writes indexed schema metadata depends on
this Protocol, not on a concrete implementation. v1 ships
`SQLiteStore`; future stores (in-memory mocks for unit tests, alternative
backends for v3 hosted) drop in by satisfying this interface.

The Protocol is `@runtime_checkable` so the test suite can assert
that a concrete class conforms via `isinstance(store, Store)`. The
check is structural — it verifies the methods are present by name,
NOT that signatures match. Signature drift between the Protocol and
`SQLiteStore` is caught by the type checker (and would surface as a
test failure on the first call that exercises the divergence).

Forward-compat shape: this Protocol will additively grow as Schema
Brain learns to persist new model classes (semantic repository,
audit log writer, etc.). Existing methods stay stable; new methods
slot in without breaking callers.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Protocol, runtime_checkable

from schemabrain.core.description import ColumnDescription
from schemabrain.core.embedding import ColumnEmbedding
from schemabrain.core.entity import Entity
from schemabrain.core.example_query import ExampleQuery
from schemabrain.core.graph import GraphEdge, GraphNode
from schemabrain.core.join import CanonicalJoin
from schemabrain.core.metric import Metric
from schemabrain.core.models import ForeignKey, IncomingForeignKey, Table
from schemabrain.pii.categories import (
    ColumnPiiTag,
    PIICategory,
    PiiConfidenceBand,
    Sensitivity,
)


@runtime_checkable
class Store(Protocol):  # pragma: no cover
    """The persistence boundary for SchemaBrain.

    Protocol class — method bodies are ellipses, never executed at
    runtime; coverage tracks the concrete `SQLiteStore` instead.

    See module docstring for the broader rationale. Method docs are
    kept terse here because the concrete implementation
    (`SQLiteStore`) carries the authoritative documentation — this
    Protocol mirrors its public surface.
    """

    # ----- Configuration -------------------------------------------

    @property
    def writer_lock(self) -> bool:
        """`True` if the store serialises writes across processes.

        Today, the cost-ledger write path honours this flag with
        `BEGIN IMMEDIATE`; callers can read it to branch on whether
        cross-process safety is in effect (e.g., for the async
        enrichment dispatcher).
        """
        ...

    # ----- Lifecycle ------------------------------------------------

    def __enter__(self) -> Store: ...

    def __exit__(self, *args: object) -> None: ...

    def close(self) -> None: ...

    # ----- Tables ---------------------------------------------------

    def write_table(
        self,
        table: Table,
        *,
        source_connection_id: str,
        estimated_row_count: int | None = None,
    ) -> None: ...

    def get_table(
        self, schema_name: str, name: str, *, source_connection_id: str
    ) -> Table | None: ...

    def delete_table(self, schema_name: str, name: str, *, source_connection_id: str) -> None: ...

    def list_tables(self, *, source_connection_id: str | None = None) -> list[tuple[str, str]]: ...

    def estimated_row_counts(self, *, source_connection_id: str) -> dict[str, int | None]:
        """Map `schema.table` -> cached `pg_class.reltuples` estimate.

        Keyed by qualified name so a caller resolving an entity's bound
        table can `.get(entity.qualified_table)` without a per-row query
        (the entities-index rollup reads it once for the whole source).
        The value is `None` for any table indexed from a backend that
        can't cheaply estimate row counts (SQLite, or a never-analyzed
        Postgres table). Read-only.
        """
        ...

    def write_graph_projection(
        self,
        *,
        source_connection_id: str,
        nodes: list[GraphNode],
        edges: list[GraphEdge],
    ) -> None:
        """Replace the whole v15 graph projection for a source atomically.

        Idempotent DELETE+INSERT in one transaction (mirrors
        `write_column_pii_tags`); empty inputs wipe the source's
        projection. An edge whose `join_name` names no live canonical
        join raises `sqlite3.IntegrityError` and rolls the projection
        back. See ADR 0010.
        """
        ...

    def list_graph_nodes(self, *, source_connection_id: str) -> list[GraphNode]:
        """Read the projected graph nodes for a source, ordered by
        `entity_name`. `is_catastrophic` is a bool; `row_count` is `None`
        when no estimate was projected. Empty source → empty list.
        Read-only."""
        ...

    def list_graph_edges(self, *, source_connection_id: str) -> list[GraphEdge]:
        """Read the projected graph edges for a source, ordered by
        `join_name`. Empty source → empty list. Read-only."""
        ...

    def list_distinct_source_connection_ids(self) -> list[str]:
        """Return every `source_connection_id` that has data in the
        store, deduped and sorted ascending.

        The union spans `tables`, `entities`, `metrics`, and
        `canonical_joins` — any row in any of those tables counts as
        "the store has data for this source". Used by `inspect`'s
        summary view to detect orphan data from a previous schemabrain
        version — i.e. when two `source_connection_id` partitions
        coexist in the same store and the rendered output would
        otherwise silently double-count. Returns an empty list for a
        fresh store with no rows yet.
        """
        ...

    # ----- Fingerprints --------------------------------------------

    def get_table_fingerprints(
        self, schema_name: str, name: str, *, source_connection_id: str
    ) -> dict[str, tuple[str, str]]: ...

    def write_table_fingerprints(
        self,
        schema_name: str,
        name: str,
        *,
        source_connection_id: str,
        fingerprints: dict[str, tuple[str, str]],
    ) -> None: ...

    # ----- Descriptions --------------------------------------------

    def get_table_descriptions(
        self, schema_name: str, name: str, *, source_connection_id: str
    ) -> dict[str, ColumnDescription]: ...

    def write_table_descriptions(
        self,
        schema_name: str,
        name: str,
        *,
        source_connection_id: str,
        descriptions: dict[str, ColumnDescription],
    ) -> None: ...

    # ----- Foreign keys --------------------------------------------

    def get_foreign_keys_targeting(
        self,
        target_schema: str,
        target_table: str,
        target_column: str,
        *,
        source_connection_id: str,
    ) -> list[IncomingForeignKey]: ...

    def list_all_foreign_keys(
        self, *, source_connection_id: str
    ) -> list[tuple[str, str, ForeignKey]]: ...

    # ----- Embeddings ----------------------------------------------

    def get_table_embeddings(
        self, schema_name: str, name: str, *, source_connection_id: str
    ) -> dict[str, ColumnEmbedding]: ...

    def write_table_embeddings(
        self,
        schema_name: str,
        name: str,
        *,
        source_connection_id: str,
        embeddings: dict[str, ColumnEmbedding],
    ) -> None: ...

    # ----- Cost ledger ----------------------------------------------
    #
    # Persists cumulative USD spent per `source_connection_id` so that
    # a process crash mid-enrichment doesn't let the next run start
    # from $0. Future store backends MUST implement these — the cost
    # cap depends on them.

    def get_spend_usd(self, *, source_connection_id: str) -> float:
        """Return cumulative USD spent on `source_connection_id`. 0.0 if absent."""
        ...

    def add_spend_usd(self, *, source_connection_id: str, amount_usd: float) -> float:
        """Atomically add `amount_usd` to the ledger; return new total.

        Implementations MUST:
        - Validate `amount_usd` is finite and non-negative; raise
          `ValueError` if not.
        - Guarantee atomic read-modify-write under concurrent access —
          a concurrent caller cannot slip a write between this call's
          read and write, producing a lost-update. `SQLiteStore` honours
          this via `BEGIN IMMEDIATE` when `writer_lock=True`; other
          backends pick their own primitive (Redis WATCH/MULTI/EXEC,
          DynamoDB conditional update, etc.).
        - Persist the new total durably before returning so a crash
          immediately after the return doesn't lose the write.
        """
        ...

    # ----- Freshness audit ------------------------------------------
    #
    # Read-only count primitive for the `index --dry-run --since DATE`
    # freshness preview. Reports how much of the cache has fallen
    # behind a cutoff so operators can estimate the cost of catching
    # up before committing to a real re-index.

    def count_stale_tables_and_columns(
        self, *, source_connection_id: str, since_ts: int
    ) -> tuple[int, int]:
        """Return (stale_tables, stale_columns) where `indexed_at < since_ts`.

        "Stale" means the cached table has not been re-indexed since
        the cutoff (Unix epoch seconds). Counts are restricted to
        rows matching `source_connection_id`. Implementations MUST
        raise `RuntimeError` when called against a closed store.
        """
        ...

    # ----- Embedding retrieval -------------------------------------
    #
    # Single bulk-fetch + cosine ranking primitive.
    # `EmbeddingRetriever` programs against this method exclusively;
    # any future store backend (in-memory mock, hosted backend) must
    # honour the full behavioural contract documented below, not just
    # the type signature, because callers rely on the error surface
    # and ordering guarantee.

    def search_embeddings_topk(
        self,
        query_vector: list[float],
        *,
        source_connection_id: str,
        k: int,
    ) -> list[tuple[str, str, str, float]]:
        """Return top-`k` columns by cosine similarity to `query_vector`.

        Each result is `(schema_name, table_name, column_name,
        cosine_score)`. Filters by `source_connection_id`; rows under
        other sources are invisible. Returns up to `k` rows — never
        pads; if fewer than `k` embeddings exist, the shorter list is
        the right answer.

        Ordering (REQUIRED — callers depend on this):
          - Descending by `cosine_score`.
          - Ties broken ascending by `(schema_name, table_name,
            column_name)`. The tiebreak must be deterministic so that
            results across repeated calls are reproducible.

        Implementations MUST raise `ValueError` for any of:
          - `k < 1`. Top-zero is a caller bug.
          - Empty `query_vector`.
          - `query_vector` contains a non-finite value (NaN or +/-inf).
          - `query_vector` has zero norm. A zero vector has no
            direction so cosine is mathematically undefined — fail
            loud rather than silently return everything tied at 0.
          - Dimension mismatch between `query_vector` and the stored
            embeddings under `source_connection_id`. The error message
            MUST contain the substring "dimension" so
            `EmbeddingRetriever` (and any future caller) can match on
            that token to surface the user-facing "embedder swapped —
            wipe and re-index" hint.

        Zero-norm STORED vectors must score 0.0 (NOT NaN). Callers
        filter zero-score rows; `EmbeddingRetriever` drops them when
        aggregating to per-table MAX.

        Empty store (no rows under `source_connection_id`) must
        short-circuit to `[]` BEFORE any dimension validation — a
        caller with the wrong query dim against an empty store must
        not raise.
        """
        ...

    def nearest_columns(
        self,
        schema_name: str,
        table_name: str,
        column_name: str,
        *,
        source_connection_id: str,
        k: int,
    ) -> list[tuple[str, str, str, float]]:
        """Return the top-`k` columns most similar to one reference column.

        The column→column nearest-neighbour counterpart of
        `search_embeddings_topk`: the query vector is the reference
        column's OWN stored embedding, searched over the same
        per-source vector space. Each result is `(schema_name,
        table_name, column_name, cosine_score)`.

        Implementations MUST:
          - exclude the reference column AND every other column in its
            table (table-mates are the same record, not "similar
            elsewhere"); a single same-table filter covers both;
          - order descending by `cosine_score`, ties broken ascending by
            `(schema_name, table_name, column_name)` — identical to
            `search_embeddings_topk` so the two share a cosine core;
          - return at most `k` rows, never padding.

        Implementations MUST return `[]` (NOT raise) for every degenerate
        INPUT, because the caller is a read-only drilldown surface that
        renders "no similar columns":
          - empty store / no candidates after the same-table filter,
          - the reference column has no stored embedding,
          - the reference embedding has zero norm (cosine undefined).

        `ValueError` is raised on the caller bug `k < 1`, and (as in
        `search_embeddings_topk`) may propagate from a corrupt or
        mixed-dimension store — a defense-in-depth guard that is
        unreachable through the write path.
        """
        ...

    # ----- Example queries -----------------------------------------
    #
    # Storage primitive behind tool #5 `get_example_queries`. The
    # writer is exposed so any miner (today: `schemabrain.mining`
    # against `pg_stat_statements`; later: alternative pipelines) can
    # populate the table without subclassing. An empty `list_example_
    # queries` result is the right answer when a table has no
    # recorded examples — the MCP tool layer maps that to a
    # charter-compliant `status="empty"` envelope.

    def write_example_queries(
        self,
        rows: list[ExampleQuery],
        *,
        source_connection_id: str,
    ) -> int:
        """UPSERT a batch of `example_queries` rows.

        Conflict target is `(source_connection_id, schema_name,
        table_name, sql_text)`. Implementations MUST:
          - Preserve `first_seen_at` across re-writes of the same
            tuple; only the FIRST insert's value is durable.
          - Update `observation_count`, `last_seen_at`, `sensitivity`,
            and `pii_categories` to the values in the latest call.
          - Treat the batch atomically — partial application on a
            mid-batch failure (FK / CHECK / unique violation) is
            forbidden; callers see all-or-nothing.

        Returns the number of rows in the input batch (equal to the
        number of write operations attempted). Empty input returns 0.
        """
        ...

    def list_all_example_queries(
        self,
        *,
        source_connection_id: str,
    ) -> list[ExampleQuery]:
        """Return every example query row for `source_connection_id`.

        Bulk reader symmetric with `list_all_foreign_keys`. Drives the
        the canonical-join mining pipeline, which needs to walk every
        recorded SQL statement to extract equi-join predicates.

        IMPORTANT — duplicate semantics: the `example_queries` table
        carries one row per `(sql_text, table_touched)` pair. A single
        SQL statement that touched N indexed tables produces N rows
        with identical `sql_text` and identical `observation_count`
        (the write path in `mining/pipeline.py` guarantees this).
        Implementations MUST return the full row set including these
        duplicates — callers that want distinct statements MUST
        dedupe in memory by `sql_text` (the join-mining pipeline at
        `joins/suggest.py::_load_aggregated_query_log` does so).

        Ordered alphabetically by `(schema_name, table_name, sql_text)`
        for stable iteration across runs. Empty result is the right
        answer for an un-mined store — callers fall back to FK
        evidence alone in that case.
        """
        ...

    def list_example_queries(
        self,
        schema: str,
        table: str,
        *,
        source_connection_id: str,
        limit: int,
    ) -> list[ExampleQuery]:
        """Return observed example SQL for `(schema, table)`, ranked.

        Ordering (REQUIRED — callers depend on this for deterministic
        pagination once mining writes many rows per table):
          - Descending by `observation_count`.
          - Then descending by `last_seen_at`.
          - Then ascending by insertion order (the surrogate row id).

        Returns up to `limit` rows; fewer is the right answer when the
        store holds fewer matching examples. Implementations MUST
        raise `ValueError` for `limit < 1` (mirror of `k` validation
        on `search_embeddings_topk`).

        This method does NOT distinguish between "no such table" and
        "table indexed but no examples" — both return `[]`. Callers
        that need to distinguish those states (the MCP tool layer
        does, so it can return `unknown_name` vs `empty` envelopes)
        must call `get_table` first to disambiguate.
        """
        ...

    # ----- Entities ------------------------------------------------
    #
    # Semantic-layer foundation. Implementations MUST raise
    # `DbtOwnedEntityError` from `write_entity` when a non-`dbt_import`
    # entity is written over an existing `dbt_import` row — the
    # contract that lets the dbt-import write-path own its entities
    # without re-litigation at every manual edit.

    def write_entity(self, entity: Entity, *, source_connection_id: str) -> None:
        """Upsert one Entity into the store.

        Implementations MUST:
          - Reject overwriting an existing `origin="dbt_import"` row
            with a non-`dbt_import` entity; raise `DbtOwnedEntityError`
            (a `ValueError` subclass, defined in
            `schemabrain.core.entity`).
          - Allow `dbt_import → dbt_import` (idempotent re-import is
            the intended `schemabrain import dbt` re-run path).
          - Allow `dbt_import` to overwrite an existing `manual` or
            `suggested` entity — the dbt import is the source of
            truth and takes ownership.
          - Allow `manual ↔ manual` and `suggested → manual` (user
            confirmation of an LLM suggestion); other transitions
            between non-`dbt_import` origins are unconstrained by
            this Protocol and may be tightened in a follow-up PR
            alongside the LLM-suggest pipeline.
          - Preserve `created_at` semantics across rewrites of the
            same `(source_connection_id, name)` — the first insert's
            timestamp is durable.
          - Enforce the bound-table foreign key — writes referencing
            a table not yet in the store MUST fail.
        """
        ...

    def get_entity(self, name: str, *, source_connection_id: str) -> Entity | None:
        """Return the entity named `name`, or `None` if absent."""
        ...

    def list_entities(self, *, source_connection_id: str | None = None) -> list[Entity]:
        """Return all entities, ordered deterministically by `name`.

        `source_connection_id=None` lists across sources; with a
        source filter, only that source's entities are returned. The
        MCP `list_entities` tool depends on the alphabetical ordering
        so the agent sees a stable list across repeat calls.
        """
        ...

    # ----- Canonical joins ------------------------------------------
    #
    # The the semantic-layer substrate. Multiple canonical joins
    # between the same `(source_entity, target_entity)` pair are
    # explicitly supported — `name` is the disambiguator. Implementations
    # MUST enforce the FK to `entities` on both source + target so a
    # canonical join cannot orphan past an entity deletion.

    def write_canonical_join(self, join: CanonicalJoin, *, source_connection_id: str) -> None:
        """Upsert one CanonicalJoin into the store.

        Implementations MUST:
          - Reject writes where `source_entity` or `target_entity` is
            not present in the `entities` table for this source. The
            store-level FK is the canonical enforcement layer.
          - Preserve `created_at` semantics across rewrites of the same
            `(source_connection_id, name)`.
          - Round-trip the `on` column list via JSON or equivalent —
            composite-key joins (length >= 2) must survive store
            re-reads byte-identical.
          - Accept all three `JoinOrigin` values (`manual`, `suggested`,
            `dbt_import`). `dbt_import` is reserved for a future release;
            the store layer doesn't refuse it — refusal lives in the
            upstream suggest/apply pipelines that don't yet know how
            to produce dbt-derived joins.
        """
        ...

    def get_canonical_join(self, name: str, *, source_connection_id: str) -> CanonicalJoin | None:
        """Return the canonical join named `name`, or `None` if absent."""
        ...

    def list_canonical_joins(
        self, *, source_connection_id: str | None = None
    ) -> list[CanonicalJoin]:
        """Return all canonical joins, ordered deterministically by name.

        `source_connection_id=None` lists across sources. The CLI
        `joins list` command and the cycle-detection report
        depend on the alphabetical ordering for stable output.
        """
        ...

    def resolve_canonical_joins(
        self,
        entity_a: str,
        entity_b: str,
        *,
        source_connection_id: str,
    ) -> list[CanonicalJoin]:
        """Return every canonical join between `entity_a` and `entity_b`.

        Direction-insensitive: rows where `(source_entity,
        target_entity)` is either `(entity_a, entity_b)` or
        `(entity_b, entity_a)` are returned. The caller — the
        `resolve_join` MCP tool — implements ambiguity refusal against
        this list (0 rows = no canonical join; 2+ rows = ambiguous,
        name-disambiguator path).

        Each row preserves its STORED direction — implementations MUST
        NOT flip columns based on input order so the agent-facing
        `sql_skeleton` aligns with how the join was originally
        confirmed.

        Ordered alphabetically by `name` for determinism.
        """
        ...

    # ----- Metrics --------------------------------------------------
    #
    # The value side of the semantic layer. Implementations MUST raise
    # `DbtOwnedMetricError` from `write_metric` when a non-`dbt_import`
    # metric is written over an existing `dbt_import` row — the same
    # contract `write_entity` enforces for entities.

    def write_metric(self, metric: Metric, *, source_connection_id: str) -> None:
        """Upsert one Metric into the store.

        Implementations MUST:
          - Reject overwriting an existing `origin="dbt_import"` row
            with a non-`dbt_import` metric; raise `DbtOwnedMetricError`
            (a `ValueError` subclass, defined in
            `schemabrain.core.metric`).
          - Allow `dbt_import → dbt_import` (idempotent re-import is
            the intended `schemabrain import dbt --include-metrics`
            re-run path).
          - Allow `dbt_import` to overwrite an existing `manual` or
            `suggested` metric — the dbt import is the source of
            truth and takes ownership.
          - Allow `manual ↔ manual` and `suggested → manual` (user
            confirmation of an LLM suggestion).
          - Preserve `created_at` semantics across rewrites of the
            same `(source_connection_id, name)`.
          - Enforce the anchor-entity foreign key — writes referencing
            an entity not yet in the store MUST fail.
          - Round-trip `time_grains` as a canonical-sorted comma-
            separated string; empty string when `time_dimension is
            None`. The dataclass invariant (paired-emptiness +
            canonical order) is the source of truth.
        """
        ...

    def get_metric(self, name: str, *, source_connection_id: str) -> Metric | None:
        """Return the metric named `name`, or `None` if absent."""
        ...

    def list_metrics(self, *, source_connection_id: str | None = None) -> list[Metric]:
        """Return all metrics, ordered deterministically by name.

        `source_connection_id=None` lists across sources. The CLI
        `metrics list` command depends on alphabetical ordering for
        stable output across runs.
        """
        ...

    def delete_metric(self, name: str, *, source_connection_id: str) -> bool:
        """Idempotent: delete the metric row for `(source_connection_id, name)`.

        Returns `True` if a row was deleted, `False` if no row existed.
        Same dbt-owned guard shape as `write_metric` — a metric with
        `origin='dbt_import'` is refused (raises `DbtOwnedMetricError`)
        because manual deletion of a dbt-managed row would silently
        drift from the upstream dbt repo on the next import.

        Used by `schemabrain metrics audit --fix` to remove
        LLM-suggested metrics whose descriptions admit the metric does
        not actually compute what its name implies (the
        `total_order_item_revenue` footgun pattern).
        """
        ...

    # ----- PII tags -------------------------------------------------
    #
    # Per-column PII classification produced by the heuristic
    # classifier (`schemabrain.pii.classifier`) at index time. The
    # `get_metric` compiler reads tags here, propagates them across
    # the columns a metric touches, and populates `mcp_audit.
    # pii_categories` + drives `pii_blocked` refusals.

    def write_column_pii_tags(
        self,
        *,
        source_connection_id: str,
        qualified_table: str,
        tags: Mapping[str, ColumnPiiTag],
        confidence: Mapping[str, tuple[PiiConfidenceBand | None, float | None]] | None = None,
    ) -> None:
        """Replace all PII tags for `qualified_table` atomically.

        Each entry in `tags` is `column_name → ColumnPiiTag` (the
        `(Sensitivity, frozenset[PIICategory])` alias from
        `pii.categories`). Implementations MUST delete every existing
        row for the table before inserting; an empty `tags` deletes
        without re-inserting (the `--no-pii-classify` opt-out depends
        on this shape).

        `confidence` (ADR 0009) is the optional index-time
        `column_name → (band, score)` map. A column absent from it — or
        the whole argument omitted — persists NULL for both
        `pii_confidence` and `pii_confidence_score`. The band/score are
        advisory PII-matrix metadata and never gate enforcement.
        """
        ...

    def get_column_pii_tags(
        self,
        *,
        source_connection_id: str,
        qualified_table: str,
        columns: Iterable[str],
    ) -> dict[str, ColumnPiiTag]:
        """Bulk-fetch PII tags for `columns` on `qualified_table`.

        Returns a mapping `column_name → ColumnPiiTag` ONLY for
        columns with a stored row. Columns absent from the result
        are treated as `("public", frozenset())` by callers (matches
        the propagation helper's empty-input contract).
        """
        ...

    def get_column_pii_confidence(
        self,
        *,
        source_connection_id: str,
        qualified_table: str,
        columns: Iterable[str],
    ) -> dict[str, tuple[str | None, float | None]]:
        """Bulk-fetch the per-column PII confidence band + raw score.

        Returns `column_name → (band, score)` ONLY for columns with a
        stored row. `band` is the display bucket (`floor_locked` /
        `high` / `medium` / `low`) or `None`; `score` is the index-time
        raw number (the source of truth) or `None`. Both are NULL until
        a re-index populates them — the score is computed on un-redacted
        in-memory values at index time and cannot be recomputed from
        store state, so a v14-migrated store and freshly-classified rows
        both read `(None, None)` until the index-time writer lands.
        """
        ...

    def upsert_column_pii_tag_override(
        self,
        *,
        source_connection_id: str,
        qualified_table: str,
        column_name: str,
        sensitivity: Sensitivity,
        categories: frozenset[PIICategory],
        force_catastrophic_downgrade: bool = False,
    ) -> None:
        """Upsert one operator-asserted PII tag override (`origin='operator'`).

        Replaces any prior row for the same `(source_connection_id,
        qualified_table, column_name)` key — heuristic OR operator.
        Per the schema, the primary key is the storage discriminator;
        there is no layering of heuristic + operator on the same row.

        Raises `CatastrophicDowngradeError` when the override would strip
        a column's always-on catastrophic-leak protection (credential /
        payment_card / government_id) and `force_catastrophic_downgrade`
        is False (LB-2 guard).
        """
        ...

    def delete_column_pii_tag_override(
        self,
        *,
        source_connection_id: str,
        qualified_table: str,
        column_name: str,
        force_catastrophic_downgrade: bool = False,
    ) -> bool:
        """Delete one operator-asserted PII tag override row.

        Filtered on `origin='operator'` so the call cannot wipe a
        heuristic row by accident. Returns True if a row was deleted.
        The next `schemabrain index` run will re-classify the column
        from scratch.
        """
        ...

    def list_column_pii_tags_with_origin(
        self,
        *,
        source_connection_id: str,
        origin: str | None = None,
    ) -> list[tuple[str, str, Sensitivity, frozenset[PIICategory], str]]:
        """List every PII tag row for a source, with origin (`heuristic`
        / `operator`) preserved.

        Returns `(qualified_table, column_name, sensitivity,
        categories, origin)` tuples. `origin` filter narrows the
        result when set. Used by the dashboard policy view + CLI
        `policy tag list` to render the per-column verdict surface.
        """
        ...
