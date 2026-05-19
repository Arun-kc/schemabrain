"""MCP tool implementation: find_relevant_entities.

Semantic-aware entity discovery. Mirrors `find_relevant_tables` but
restricts the result set to tables that have a confirmed entity
binding, returning the entity name (not the qualified table) as the
primary identifier so the agent can chain straight into
`describe_entity` / `resolve_join` / `get_metric`.

Two ranking signals contribute, scored independently and combined
per-entity by MAX cosine:

  1. **Column-level** — embedded column descriptions for the entity's
     bound table. This is what `find_relevant_tables` ranks; we
     intersect it with the entity-binding lookup so only curated
     entities surface.
  2. **Entity-description** — the one-sentence LLM-generated
     description of each entity (`Entity.description`), embedded
     at query time and cached in `_DESC_EMBED_CACHE` so the cost
     amortizes across calls within a serve process.

Why both: the column signal is good when the user's query maps to a
specific column (e.g. "email addresses" → users.email). The
description signal is good when the user's query maps to the
entity's overall concept (e.g. "customer" → user.description even
when no column literally mentions "customer"). Without the
description signal, short entity names like `user` lose to
longer-named neighbors like `order` whose `user_id` column
description coincidentally scores higher on "customer" than
`email` does. The MAX combination picks whichever signal is
strongest per entity rather than averaging — averaging would
dilute a strong description match with weak column matches.
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

# Sentinel value placed in the `best_column` field of an `EntityHit`
# when the ranking score came from the entity-description embedding
# rather than from one of the entity's bound-table column embeddings.
# Surfaced to the agent so it sees WHY the entity surfaced — distinct
# from any real column name (parenthesised strings are not legal
# Postgres identifiers per `_IDENT_RE` in `core/entity.py`).
_ENTITY_DESCRIPTION_MATCH_LABEL = "(entity description)"

# Process-local cache of `description text -> embedding vector`.
# Embeddings are deterministic for the same `(embedder, text)` pair
# (modulo ONNX nondeterminism the embedder itself warns about); the
# cache key is text alone because a serve process has exactly one
# `Embedder` instance shared by every tool call. Without the cache,
# every `find_relevant_entities` invocation would re-embed every
# entity description, costing ~O(entities * embedder_latency) per
# call. Cache size is bounded in practice (~100 entities * ~400-dim
# float32 = ~150KB per source) — small enough to skip an LRU eviction
# policy at v1 scale. Cleared between tests via
# `_clear_description_embedding_cache()`.
_DESC_EMBED_CACHE: dict[str, np.ndarray] = {}


def _clear_description_embedding_cache() -> None:
    """Reset the description-embedding cache. Test-only seam — tests
    that exercise the ranking path need a clean cache so cached
    embeddings from a prior test don't bleed into the current one.
    """
    _DESC_EMBED_CACHE.clear()


def _get_description_embedding(embedder: Embedder, text: str) -> np.ndarray:
    """Embed `text` once per process and cache the result.

    Returns a float32 numpy array of length `embedder.dimension`.
    """
    cached = _DESC_EMBED_CACHE.get(text)
    if cached is not None:
        return cached
    vec = np.asarray(embedder.embed(text), dtype=np.float32)
    _DESC_EMBED_CACHE[text] = vec
    return vec


def _cosine_or_zero(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity of two float32 vectors. Returns 0.0 on any
    zero-norm input rather than raising — matches the silent-skip
    behavior the surrounding code uses for degenerate query vectors.
    """
    a_norm = float(np.linalg.norm(a))
    b_norm = float(np.linalg.norm(b))
    if a_norm == 0.0 or b_norm == 0.0:
        return 0.0
    return float(np.dot(a, b) / (a_norm * b_norm))


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

    # Entity-description signal: independent of column embeddings.
    # For every entity with a non-empty description, embed it (cached)
    # and compare cosine to the query. If the description score beats
    # the column score for the same entity, promote it — and tag the
    # match with the sentinel `_ENTITY_DESCRIPTION_MATCH_LABEL` so the
    # caller sees WHY the entity surfaced. Entities that had NO column
    # contribution (e.g. tables whose columns have no embeddings yet)
    # but DO have a description still join the result set via this
    # path. This is the fix for the S6 quality issue: short, generic
    # entity names like `user` lose to longer column-rich neighbors on
    # cosine-against-columns alone; the description signal restores
    # the natural "customer" → user mapping that the LLM-generated
    # one-sentence description carries explicitly.
    query_np = np.asarray(query_vec, dtype=np.float32)
    for (schema, table), entity in table_to_entity.items():
        if not entity.description:
            continue
        desc_vec = _get_description_embedding(embedder, entity.description)
        desc_score = _cosine_or_zero(query_np, desc_vec)
        if desc_score <= 0.0:
            continue
        current = best_per_entity.get(entity.name)
        if current is None or desc_score > current.score:
            best_per_entity[entity.name] = _EntityBest(
                score=desc_score,
                schema=schema,
                table=table,
                best_column=_ENTITY_DESCRIPTION_MATCH_LABEL,
            )

    # Sort by descending score, alphabetical tiebreak on entity name.
    ranked = sorted(
        best_per_entity.items(),
        key=lambda kv: (-kv[1].score, kv[0]),
    )
    top = ranked[:limit]

    hits: list[EntityHit] = []
    for entity_name, best in top:
        if best.best_column == _ENTITY_DESCRIPTION_MATCH_LABEL:
            # Entity-description match path. The sentinel best_column
            # isn't a real column, so we don't query the column-
            # description store; instead surface the entity's own
            # description text as `best_column_description` so the
            # agent sees WHY the entity scored.
            entity = table_to_entity[(best.schema, best.table)]
            best_desc = entity.description
        else:
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
