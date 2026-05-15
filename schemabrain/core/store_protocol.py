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

from typing import Protocol, runtime_checkable

from schemabrain.core.description import ColumnDescription
from schemabrain.core.embedding import ColumnEmbedding
from schemabrain.core.example_query import ExampleQuery
from schemabrain.core.models import ForeignKey, IncomingForeignKey, Table


@runtime_checkable
class Store(Protocol):
    """The persistence boundary for Schema Brain.

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

    def write_table(self, table: Table, *, source_connection_id: str) -> None: ...

    def get_table(
        self, schema_name: str, name: str, *, source_connection_id: str
    ) -> Table | None: ...

    def delete_table(self, schema_name: str, name: str, *, source_connection_id: str) -> None: ...

    def list_tables(self, *, source_connection_id: str | None = None) -> list[tuple[str, str]]: ...

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
