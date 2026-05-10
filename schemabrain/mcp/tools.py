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
from collections import deque
from typing import Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from schemabrain.core.models import ForeignKey
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


class ColumnNotFoundError(LookupError):
    """Raised by `describe_column_impl` when the table exists but the
    requested column does not. Distinct from `TableNotFoundError` so
    callers can route the two cases differently.
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


class IncomingForeignKeyInfo(BaseModel):
    """One FK pointing INTO the column being described — i.e., another
    table joins to us here. `source_qualified_name` is the referencing
    table (`schema.table` pre-joined). `source_columns` is what they
    use to join; `target_columns` is what they target on OUR table
    (always includes the column being described).

    Symmetric counterpart of `ForeignKeyInfo`, which describes outgoing
    FKs. The two shapes are kept separate so the JSON the agent sees
    makes the direction explicit.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    source_qualified_name: str
    source_columns: list[str]
    target_columns: list[str]


class ColumnDetail(BaseModel):
    """Return shape for `describe_column`. Everything an agent needs to
    understand one column — structure + semantics + the join graph it
    participates in (both directions).

    `outgoing_foreign_keys` lists FKs where this column appears in the
    source list (this column joins out to another table). When the
    column is part of a composite FK, the full FK row is returned —
    including sibling source columns.

    `incoming_foreign_keys` lists FKs where this column appears in the
    target list (other tables join in here). Same composite-FK
    handling: the full source row is returned even if our column is
    just one of several target columns.

    Stats (NULL%, distinct_count, sample_values) are deferred — the
    indexer profiles them but doesn't yet persist them. See
    deferred-decisions doc.
    """

    model_config = ConfigDict(frozen=True)

    qualified_name: str
    schema_name: str
    table_name: str
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
    outgoing_foreign_keys: list[ForeignKeyInfo]
    incoming_foreign_keys: list[IncomingForeignKeyInfo]
    token_estimate: int


class JoinEdge(BaseModel):
    """One join condition along a `JoinPath`. Path-oriented: `left` is
    the table already in the path; `right` is the table being added.
    `left_columns` and `right_columns` are positionally aligned and ready
    to drop into a SQL `JOIN ... ON left.{col} = right.{col}` clause —
    the agent doesn't have to figure out which side declared the FK.

    `via` is "foreign_key" at v0; once query-log mining ships, edges
    inferred from JOIN patterns in `pg_stat_statements` will set
    `via="query_log"` with a confidence < 1.0.
    """

    model_config = ConfigDict(frozen=True)

    fk_name: str
    left_qualified_name: str
    left_columns: list[str]
    right_qualified_name: str
    right_columns: list[str]
    confidence: float
    via: Literal["foreign_key", "query_log"]


class JoinPath(BaseModel):
    """One sequence of `JoinEdge`s connecting `start_qualified_name` to
    `end_qualified_name`. `edges` is in path order: `edges[0]` joins
    from `start`, `edges[-1]` lands on `end`. `confidence` is the MIN
    across edge confidences (weakest-link).

    `hops` == `len(edges)`. A direct FK is 1 hop. The validator enforces
    this invariant — `JoinPath` is part of the public MCP contract and
    a future caller constructing one directly shouldn't be able to ship
    a corrupt payload silently.
    """

    model_config = ConfigDict(frozen=True)

    start_qualified_name: str
    end_qualified_name: str
    hops: int
    edges: list[JoinEdge]
    confidence: float
    token_estimate: int

    @model_validator(mode="after")
    def _validate_hops(self) -> JoinPath:
        if self.hops != len(self.edges):
            raise ValueError(f"hops ({self.hops}) must equal len(edges) ({len(self.edges)})")
        return self


class SuggestJoinsResult(BaseModel):
    """Return shape for `suggest_joins`. Holds one shortest path per
    pair of input tables that's reachable through the FK graph, plus
    a flat list of unreachable pairs so the caller can see which
    tables the FK graph can't connect.

    Each entry in `unreachable_pairs` is a 2-element list (lists, not
    tuples, for JSON-friendliness) sorted alphabetically — `(lo, hi)`
    so the unordered pair has a canonical form.
    """

    model_config = ConfigDict(frozen=True)

    paths: list[JoinPath]
    unreachable_pairs: list[list[str]]
    token_estimate: int


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


def _parse_column_qualified_name(qualified_name: str) -> tuple[str, str, str]:
    """Split `"schema.table.column"` into `(schema, table, column)`.

    Raises `ValueError` on malformed input — exactly two dots, all
    three parts non-empty. We use plain `split` rather than rsplit
    tricks because Postgres identifiers don't allow embedded dots in
    standard usage; if a future schema needs that, the user will use
    quoting and we'll revisit.
    """
    parts = qualified_name.split(".")
    if len(parts) != 3 or not all(parts):
        raise ValueError(
            f"qualified_name must be exactly `schema.table.column`, got {qualified_name!r}"
        )
    return parts[0], parts[1], parts[2]


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


