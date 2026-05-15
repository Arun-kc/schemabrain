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
Brain learns to persist new model classes. v1 wk 11+ adds
SemanticRepository methods (entities/metrics/joins); v1 wk 16 adds
AuditLogWriter methods (append-only audit rows with hash-chained
fingerprints). Existing methods stay stable; new methods slot in
without breaking callers.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from schemabrain.core.description import ColumnDescription
from schemabrain.core.embedding import ColumnEmbedding
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

        Phase A Commit 1 stores the flag without wiring behaviour;
        Phase A Commit 2 wires write methods to honour it for the
        cost-ledger CAS path. Callers can read this to branch on
        whether cross-process safety is in effect — useful for
        Commit 2's async-enrichment dispatcher.
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

    # ----- Embedding retrieval (filled in Phase A Commit 3) ---------
    #
    # New seam introduced in Phase A. v1 retrieval today loads
    # embeddings per-table and computes cosine in Python, which is an
    # N+1 SQL pattern (`get_table_embeddings` per table) bounded by a
    # quadratic-ish round-trip cost. `search_embeddings_topk` exposes
    # a single bulk-fetch + NumPy matmul path that Commit 3 fills in
    # `SQLiteStore`. The slot lives on the Protocol from Commit 1 so
    # Commit 3 is a pure body-fill, not a Protocol-plus-callers
    # refactor.
    #
    # Today: `SQLiteStore.search_embeddings_topk` raises
    # `NotImplementedError`. `EmbeddingRetriever` does NOT call it
    # yet; the per-table-loop retrieval path stays the production
    # path until Commit 3 flips the switch.
    def search_embeddings_topk(
        self,
        query_vector: list[float],
        *,
        source_connection_id: str,
        k: int,
    ) -> list[tuple[str, str, str, float]]:
        """Return top-`k` `(schema, table, column, cosine_score)` for the query.

        Body filled in Phase A Commit 3 (NumPy bulk-fetch matmul cosine).
        Until then, the canonical implementation raises
        `NotImplementedError`.
        """
        ...
