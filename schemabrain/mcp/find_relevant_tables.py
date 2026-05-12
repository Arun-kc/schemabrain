"""MCP tool implementation: find_relevant_tables."""

from __future__ import annotations

from schemabrain.core.store import SQLiteStore
from schemabrain.enrichment.embeddings import Embedder
from schemabrain.mcp._helpers import _cosine, _with_token_estimate
from schemabrain.mcp.shapes import TableHit


def find_relevant_tables_impl(
    *,
    store: SQLiteStore,
    source_connection_id: str,
    embedder: Embedder,
    query: str,
    limit: int,
) -> list[TableHit]:
    """Embedding-cosine retrieval that returns ranked `TableHit`s.

    Per-table score = MAX cosine across the table's columns (sparse-
    relevance heuristic — one highly-aligned column is strong evidence).
    Beyond just ranking (what `EmbeddingRetriever` does), this also
    surfaces the WINNING column name + description so the MCP response
    explains WHY the table matched.

    Behavior is parallel to `EmbeddingRetriever`:
      - Empty/whitespace query and `limit <= 0` short-circuit to `[]`
        without calling the embedder.
      - Tables without any embeddings are silently skipped.
      - Zero-score tables are excluded from the result list.
      - Deterministic tiebreak by qualified name when scores tie.
    """
    if limit <= 0:
        return []
    if not query.strip():
        return []

    query_vec = embedder.embed(query)

    # Per-table best (score, best_column_name) tuples.
    # Reading descriptions per table only when needed (deferred until
    # we know the table is in the result set).
    candidates: list[tuple[float, str, str, str]] = []
    for schema, table in store.list_tables(source_connection_id=source_connection_id):
        embeddings = store.get_table_embeddings(
            schema, table, source_connection_id=source_connection_id
        )
        if not embeddings:
            continue
        best_score = -1.0
        best_column = ""
        for col_name, emb in embeddings.items():
            score = _cosine(query_vec, emb.vector)
            if score > best_score:
                best_score = score
                best_column = col_name
        if best_score > 0.0:
            candidates.append((best_score, schema, table, best_column))

    # Sort descending by score; tiebreak by qualified name for
    # reproducibility (matches EmbeddingRetriever's contract).
    candidates.sort(key=lambda x: (-x[0], x[1], x[2]))
    top = candidates[:limit]

    hits: list[TableHit] = []
    for score, schema, table, best_column in top:
        descs = store.get_table_descriptions(
            schema, table, source_connection_id=source_connection_id
        )
        best_desc_obj = descs.get(best_column)
        best_desc = best_desc_obj.text if best_desc_obj is not None else ""
        partial = TableHit(
            qualified_name=f"{schema}.{table}",
            score=score,
            best_column=best_column,
            best_column_description=best_desc,
            token_estimate=0,  # placeholder; rebuilt by _with_token_estimate
        )
        hits.append(_with_token_estimate(partial))
    return hits