def describe_column_impl(
    *,
    store: SQLiteStore,
    source_connection_id: str,
    qualified_name: str,
) -> ColumnDetail:
    """Return a full `ColumnDetail` for `schema.table.column`.

    Returns the column's structural metadata, LLM description (if any),
    and BOTH directions of FK participation: outgoing FKs where this
    column is in the source list, and incoming FKs where this column is
    in another table's target list (back-references).

    Raises:
        ValueError: if `qualified_name` is not in `schema.table.column` form.
        TableNotFoundError: if the table is absent from the store under
            the configured `source_connection_id`.
        ColumnNotFoundError: if the table exists but the column doesn't.
    """
    schema, table_name, column_name = _parse_column_qualified_name(qualified_name)

    table = store.get_table(schema, table_name, source_connection_id=source_connection_id)
    if table is None:
        raise TableNotFoundError(
            f"{schema}.{table_name} is not in the store for source "
            f"{source_connection_id!r}. Run `schemabrain index` against the "
            f"source database first."
        )

    column = table.get_column(column_name)
    if column is None:
        raise ColumnNotFoundError(
            f"{qualified_name} does not exist on {schema}.{table_name}. "
            f"Existing columns: {sorted(c.name for c in table.columns)}"
        )

    descriptions = store.get_table_descriptions(
        schema, table_name, source_connection_id=source_connection_id
    )
    description = descriptions[column_name].text if column_name in descriptions else ""

    # Outgoing FKs: walk the table's FK list and pick any whose
    # `source_columns` includes our column name. Composite FKs are
    # surfaced whole — sibling source columns are kept so the agent
    # sees the full join shape.
    outgoing: list[ForeignKeyInfo] = [
        ForeignKeyInfo(
            name=fk.name,
            source_columns=list(fk.source_columns),
            target_qualified_name=f"{fk.target_schema}.{fk.target_table}",
            target_columns=list(fk.target_columns),
        )
        for fk in table.foreign_keys
        if column_name in fk.source_columns
    ]

    # Incoming FKs: ask the store for everything pointing at us.
    # Returns IncomingForeignKey domain objects already filtered to FKs
    # whose `target_columns` includes this column.
    incoming_raw = store.get_foreign_keys_targeting(
        schema, table_name, column_name, source_connection_id=source_connection_id
    )
    incoming: list[IncomingForeignKeyInfo] = [
        IncomingForeignKeyInfo(
            name=ifk.name,
            source_qualified_name=ifk.source_qualified_name,
            source_columns=list(ifk.source_columns),
            target_columns=list(ifk.target_columns),
        )
        for ifk in incoming_raw
    ]

    # Reconstruct from parsed parts rather than echoing the user's raw
    # input. Today the parser doesn't normalize, so this is identical to
    # the input string for any successfully-parsed name — but if a future
    # parser ever lowercases/strips, this keeps `qualified_name` aligned
    # with `schema_name`/`table_name`/`name` instead of silently
    # diverging.
    partial = ColumnDetail(
        qualified_name=f"{schema}.{table_name}.{column_name}",
        schema_name=schema,
        table_name=table_name,
        name=column_name,
        data_type=column.data_type,
        nullable=column.nullable,
        default=column.default,
        is_primary_key=column.is_primary_key,
        description=description,
        outgoing_foreign_keys=outgoing,
        incoming_foreign_keys=incoming,
        token_estimate=0,  # placeholder; rebuilt by _with_token_estimate
    )
    return _with_token_estimate(partial)


# Confidence floor for declared FKs. Query-log-inferred edges (post-Week-8)
# will land below 1.0; keeping the constant lets the formula compose cleanly.
_FK_CONFIDENCE = 1.0
_VIA_FK = "foreign_key"
# Default cap on BFS depth. ~4 covers typical 3NF schemas (e.g.
# user → org → org_member → role) without runaway exploration on
# wide graphs. Override per call if the caller needs more.
_DEFAULT_MAX_HOPS = 4


class FrozenJoinEdgeSeed(BaseModel):
    """Internal: an FK reduced to the data BFS needs to materialize a
    `JoinEdge` once direction-of-traversal is known. Distinct from
    `JoinEdge` because the public shape is path-oriented (`left`/`right`)
    while the seed is FK-oriented (`owner`/`referenced`).
    """

    model_config = ConfigDict(frozen=True)

    fk_name: str
    owner_qualified_name: str
    owner_columns: tuple[str, ...]
    referenced_qualified_name: str
    referenced_columns: tuple[str, ...]


