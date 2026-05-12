"""MCP tool implementation: suggest_joins."""

from __future__ import annotations

from collections import deque

from pydantic import BaseModel, ConfigDict

from schemabrain.core.models import ForeignKey
from schemabrain.core.store import SQLiteStore
from schemabrain.mcp._helpers import _parse_qualified_name, _with_token_estimate
from schemabrain.mcp.shapes import JoinEdge, JoinPath, SuggestJoinsResult, TableNotFoundError

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
