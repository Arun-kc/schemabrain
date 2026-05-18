"""MCP tool implementation: find_relevant_entities.

Semantic-aware entity discovery. Mirrors `find_relevant_tables` but
restricts the result set to tables that have a confirmed entity
binding, returning the entity name (not the qualified table) as the
primary identifier so the agent can chain straight into
`describe_entity` / `resolve_join` / `get_metric`.

No new embedding work: the existing column-level embedding index is
the semantic signal. An entity's score is the MAX cosine similarity
across the columns of its bound table — same per-table aggregation
as `find_relevant_tables`, gated by the entity-binding lookup so only
curated entities surface.
"""

from __future__ import annotations

from typing import NamedTuple

import numpy as np

from schemabrain.core.entity import Entity
from schemabrain.core.store_protocol import Store
from schemabrain.enrichment.embeddings import Embedder
from schemabrain.mcp._helpers import _with_token_estimate
from schemabrain.mcp.shapes import EntityHit

# Same value as `find_relevant_tables._BULK_FETCH_COLUMN_K`. Mirrored
# rather than imported so the two callers stay coupled by intent (cover
# every embedded column at v1 scale, < 10k columns) rather than by a
# shared constant whose semantics differ as the project grows.
_BULK_FETCH_COLUMN_K = 10_000


class _EntityBest(NamedTuple):
    """Per-entity aggregation record. Internal to this module — the wire
    shape is `EntityHit`. Used as the value type of the `best_per_entity`
    dict so the per-entity (score, location, winning column) tuple stays
    accessed by name rather than by positional index. Replaces an
    earlier `tuple[float, str, str, str]` that hid a field-swap risk
    during refactor (a reordering of two same-typed string fields
    would compile silently).
    """

    score: float
    schema: str
    table: str
    best_column: str


def find_relevant_entities_impl(
    *,
    store: Store,
    source_connection_id: str,
    embedder: Embedder,
    query: str,
    limit: int,
) -> list[EntityHit]:
    """Embedding-cosine retrieval scoped to curated entities.

    Per-entity score = MAX cosine across the columns of the entity's
    bound table. Same MAX aggregation as `find_relevant_tables`, but
    only tables with an associated entity contribute — columns under
    non-entity tables are silently dropped.

    Behavior contract (mirrored from `find_relevant_tables` so agents
    get a consistent surface across both discovery tools):
      - `limit <= 0` and empty/whitespace query short-circuit to `[]`
        BEFORE calling the embedder.
      - A store with no entities curated under `source_connection_id`
        short-circuits to `[]` BEFORE calling the embedder — a
        non-curated store shouldn't burn embedder cycles per call.
      - Zero-norm query vectors translate to `[]` here so the Store's
        loud `ValueError` doesn't propagate to the MCP caller.
      - Columns belonging to tables without a confirmed entity binding
        are silently dropped.
      - Zero-score entities are excluded.
      - Deterministic tiebreak: alphabetical by entity name when scores
        tie.
    """
    if limit <= 0:
        return []
    if not query.strip():
        return []

    # Build (schema, table) -> Entity lookup. Empty entity surface
    # short-circuits BEFORE embedding: a store with no entities has no
    # possible result. Store-side invariant guarantees at-most-one
    # entity per (source, table), so collisions are impossible here.
    entities = store.list_entities(source_connection_id=source_connection_id)
    if not entities:
        return []
    table_to_entity: dict[tuple[str, str], Entity] = {}
    for entity in entities:
        schema, table = entity.qualified_table.split(".", 1)
        table_to_entity[(schema, table)] = entity

    query_vec = embedder.embed(query)

    # Zero-norm query → no direction → no signal. Mirror the
    # `find_relevant_tables` translation so an embedder that emits a
    # degenerate vector returns an empty result instead of crashing
    # the MCP call.
    if float(np.linalg.norm(np.asarray(query_vec, dtype=np.float32))) == 0.0:
        return []

    col_rows = store.search_embeddings_topk(
        list(query_vec),
        source_connection_id=source_connection_id,
        k=_BULK_FETCH_COLUMN_K,
    )

    # Aggregate column scores to per-entity best. Load-bearing
    # assumption (same as find_relevant_tables): rows arrive sorted by
    # descending cosine score with deterministic alphabetic tiebreak on
    # (schema, table, column). Under that contract the FIRST occurrence
    # per (schema, table) IS its best column — both highest-scoring and
    # (for ties) the alphabetically first column. Subsequent occurrences
    # are skipped.
    best_per_entity: dict[str, _EntityBest] = {}
    for schema, table, col, score in col_rows:
        if score <= 0.0:
            continue
        entity = table_to_entity.get((schema, table))
        if entity is None:
            continue
        if entity.name not in best_per_entity:
            best_per_entity[entity.name] = _EntityBest(
                score=score, schema=schema, table=table, best_column=col
            )

    # Sort by descending score, alphabetical tiebreak on entity name.
    ranked = sorted(
        best_per_entity.items(),
        key=lambda kv: (-kv[1].score, kv[0]),
    )
    top = ranked[:limit]

    hits: list[EntityHit] = []
    for entity_name, best in top:
        descs = store.get_table_descriptions(
            best.schema, best.table, source_connection_id=source_connection_id
        )
        best_desc_obj = descs.get(best.best_column)
        best_desc = best_desc_obj.text if best_desc_obj is not None else ""
        partial = EntityHit(
            name=entity_name,
            score=best.score,
            qualified_table=f"{best.schema}.{best.table}",
            best_column=best.best_column,
            best_column_description=best_desc,
            token_estimate=0,  # placeholder; rebuilt by _with_token_estimate
        )
        hits.append(_with_token_estimate(partial))
    return hits
