"""MCP tool implementations.

Pure functions over `SQLiteStore` + `Embedder`. The FastMCP wiring in
`server.py` is a thin adapter — these impls are the actual logic and
are independently unit-tested without touching the MCP transport.

Return shapes are Pydantic models so FastMCP serializes them with
schema-aware structured output (clients see typed fields, not opaque
strings). `token_estimate` on every payload lets agents budget context.
"""

from __future__ import annotations

import math
from typing import TypeVar

from pydantic import BaseModel, ConfigDict, Field

from schemabrain.core.store import SQLiteStore
from schemabrain.enrichment.embeddings import Embedder

# Char-to-token ratio for the token estimator. ~4 is the standard rough
# estimate for English text + JSON punctuation. Used uniformly across
# all tool responses so agent budget arithmetic stays consistent.
_CHARS_PER_TOKEN = 4

_M = TypeVar("_M", bound=BaseModel)


class TableNotFoundError(LookupError):
    """Raised by `describe_table_impl` when the qualified name has no
    matching row in the store for the given `source_connection_id`.
    """


class ColumnInfo(BaseModel):
    """One column inside a `TableDescription`. Raw `data_type` (the
    source's original type string, e.g. `"VARCHAR(255)"`) is exposed
    as-is so an agent sees what it would write in SQL. Type
    normalization (`raw_data_type` → canonical enum) stays deferred —
    track in deferred-decisions doc.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    data_type: str
    nullable: bool
    default: str | None = None
    is_primary_key: bool = False
    description: str = Field(
        default="",
        description="LLM-generated semantic description, or empty string if "
        "the column was indexed without enrichment.",
    )


class ForeignKeyInfo(BaseModel):
    """One FK on a `TableDescription`. `target_qualified_name` is
    pre-joined as `schema.table` so an agent doesn't have to assemble
    it. `source_columns`/`target_columns` are lists (not tuples) for
    JSON friendliness.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    source_columns: list[str]
    target_qualified_name: str
    target_columns: list[str]


class TableDescription(BaseModel):
    """Return shape for `describe_table`. Everything an agent needs to
    understand the table at a single look — structure, semantics, and
    join targets — without making a second round-trip.
    """

    model_config = ConfigDict(frozen=True)

    qualified_name: str
    schema_name: str
    name: str
    columns: list[ColumnInfo]
    foreign_keys: list[ForeignKeyInfo]
    token_estimate: int


class TableHit(BaseModel):
    """One ranked hit returned by `find_relevant_tables`. `score` is the
    raw cosine similarity (0..1) between the query and the best-matching
    column's stored embedding. `best_column` and `best_column_description`
    surface WHY the table was matched so the agent can decide whether
    to dig deeper via `describe_table`.
    """

    model_config = ConfigDict(frozen=True)

    qualified_name: str
    score: float
    best_column: str
    best_column_description: str
    token_estimate: int


def _token_estimate_of(model: BaseModel) -> int:
    """Rough token count for a Pydantic payload, via JSON-length / 4."""
    serialized = model.model_dump_json()
    return max(1, len(serialized) // _CHARS_PER_TOKEN)


def _with_token_estimate(model: _M) -> _M:
    """Return a copy of `model` with `token_estimate` set to a fresh
    estimate of itself. Encapsulates the two-pass build (Pydantic
    frozen models leave no other clean path).

    Off-by-one is acceptable: the estimate is computed against a JSON
    blob where `token_estimate` was the placeholder `0`, then the final
    blob has the real value (1-3 more chars). At char/4 granularity
    this is at most a 1-token error on a rough estimate.
    """
    return model.model_copy(update={"token_estimate": _token_estimate_of(model)})


def _cosine(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    """Cosine similarity. Returns 0.0 on zero-norm vectors. Raises on
    dimension mismatch — that's an embedder swap without re-index, a
    programming error worth surfacing loudly.
    """
    if len(a) != len(b):
        raise ValueError(
            f"vector dimension mismatch — query has {len(a)}, stored has {len(b)}. "
            f"The embedder used at MCP-call time differs from the one used at "
            f"index time. Wipe the store and re-index with the new embedder."
        )
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    # Lengths pre-checked above; `strict=False` keeps ruff B905 quiet.
    for ai, bi in zip(a, b, strict=False):
        dot += ai * bi
        norm_a += ai * ai
        norm_b += bi * bi
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


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


def _parse_qualified_name(qualified_name: str) -> tuple[str, str]:
    """Split `"schema.name"` into `(schema, name)`. Raises `ValueError`
    on malformed input — exactly one dot, both parts non-empty.
    """
    parts = qualified_name.split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"qualified_name must be exactly `schema.name`, got {qualified_name!r}")
    return parts[0], parts[1]


def describe_table_impl(
    *,
    store: SQLiteStore,
    source_connection_id: str,
    qualified_name: str,
) -> TableDescription:
    """Return a full `TableDescription` for `qualified_name`.

    Raises:
        ValueError: if `qualified_name` is not in `schema.name` form.
        TableNotFoundError: if the table is absent from the store under
            the configured `source_connection_id`.
    """
    schema, name = _parse_qualified_name(qualified_name)

    table = store.get_table(schema, name, source_connection_id=source_connection_id)
    if table is None:
        raise TableNotFoundError(
            f"{qualified_name} is not in the store for source "
            f"{source_connection_id!r}. Run `schemabrain index` against the "
            f"source database first."
        )

    descriptions = store.get_table_descriptions(
        schema, name, source_connection_id=source_connection_id
    )

    # `Table.columns` already comes back ordered by ordinal_position
    # from the store; preserve that.
    columns = [
        ColumnInfo(
            name=c.name,
            data_type=c.data_type,
            nullable=c.nullable,
            default=c.default,
            is_primary_key=c.is_primary_key,
            description=descriptions[c.name].text if c.name in descriptions else "",
        )
        for c in table.columns
    ]
    foreign_keys = [
        ForeignKeyInfo(
            name=fk.name,
            source_columns=list(fk.source_columns),
            target_qualified_name=f"{fk.target_schema}.{fk.target_table}",
            target_columns=list(fk.target_columns),
        )
        for fk in table.foreign_keys
    ]

    partial = TableDescription(
        qualified_name=qualified_name,
        schema_name=schema,
        name=name,
        columns=columns,
        foreign_keys=foreign_keys,
        token_estimate=0,  # placeholder; rebuilt by _with_token_estimate
    )
    return _with_token_estimate(partial)