def _build_fk_adjacency(
    fk_rows: list[tuple[str, str, ForeignKey]],
) -> dict[str, list[tuple[str, str, FrozenJoinEdgeSeed]]]:
    """Build an undirected adjacency map for FK BFS.

    Returned shape: `{node: [(neighbor, sort_key, seed), ...]}`. `seed`
    holds enough info to materialize a `JoinEdge` once we know the
    traversal direction. `sort_key` makes BFS deterministic.

    Each FK contributes TWO entries — one in each direction. Same `seed`
    is reused; the BFS picks the per-traversal-direction `JoinEdge`
    later via `_edge_from_seed`. Self-referential FKs (source == target)
    add two entries pointing to the same node — harmless because BFS's
    `visited` set rejects them on first discovery, and the column shape
    is correct because forward and reverse coincide for self-joins.
    """
    adjacency: dict[str, list[tuple[str, str, FrozenJoinEdgeSeed]]] = {}
    for source_schema, source_table, fk in fk_rows:
        source_qn = f"{source_schema}.{source_table}"
        target_qn = f"{fk.target_schema}.{fk.target_table}"
        seed = FrozenJoinEdgeSeed(
            fk_name=fk.name,
            owner_qualified_name=source_qn,
            owner_columns=tuple(fk.source_columns),
            referenced_qualified_name=target_qn,
            referenced_columns=tuple(fk.target_columns),
        )
        # forward: source → target
        adjacency.setdefault(source_qn, []).append((target_qn, fk.name, seed))
        # reverse: target → source (same FK, same seed)
        adjacency.setdefault(target_qn, []).append((source_qn, fk.name, seed))

    # Sort each node's adjacency list deterministically: by neighbor
    # qualified name, then FK name. BFS picks the first match.
    for neighbors in adjacency.values():
        neighbors.sort(key=lambda x: (x[0], x[1]))
    return adjacency


def _edge_from_seed(seed: FrozenJoinEdgeSeed, *, left: str) -> JoinEdge:
    """Build a path-oriented `JoinEdge` from a seed, given which side
    of the FK is on the LEFT (already in the path).
    """
    if left == seed.owner_qualified_name:
        # Forward traversal — owner is left, referenced is right.
        return JoinEdge(
            fk_name=seed.fk_name,
            left_qualified_name=seed.owner_qualified_name,
            left_columns=list(seed.owner_columns),
            right_qualified_name=seed.referenced_qualified_name,
            right_columns=list(seed.referenced_columns),
            confidence=_FK_CONFIDENCE,
            via=_VIA_FK,
        )
    # Reverse traversal — referenced is left, owner is right. Columns
    # swap so the JOIN-ON pairing remains correct.
    return JoinEdge(
        fk_name=seed.fk_name,
        left_qualified_name=seed.referenced_qualified_name,
        left_columns=list(seed.referenced_columns),
        right_qualified_name=seed.owner_qualified_name,
        right_columns=list(seed.owner_columns),
        confidence=_FK_CONFIDENCE,
        via=_VIA_FK,
    )


def _bfs_shortest_path(
    *,
    adjacency: dict[str, list[tuple[str, str, FrozenJoinEdgeSeed]]],
    start: str,
    end: str,
    max_hops: int,
) -> list[JoinEdge] | None:
    """Standard BFS shortest-path. Returns the list of `JoinEdge`s in
    path order, or `None` if `end` is unreachable from `start` within
    `max_hops`.

    Tiebreak among equally-short paths is by neighbor sort order — the
    adjacency lists are pre-sorted, so the FIRST path discovered for a
    given hop count is the deterministic representative.

    Caller must guarantee `start != end` (and `max_hops >= 1`); the
    public `suggest_joins_impl` enforces both at the boundary.
    """
    # parent[node] = (prev_node, seed_used) — enough to walk back and
    # build edges with the right traversal direction.
    parent: dict[str, tuple[str, FrozenJoinEdgeSeed]] = {}
    visited: set[str] = {start}
    # Frontier as (node, depth). Cap depth at max_hops.
    frontier: deque[tuple[str, int]] = deque([(start, 0)])
    while frontier:
        node, depth = frontier.popleft()
        if depth >= max_hops:
            continue
        for neighbor, _fk_name, seed in adjacency.get(node, []):
            if neighbor in visited:
                continue
            visited.add(neighbor)
            parent[neighbor] = (node, seed)
            if neighbor == end:
                # Reconstruct backward, then reverse.
                edges_reversed: list[JoinEdge] = []
                cursor = end
                while cursor != start:
                    prev, seed_used = parent[cursor]
                    edges_reversed.append(_edge_from_seed(seed_used, left=prev))
                    cursor = prev
                edges_reversed.reverse()
                return edges_reversed
            frontier.append((neighbor, depth + 1))
    return None


