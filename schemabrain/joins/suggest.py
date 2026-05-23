"""Canonical-join suggestion pipeline.

Merges two evidence sources into a single ranked list of
`JoinCandidate`s:

  - **FK seeds** (always-on, confidence: `"high"`) — declared FK
    constraints between physical tables that both back an entity.
    Deterministic; gives the suggester precision.

  - **Query-log predicates** (when `pg_stat_statements` populated,
    confidence: `"medium"` or `"low"` by frequency) — observed
    equi-join predicates from real workload SQL via
    `joins/mining.py`. Surfaces "implicit joins" that lack FK
    constraints (legacy schemas, denormalised event tables, etc.).

The pipeline output is the suggestion ENVELOPE — it carries provenance
(`confidence`, `evidence`, `fk_name`, `query_log_frequency`,
`rationale`) for review surfaces (dry-run CLI, `--out-dir` YAML
preamble). When the user applies a candidate, the persisted
`CanonicalJoin` is CLEAN — no provenance bleeds into the canonical
record. Mirrors PR #29's `EntityCandidate` discipline.

Merge rule: for each `(source_entity, target_entity, on)` triple,
keep the candidate with the highest evidence. FK evidence wins over
query-log; tied FK-only triples pick by FK name alphabetically. The
emitted candidate may carry BOTH evidence tags (when FK constraint
AND query-log frequency both observed the same join).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from schemabrain.core.entity import Entity
from schemabrain.core.join import CanonicalJoin, Cardinality, JoinColumnPair, JoinOrigin
from schemabrain.core.models import ForeignKey, Table
from schemabrain.core.store_protocol import Store
from schemabrain.joins.mining import (
    AggregatedJoin,
    ExtractedJoin,
    aggregate_join_predicates,
    confidence_for_frequency,
    extract_join_predicates,
)

_logger = logging.getLogger(__name__)

JoinConfidence = Literal["high", "medium", "low"]
JoinEvidence = Literal["foreign_key", "query_log"]


@dataclass(frozen=True)
class JoinCandidate:
    """One suggested canonical join with full provenance.

    `entity_pair_key` is `(source_entity, target_entity, frozen-on-pairs)`
    — the canonical merge key that lets FK + query-log evidence about
    the same join combine into one candidate. Surfaced on the
    dataclass for tests; not user-facing.

    `name` is auto-generated: prefer FK constraint name (cleaned), fall
    back to `{source_entity}_{target_entity}` (or with a suffix if
    multi-canonical between the same pair). User edits in the YAML
    before `joins apply`.
    """

    name: str
    source_entity: str
    target_entity: str
    on: tuple[JoinColumnPair, ...]
    confidence: JoinConfidence
    evidence: tuple[JoinEvidence, ...]
    fk_name: str | None
    query_log_frequency: int
    rationale: str
    # v14: cardinality inferred from FK + PK info at suggestion time.
    # `None` when the FK is query-log-only (no constraint to inspect)
    # or when the FK columns + target PK don't match a recognisable
    # shape. Carried through to the persisted `CanonicalJoin` so the
    # fan-out detector + planner see a precise signal rather than
    # the conservative "treat unknown as many_to_many" default.
    cardinality: Cardinality | None = None

    def __post_init__(self) -> None:
        # Every code path in `_merge_evidence_sources` produces at
        # least one evidence tag — but the dataclass is public, and a
        # caller constructing a synthetic `JoinCandidate` (e.g., a
        # test) could inadvertently produce `evidence=()`, making the
        # confidence/rationale fields semantically contradictory.
        # Refusing empty `evidence` here makes the invariant explicit.
        if not self.evidence:
            raise ValueError(
                "JoinCandidate.evidence must be non-empty — every "
                "candidate must have at least one evidence source "
                "('foreign_key' and/or 'query_log')"
            )

    def to_canonical_join(self, origin: JoinOrigin = "suggested") -> CanonicalJoin:
        """Convert this candidate to a persistable `CanonicalJoin`.

        Drops the suggestion-envelope metadata (`confidence`,
        `evidence`, `fk_name`, `query_log_frequency`, `rationale`). The
        persisted row is provenance-clean — its origin tells the audit
        log how it got there; the inference artifacts ride on the
        suggestion envelope only.

        `origin` defaults to `"suggested"` for the suggester's
        `--apply` path. The standalone `joins apply <yaml>` flow uses
        `"manual"` instead. Typed as `JoinOrigin` so the type checker
        rejects any caller passing a bare `str` that isn't one of the
        three valid values.

        v14: derives the charter-v1.2 `inference_method` from the
        evidence tags. An FK-backed candidate (with or without
        query-log corroboration) writes `fk_constraint` — the join
        is DB-validated. A query-log-only candidate writes
        `observed_in_query_log` — the join is inferred from observed
        SQL, not a declared constraint. `validation_state` lands on
        `applied` for both: the operator's `--apply` gesture stored
        the row, but no human-confirmation step has happened yet.
        """
        if "foreign_key" in self.evidence:
            inference_method = "fk_constraint"
        else:
            inference_method = "observed_in_query_log"
        return CanonicalJoin(
            name=self.name,
            description="",  # suggester leaves description blank; user fills in
            source_entity=self.source_entity,
            target_entity=self.target_entity,
            on=self.on,
            origin=origin,
            cardinality=self.cardinality,
            inference_method=inference_method,
            validation_state="applied",
        )


@dataclass(frozen=True)
class _FkSeed:
    """Internal: an FK constraint mapped to its entity-level shape."""

    fk_name: str
    source_entity: str
    target_entity: str
    on: tuple[JoinColumnPair, ...]
    cardinality: Cardinality | None = None


@dataclass(frozen=True)
class _QueryLogEvidence:
    """Internal: a query-log aggregate mapped to its entity-level shape."""

    source_entity: str
    target_entity: str
    on: tuple[JoinColumnPair, ...]
    frequency: int


# ----- public entry point ----------------------------------------------------


def suggest_canonical_joins(
    *,
    store: Store,
    source_connection_id: str,
) -> list[JoinCandidate]:
    """Mine FK + query-log evidence; return ranked candidate list.

    Output ordering:
      1. Descending by confidence (`high` > `medium` > `low`)
      2. Descending by `query_log_frequency` (FK-only candidates have
         frequency 0; FK+query-log candidates sort above FK-only)
      3. Ascending by `name` for deterministic tiebreak

    Candidates whose underlying physical tables don't both back an
    indexed entity are dropped silently — they would have no persistable
    `source_entity` / `target_entity` to write to. The pipeline logs
    a DEBUG count of dropped predicates so an operator debugging
    "why is my join missing?" can trace the gap.
    """
    entities = store.list_entities(source_connection_id=source_connection_id)
    table_to_entity = _build_table_to_entity_map(entities)
    if not table_to_entity:
        _logger.debug(
            "no entities present for source %r — suggester yields empty "
            "(define entities first via `schemabrain entities apply`)",
            source_connection_id,
        )
        return []

    fk_rows = store.list_all_foreign_keys(source_connection_id=source_connection_id)
    fk_seeds = _build_fk_seeds(
        fk_rows,
        table_to_entity,
        store=store,
        source_connection_id=source_connection_id,
    )

    aggregated_predicates = _load_aggregated_query_log(
        store, source_connection_id=source_connection_id
    )
    query_log_evidence = _build_query_log_evidence(aggregated_predicates, table_to_entity)

    candidates = _merge_evidence_sources(fk_seeds, query_log_evidence)
    candidates.sort(
        key=lambda c: (
            _confidence_rank(c.confidence),
            -c.query_log_frequency,
            c.name,
        )
    )
    return candidates


# ----- entity / table mapping ------------------------------------------------


def _build_table_to_entity_map(entities: Sequence[Entity]) -> dict[str, str]:
    """Return `{qualified_table: entity_name}` for direction-fast lookup.

    Multiple entities backing the same physical table is technically
    possible at v1 (the entities schema doesn't enforce unique bindings)
    but operationally a bug — the suggester picks the
    alphabetically-first one and emits a DEBUG diagnostic so a
    contributor debugging "why am I seeing one entity not the other"
    can trace it.
    """
    mapping: dict[str, str] = {}
    for entity in entities:
        qn = entity.binding.qualified_table
        existing = mapping.get(qn)
        if existing is None:
            mapping[qn] = entity.name
            continue
        # Collision — both the kept and the dropped entity are
        # surfaced at DEBUG so an operator debugging "why is `X`
        # missing from join candidates?" sees the drop regardless of
        # which entity arrived first. The earlier shape only logged
        # the replacement path, hiding the case where the second
        # entity loses.
        if entity.name < existing:
            winner, loser = entity.name, existing
            mapping[qn] = entity.name
        else:
            winner, loser = existing, entity.name
        _logger.debug(  # pragma: no cover
            "multiple entities bind physical table %r — "
            "suggester keeps alphabetically-first (%r); dropped: %r",
            qn,
            winner,
            loser,
        )
    return mapping


# ----- FK seed extraction ----------------------------------------------------


def _build_fk_seeds(
    fk_rows: Sequence[tuple[str, str, ForeignKey]],
    table_to_entity: dict[str, str],
    *,
    store: Store,
    source_connection_id: str,
) -> list[_FkSeed]:
    """Map each FK to its entity-level seed, dropping FKs whose endpoints
    don't both back an entity.

    Cardinality is inferred from FK + PK info: when `fk.target_columns`
    match the target table's primary-key column set exactly, the target
    is uniquely identified by the FK and the join is `many_to_one`
    (the typical OLTP shape — `orders.user_id` → `users.id`).
    `one_to_one` requires both sides to be unique. Falls back to `None`
    when table info is unavailable or the column shape doesn't match
    a recognisable pattern.
    """
    # Cache table lookups so multi-FK tables don't re-fetch.
    table_cache: dict[str, Table | None] = {}

    def _table(schema: str, name: str) -> Table | None:
        key = f"{schema}.{name}"
        if key not in table_cache:
            table_cache[key] = store.get_table(
                schema, name, source_connection_id=source_connection_id
            )
        return table_cache[key]

    seeds: list[_FkSeed] = []
    for source_schema, source_table, fk in fk_rows:
        source_qn = f"{source_schema}.{source_table}"
        target_qn = f"{fk.target_schema}.{fk.target_table}"
        source_entity = table_to_entity.get(source_qn)
        target_entity = table_to_entity.get(target_qn)
        if source_entity is None or target_entity is None:
            # Honors the public `suggest_canonical_joins` docstring's
            # promise that drops are surfaced at DEBUG so an operator
            # debugging "why is my FK-backed join missing?" can trace
            # the gap. The most common cause is one endpoint having
            # no matching entity definition.
            _logger.debug(  # pragma: no cover
                "FK %r dropped from candidates: %s not backed by an entity",
                fk.name,
                "source table" if source_entity is None else "target table",
            )
            continue
        if source_entity == target_entity:
            # Self-referential FK — refused by the CanonicalJoin
            # dataclass invariant. Suggester drops these silently
            # so the operator's `--out-dir` listing isn't polluted
            # with candidates that would never apply. The workaround
            # documented in CanonicalJoin's docstring (split the
            # entity into two — e.g. `manager` and `direct_report`)
            # is the path forward for users who hit this.
            _logger.debug(
                "self-FK on entity %r dropped from join candidates "
                "(self-joins are not supported; split into two entities)",
                source_entity,
            )
            continue
        pairs = tuple(
            JoinColumnPair(source_column=src, target_column=tgt)
            for src, tgt in zip(fk.source_columns, fk.target_columns, strict=True)
        )
        cardinality = _infer_fk_cardinality(
            fk=fk,
            source_table=_table(source_schema, source_table),
            target_table=_table(fk.target_schema, fk.target_table),
        )
        seeds.append(
            _FkSeed(
                fk_name=fk.name,
                source_entity=source_entity,
                target_entity=target_entity,
                on=pairs,
                cardinality=cardinality,
            )
        )
    return seeds


def _infer_fk_cardinality(
    *,
    fk: ForeignKey,
    source_table: Table | None,
    target_table: Table | None,
) -> Cardinality | None:
    """Best-effort cardinality from FK columns + primary-key info.

    Pragmatic rules (UNIQUE-index info isn't surfaced through the Store
    Protocol yet, so PK is the proxy for "uniquely identified"):

      - Target FK columns == target table's PK column set → target
        side is unique → at most ONE target row per source row.
      - Source FK columns == source table's PK column set → source
        side is unique → at most ONE source row per target row.
      - Both unique → `one_to_one` (supertype/subtype pattern).
      - Only target unique → `many_to_one` (typical OLTP FK).
      - Only source unique → `one_to_many` (rare — usually the FK
        is on the "many" side, but a 1:1+ shape can land here).
      - Neither side recognisable → `None` (defensive).

    Returning `None` keeps the v1 worst-case behavior — the fan-out
    detector still flags the join — rather than guessing wrong. Most
    real-world FKs land on the `many_to_one` branch which removes a
    spurious fan-out warning.
    """
    if target_table is None:
        return None
    target_pk = frozenset(target_table.primary_key_columns())
    target_fk_cols = frozenset(fk.target_columns)
    target_unique = bool(target_pk) and target_pk == target_fk_cols

    source_unique: bool = False
    if source_table is not None:
        source_pk = frozenset(source_table.primary_key_columns())
        source_fk_cols = frozenset(fk.source_columns)
        source_unique = bool(source_pk) and source_pk == source_fk_cols

    if target_unique and source_unique:
        return "one_to_one"
    if target_unique:
        return "many_to_one"
    if source_unique:
        return "one_to_many"
    return None


# ----- query-log aggregation -------------------------------------------------


def _load_aggregated_query_log(store: Store, *, source_connection_id: str) -> list[AggregatedJoin]:
    """Read every example query for the source, dedupe by sql_text, and
    aggregate predicates by frequency.

    The `example_queries` table stores one row per `(sql_text,
    table_touched)` pair — a 3-table JOIN produces 3 rows with
    identical sql_text. We dedupe by `sql_text` so each statement is
    parsed exactly once. The `observation_count` is identical across
    those rows by construction (per `mining/pipeline.py`'s write
    path), so the dedupe pick is arbitrary on count.
    """
    rows = store.list_all_example_queries(source_connection_id=source_connection_id)
    seen_sql: dict[str, int] = {}
    for row in rows:
        if row.sql_text not in seen_sql:
            seen_sql[row.sql_text] = row.observation_count

    extracted_with_count: list[tuple[ExtractedJoin, int]] = []
    for sql_text, observation_count in seen_sql.items():
        for extracted in extract_join_predicates(sql_text):
            extracted_with_count.append((extracted, observation_count))

    return aggregate_join_predicates(extracted_with_count)


def _build_query_log_evidence(
    aggregated: Iterable[AggregatedJoin],
    table_to_entity: dict[str, str],
) -> list[_QueryLogEvidence]:
    """Map aggregated query-log predicates to entity-level evidence."""
    evidence: list[_QueryLogEvidence] = []
    for agg in aggregated:
        qn_a = f"{agg.join.schema_a}.{agg.join.table_a}"
        qn_b = f"{agg.join.schema_b}.{agg.join.table_b}"
        entity_a = table_to_entity.get(qn_a)
        entity_b = table_to_entity.get(qn_b)
        if entity_a is None or entity_b is None:
            # Same diagnostic promise as `_build_fk_seeds` — the
            # docstring on `suggest_canonical_joins` says drops are
            # logged for the operator to trace. The query-log
            # predicate references a table that isn't backed by an
            # entity (typical when the user has indexed but not yet
            # defined an entity for some tables).
            _logger.debug(
                "query-log join %r ↔ %r dropped from candidates: %s not backed by an entity",
                qn_a,
                qn_b,
                "left table" if entity_a is None else "right table",
            )
            continue
        if entity_a == entity_b:  # pragma: no cover
            # Same-entity join in query log — typically aliased
            # self-references that resolved to the same underlying
            # table. Drop for the same reason as FK self-joins:
            # CanonicalJoin refuses self-references by invariant.
            continue
        # The mining normalisation puts the schema-alphabetically-
        # lesser table first; we preserve that ordering as
        # `source_entity` so the suggester's output direction is
        # deterministic. The user can flip at apply time.
        pairs = tuple(
            JoinColumnPair(source_column=src, target_column=tgt) for src, tgt in agg.join.pairs
        )
        evidence.append(
            _QueryLogEvidence(
                source_entity=entity_a,
                target_entity=entity_b,
                on=pairs,
                frequency=agg.frequency,
            )
        )
    return evidence


# ----- merge step ------------------------------------------------------------


def _candidate_key(
    source_entity: str,
    target_entity: str,
    on: tuple[JoinColumnPair, ...],
) -> tuple[str, str, tuple[tuple[str, str], ...]]:
    """Canonical merge key for an entity-level join shape.

    Direction is preserved (not normalised) — FK semantics carry a real
    direction (owner → referenced) the suggester respects. The merge
    layer matches FK seeds to query-log evidence whose entity-level
    shape is identical, including direction.
    """
    pair_tuples = tuple((p.source_column, p.target_column) for p in on)
    return (source_entity, target_entity, pair_tuples)


def _merge_evidence_sources(
    fk_seeds: Sequence[_FkSeed],
    query_log_evidence: Sequence[_QueryLogEvidence],
) -> list[JoinCandidate]:
    """Combine FK + query-log into one candidate per `(source, target, on)`.

    Match rule:
      - FK seed key == query-log evidence key → merged candidate with
        BOTH evidence tags, FK-derived confidence (`high`), query-log
        frequency carried for the envelope.
      - FK seed without matching query-log → FK-only candidate,
        confidence `high`, frequency 0.
      - Query-log evidence without matching FK → query-log-only
        candidate, confidence `medium`/`low` per frequency, fk_name
        None.

    Direction-aware key: a query-log entry's direction must match the
    FK seed's direction to merge. If only one direction matches, the
    other surfaces as a separate (non-FK) candidate. This is rare in
    practice — query-log normalisation tends to converge on FK
    direction when one exists.

    Candidate names follow:
      - FK-backed: cleaned FK name (strip `_fkey` suffix), fall back
        to `<source>_<target>` if FK name is uninformative.
      - Query-log-only: `<source>_<target>` (or with numeric suffix
        if multiple distinct `on` shapes between the same pair).
    """
    fk_by_key: dict[tuple[str, str, tuple[tuple[str, str], ...]], _FkSeed] = {}
    for seed in fk_seeds:
        key = _candidate_key(seed.source_entity, seed.target_entity, seed.on)
        # Multiple FKs with the same shape (extremely rare) — keep the
        # alphabetically-first FK name.
        existing = fk_by_key.get(key)
        if existing is None or seed.fk_name < existing.fk_name:  # pragma: no branch
            fk_by_key[key] = seed

    query_log_by_key: dict[tuple[str, str, tuple[tuple[str, str], ...]], _QueryLogEvidence] = {}
    for ev in query_log_evidence:
        key = _candidate_key(ev.source_entity, ev.target_entity, ev.on)
        # Defensive dedupe — the aggregator should have collapsed
        # identical keys already, but be explicit about the contract.
        existing = query_log_by_key.get(key)
        if existing is None or ev.frequency > existing.frequency:  # pragma: no branch
            query_log_by_key[key] = ev

    # Track used names so multi-candidate-per-entity-pair gets a
    # numeric suffix on the second+ occurrence rather than silently
    # producing duplicate names that collide at write time.
    used_names: set[str] = set()
    candidates: list[JoinCandidate] = []

    # 1. FK-backed candidates (matched or unmatched).
    for key, seed in fk_by_key.items():
        ql = query_log_by_key.get(key)
        if ql is not None:
            evidence: tuple[JoinEvidence, ...] = ("foreign_key", "query_log")
            frequency = ql.frequency
            rationale = (
                f"Declared FK {seed.fk_name!r} confirmed by "
                f"{frequency} observed joins in the query log."
            )
        else:
            evidence = ("foreign_key",)
            frequency = 0
            rationale = f"Declared FK {seed.fk_name!r}."
        name = _allocate_name(
            preferred=_clean_fk_name(seed.fk_name, seed.source_entity, seed.target_entity),
            used=used_names,
        )
        candidates.append(
            JoinCandidate(
                name=name,
                source_entity=seed.source_entity,
                target_entity=seed.target_entity,
                on=seed.on,
                confidence="high",
                evidence=evidence,
                fk_name=seed.fk_name,
                query_log_frequency=frequency,
                rationale=rationale,
                cardinality=seed.cardinality,
            )
        )

    # 2. Query-log-only candidates (no FK match).
    for key, ql in query_log_by_key.items():
        if key in fk_by_key:
            continue
        name = _allocate_name(
            preferred=f"{ql.source_entity}_{ql.target_entity}",
            used=used_names,
        )
        confidence = confidence_for_frequency(ql.frequency)
        rationale = (
            f"Observed in query log with frequency {ql.frequency}; "
            f"no declared FK between {ql.source_entity!r} and "
            f"{ql.target_entity!r}."
        )
        candidates.append(
            JoinCandidate(
                name=name,
                source_entity=ql.source_entity,
                target_entity=ql.target_entity,
                on=ql.on,
                confidence=confidence,  # type: ignore[arg-type]
                evidence=("query_log",),
                fk_name=None,
                query_log_frequency=ql.frequency,
                rationale=rationale,
                cardinality=None,
            )
        )

    return candidates


def _clean_fk_name(fk_name: str, source_entity: str, target_entity: str) -> str:
    """Derive a candidate join name from an FK constraint name.

    Removes the common `_fkey` Postgres suffix and `_fk` shorthand.
    Falls back to `<source>_<target>` when the FK name is empty or
    reduces to an empty identifier after cleaning. The output is the
    raw preferred name — `_allocate_name` handles uniqueness.
    """
    cleaned = fk_name
    for suffix in ("_fkey", "_fk"):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
            break
    if not cleaned:
        return f"{source_entity}_{target_entity}"
    return cleaned


def _allocate_name(*, preferred: str, used: set[str]) -> str:
    """Reserve a unique candidate name, appending `_2`, `_3`, … on collision.

    Mutates `used` to mark the chosen name as taken. Pure function
    semantics aside — this is the candidate-naming loop's natural
    shape.
    """
    if preferred not in used:
        used.add(preferred)
        return preferred
    counter = 2
    while True:
        candidate = f"{preferred}_{counter}"
        if candidate not in used:
            used.add(candidate)
            return candidate
        counter += 1


_CONFIDENCE_RANK_MAP: dict[str, int] = {"high": 0, "medium": 1, "low": 2}


def _confidence_rank(confidence: JoinConfidence) -> int:
    """Numeric sort key for confidence — lower is better (descending)."""
    return _CONFIDENCE_RANK_MAP[confidence]


# ----- cycle detection -------------------------------------------------------


@dataclass(frozen=True)
class JoinGraphReport:
    """Structural-analysis report on the canonical-join graph.

    Surfaced by `joins suggest --report` for operator awareness. Per
    the design, cycles are NOT a write refusal — they're legal in real
    schemas (department → manager → user → department). Reporting
    visibility lets operators see what they have without forcing
    decisions.

    `isolated_entities` lists entities that exist in the store but
    don't appear in any canonical join. Requires `all_entity_names`
    to be passed to `detect_cycles_in_join_graph` — without it, the
    field stays an empty tuple (the analyser can't conjure entities
    out of the join set alone).
    """

    cycles: tuple[tuple[str, ...], ...]
    isolated_entities: tuple[str, ...]
    max_path_length: int


def detect_cycles_in_join_graph(
    joins: Sequence[CanonicalJoin],
    *,
    all_entity_names: Iterable[str] | None = None,
) -> JoinGraphReport:
    """Walk the directed `source_entity → target_entity` graph for cycles.

    Algorithm: iterative DFS with explicit recursion stack. Each
    entity is visited once; back-edges (a successor already on the
    current DFS stack) indicate a cycle, recorded as the path from the
    back-edge target through the current node.

    `isolated_entities` lists entities present in `all_entity_names`
    that don't appear in any canonical join. When `all_entity_names`
    is `None`, the field is empty (we can't compute isolation without
    knowing what entities exist). The CLI passes the full entity list
    from the store so the report is meaningful for `--report`
    consumers.

    `max_path_length` is the longest simple path from any DFS root.

    Worst-case complexity is O(V * (V + E)) because there is no
    cross-root visited set — a node reachable from multiple DFS roots
    is re-explored per root. For v1 scale (tens to low hundreds of
    entities) this is well below any observable cost.
    """
    if not joins:
        isolated = tuple(sorted(all_entity_names)) if all_entity_names is not None else ()
        return JoinGraphReport(
            cycles=(),
            isolated_entities=isolated,
            max_path_length=0,
        )

    # Build the adjacency list.
    adjacency: dict[str, list[str]] = {}
    connected: set[str] = set()
    for join in joins:
        adjacency.setdefault(join.source_entity, []).append(join.target_entity)
        connected.add(join.source_entity)
        connected.add(join.target_entity)
    for neighbors in adjacency.values():
        neighbors.sort()  # deterministic walk

    cycles: list[tuple[str, ...]] = []
    max_path_length = 0

    # Iterative DFS per entity. Tracks (node, neighbor_index, path_so_far).
    for root in sorted(connected):
        if root not in adjacency:
            continue
        stack: list[tuple[str, int]] = [(root, 0)]
        path: list[str] = [root]
        on_stack: set[str] = {root}
        while stack:
            node, neighbor_idx = stack[-1]
            neighbors = adjacency.get(node, [])
            if neighbor_idx >= len(neighbors):
                # Backtrack.
                stack.pop()
                popped = path.pop()
                on_stack.discard(popped)
                continue
            # Advance the neighbor cursor BEFORE recursing so backtracking
            # picks up the next sibling correctly.
            stack[-1] = (node, neighbor_idx + 1)
            neighbor = neighbors[neighbor_idx]
            if neighbor in on_stack:
                # Back-edge: cycle from `neighbor` through `path` to `node`.
                cycle_start_idx = path.index(neighbor)
                cycle = (*path[cycle_start_idx:], neighbor)
                cycles.append(cycle)
                continue
            if neighbor in adjacency:
                stack.append((neighbor, 0))
                path.append(neighbor)
                on_stack.add(neighbor)
                max_path_length = max(max_path_length, len(path))
            else:
                # Leaf node — still counts toward path length.
                max_path_length = max(max_path_length, len(path) + 1)

    # Dedupe cycles by canonical rotation: each cycle has multiple
    # representations depending on the starting node. Rotate each to
    # start at its alphabetically-smallest node, then dedupe by that
    # form.
    canonical_cycles = {_canonical_cycle(c) for c in cycles}
    sorted_cycles = tuple(sorted(canonical_cycles))

    if all_entity_names is None:
        isolated: tuple[str, ...] = ()
    else:
        # Entities present in the store but not appearing in any
        # canonical join (neither source nor target).
        isolated = tuple(sorted(set(all_entity_names) - connected))

    return JoinGraphReport(
        cycles=sorted_cycles,
        isolated_entities=isolated,
        max_path_length=max_path_length,
    )


def _canonical_cycle(cycle: tuple[str, ...]) -> tuple[str, ...]:
    """Rotate a cycle so it starts at its alphabetically-smallest node.

    A cycle `[a, b, c, a]` and `[b, c, a, b]` are the same cycle
    written from different start points. Canonical form starts at
    the smallest node. The trailing repeat is dropped from the
    rotation logic and re-added at the end.
    """
    if not cycle:  # pragma: no cover — DFS only produces non-empty cycles; defensive guard
        return cycle
    # The last element is the cycle's repeat of its first element —
    # drop it before rotating.
    body = list(cycle[:-1])
    if not body:  # pragma: no cover — single-element cycles aren't emitted at v1 (self-joins refused at dataclass layer)
        return cycle
    min_idx = body.index(min(body))
    rotated = body[min_idx:] + body[:min_idx]
    rotated.append(rotated[0])
    return tuple(rotated)
