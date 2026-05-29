"""MCP tool implementation: find_relevant_tables."""

from __future__ import annotations

import numpy as np

from schemabrain.core.store_protocol import Store
from schemabrain.enrichment.embeddings import Embedder
from schemabrain.mcp._helpers import _with_token_estimate
from schemabrain.mcp.shapes import TableHit
from schemabrain.pii import CATASTROPHIC_LEAK_CATEGORIES, PIICategory

# Upper bound on columns the bulk-fetch primitive will rank before we
# aggregate to per-table MAX. Parallel to `_BULK_FETCH_COLUMN_K` in
# `schemabrain.eval.retriever`; both callers want the same property
# (cover every embedded column under one source for a v1-scale store
# of < 10k columns). Duplicated rather than abstracted because the two
# call sites live in different packages and the value is the same
# *coincidence* of v1 scale, not a shared concept that should be
# reified yet. Revisit if a third caller appears.
_BULK_FETCH_COLUMN_K = 10_000


def find_relevant_tables_impl(
    *,
    store: Store,
    source_connection_id: str,
    embedder: Embedder,
    query: str,
    limit: int,
    pii_block: frozenset[PIICategory] = frozenset(),
) -> list[TableHit]:
    """Embedding-cosine retrieval that returns ranked `TableHit`s.

    Per-table score = MAX cosine across the table's columns (sparse-
    relevance heuristic — one highly-aligned column is strong evidence).
    Beyond just ranking (what `EmbeddingRetriever` does), this also
    surfaces the WINNING column name + description so the MCP response
    explains WHY the table matched.

    Delegates the ranking primitive to `store.search_embeddings_topk`
    — one bulk SELECT + NumPy matmul. Description lookup for the
    winning column happens per-table for the top `limit` results, so
    the per-table SQL is bounded by `limit` (10 by default) instead of
    by the total table count.

    Behavior contract:
      - Empty/whitespace query and `limit <= 0` short-circuit to `[]`
        without calling the embedder.
      - Tables without any embeddings are silently skipped (the
        bulk-fetch primitive has no rows for them).
      - Zero-score tables are excluded from the result list.
      - Deterministic tiebreak by qualified name when scores tie.
      - Zero-norm queries are translated to `[]` here so the Store's
        loud `ValueError` doesn't propagate to the MCP caller — the
        retriever's degeneracy is the right signal at this layer.
    """
    if limit <= 0:
        return []
    if not query.strip():
        return []

    query_vec = embedder.embed(query)

    # Zero-norm query → no direction → no signal. Mirror the
    # `EmbeddingRetriever` translation so an embedder that emits a
    # degenerate vector returns an empty result instead of crashing
    # the MCP call. Use the same float32 norm the Store uses so
    # subnormal vectors don't slip past this pre-check and trip the
    # Store's ValueError downstream.
    if float(np.linalg.norm(np.asarray(query_vec, dtype=np.float32))) == 0.0:
        return []

    col_rows = store.search_embeddings_topk(
        list(query_vec),
        source_connection_id=source_connection_id,
        k=_BULK_FETCH_COLUMN_K,
    )

    # Aggregate column scores to per-table best (score, winning column).
    # Load-bearing assumption: `Store.search_embeddings_topk` returns
    # rows sorted by descending cosine score with deterministic
    # alphabetic tiebreak on (schema, table, column). See the
    # "Ordering (REQUIRED — callers depend on this)" block in
    # `core.store_protocol.Store.search_embeddings_topk`. Under that
    # contract the FIRST occurrence of any (schema, table) IS its
    # best column — both highest-scoring and (for ties) the
    # alphabetically-first column. Subsequent occurrences must be
    # skipped, not allowed to clobber.
    best_per_table: dict[tuple[str, str], tuple[float, str]] = {}
    for schema, table, col, score in col_rows:
        if score <= 0.0:
            continue
        if (schema, table) not in best_per_table:
            best_per_table[(schema, table)] = (score, col)

    candidates = [
        (score, schema, table, best_col)
        for (schema, table), (score, best_col) in best_per_table.items()
    ]
    candidates.sort(key=lambda x: (-x[0], x[1], x[2]))
    top = candidates[:limit]

    # Same effective_block policy describe_table applies: operator's
    # `--pii-block` union the catastrophic-leak floor. Without this,
    # `find_relevant_tables(query="user passwords")` happily returns
    # `best_column="password_hash"` even though describe_table redacts
    # the same column to `<redacted_credential_column_N>`. The discovery
    # surface and the per-table view must enforce the same policy.
    effective_block: frozenset[PIICategory] = pii_block | CATASTROPHIC_LEAK_CATEGORIES

    hits: list[TableHit] = []
    for score, schema, table, best_column in top:
        qualified_table = f"{schema}.{table}"
        tags = store.get_column_pii_tags(
            source_connection_id=source_connection_id,
            qualified_table=qualified_table,
            columns=[best_column],
        )
        _, categories = tags.get(best_column, ("public", frozenset()))
        if categories & effective_block:
            # Hide the real name AND the description (the LLM-authored
            # text often names the column verbatim — "the bcrypt-hashed
            # password" — so passing it through would defeat the
            # column-name redaction).
            display_column = "<redacted_column>"
            best_desc = ""
        else:
            descs = store.get_table_descriptions(
                schema, table, source_connection_id=source_connection_id
            )
            best_desc_obj = descs.get(best_column)
            best_desc = best_desc_obj.text if best_desc_obj is not None else ""
            display_column = best_column
        partial = TableHit(
            qualified_name=qualified_table,
            score=score,
            best_column=display_column,
            best_column_description=best_desc,
            token_estimate=0,  # placeholder; rebuilt by _with_token_estimate
        )
        hits.append(_with_token_estimate(partial))
    return hits