def suggest_joins_impl(
    *,
    store: SQLiteStore,
    source_connection_id: str,
    tables: list[str],
    max_hops: int = _DEFAULT_MAX_HOPS,
) -> SuggestJoinsResult:
    """Find shortest FK-graph join paths between every pair of input tables.

    Algorithm:
      1. Validate input — at least 2 distinct, parseable, present tables.
      2. Build an undirected FK adjacency map from the store.
      3. For each unordered pair of input tables, BFS the shortest path
         (≤ `max_hops` edges). Equally-short paths break ties by
         alphabetical neighbor order.
      4. Pairs with no path within the bound land in `unreachable_pairs`.
      5. Sort `paths` by `(hops, start_qualified_name, end_qualified_name)`.

    Confidence is `1.0` for every FK at v0 (declared constraints are
    deterministic). Once query-log mining ships, inferred edges will
    contribute lower confidences and `JoinPath.confidence` will become
    a meaningful weakest-link score.

    Raises:
        ValueError: input shape is degenerate — fewer than 2 distinct
            tables, malformed qualified name, or `max_hops <= 0`.
        TableNotFoundError: any input table is not present in the store
            for `source_connection_id`.
    """
    if max_hops <= 0:
        raise ValueError(f"max_hops must be positive, got {max_hops}")

    # Normalize input: dedupe while preserving first-seen ordering. The
    # set walk below is order-insensitive but a deterministic dedupe
    # keeps error messages consistent across runs.
    seen: set[str] = set()
    unique_tables: list[str] = []
    for t in tables:
        if t not in seen:
            seen.add(t)
            unique_tables.append(t)
    if len(unique_tables) < 2:
        raise ValueError(
            f"suggest_joins requires at least 2 distinct tables, "
            f"got {len(unique_tables)} unique entries from {tables!r}"
        )

    # Validate every input parses + exists. Failing fast on a bad input
    # avoids confusing partial results. Loads the full table list once
    # and checks set membership — avoids N round-trips for N inputs and
    # mirrors the bulk-reader pattern used by the FK adjacency below.
    known_tables = {
        f"{schema}.{name}"
        for schema, name in store.list_tables(source_connection_id=source_connection_id)
    }
    for qn in unique_tables:
        _parse_qualified_name(qn)  # raises ValueError on bad form
        if qn not in known_tables:
            raise TableNotFoundError(
                f"{qn} is not in the store for source "
                f"{source_connection_id!r}. Run `schemabrain index` against the "
                f"source database first."
            )

    fk_rows = store.list_all_foreign_keys(source_connection_id=source_connection_id)
    adjacency = _build_fk_adjacency(fk_rows)

    paths: list[JoinPath] = []
    unreachable: list[list[str]] = []

    # Iterate unordered pairs in INPUT order so each path's start/end
    # honors the order the caller listed them. The output list is then
    # re-sorted by (hops, start, end) for determinism — but the
    # orientation of each individual path stays input-faithful so an
    # agent that asked "join A to B" reads a path that flows A → B.
    for i in range(len(unique_tables)):
        for j in range(i + 1, len(unique_tables)):
            start, end = unique_tables[i], unique_tables[j]
            edges = _bfs_shortest_path(adjacency=adjacency, start=start, end=end, max_hops=max_hops)
            if edges is None:
                # Unordered pair → canonicalize alphabetically so the
                # set of unreachable pairs has one stable form regardless
                # of input ordering.
                lo, hi = sorted([start, end])
                unreachable.append([lo, hi])
                continue
            # BFS only returns a non-None list with at least one edge,
            # so `min` always sees at least one value here.
            confidence = min(e.confidence for e in edges)
            partial_path = JoinPath(
                start_qualified_name=start,
                end_qualified_name=end,
                hops=len(edges),
                edges=edges,
                confidence=confidence,
                token_estimate=0,  # placeholder; rebuilt below
            )
            paths.append(_with_token_estimate(partial_path))

    paths.sort(key=lambda p: (p.hops, p.start_qualified_name, p.end_qualified_name))
    unreachable.sort()

    partial = SuggestJoinsResult(
        paths=paths,
        unreachable_pairs=unreachable,
        token_estimate=0,  # placeholder; rebuilt by _with_token_estimate
    )
    return _with_token_estimate(partial)
