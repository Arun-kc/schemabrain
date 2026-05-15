"""Pydantic return shapes and exceptions for the MCP tools.

All public response types and the two error classes live here so the
per-tool modules stay focused on logic only.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from schemabrain.core.example_query import ExampleQuerySource
from schemabrain.pii.categories import Sensitivity


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


class ExampleQueryItem(BaseModel):
    """One observed example SQL pattern surfaced through
    `get_example_queries`. `sql_text` is the literal SQL as observed
    (mining-side normalisation happens before write; this layer is
    pass-through). `observation_count` describes how often the same
    pattern was seen. `source` is `pg_stat_statements` for mined rows
    or `curated` for operator-seeded rows; `sensitivity` and
    `pii_categories` carry the Phase B ADR's 2-layer taxonomy.

    Cross-layer invariant (`sensitivity == "pii"` requires at least
    one category; non-pii sensitivity must carry an empty category
    list) is enforced by the model validator — same rule the store-
    side `ExampleQuery.__post_init__` enforces, so an envelope that
    round-trips through this shape preserves the contract.
    """

    model_config = ConfigDict(frozen=True)

    sql_text: str
    observation_count: int
    source: ExampleQuerySource
    sensitivity: Sensitivity
    # Sorted alphabetically — stable order so an agent's downstream
    # comparison logic doesn't have to deal with frozenset/set ordering
    # nondeterminism.
    pii_categories: list[str]

    @model_validator(mode="after")
    def _validate_pii_consistency(self) -> ExampleQueryItem:
        if self.sensitivity == "pii" and not self.pii_categories:
            raise ValueError("sensitivity='pii' requires at least one pii_category")
        if self.sensitivity != "pii" and self.pii_categories:
            raise ValueError(
                f"sensitivity={self.sensitivity!r} cannot carry pii_categories "
                f"(got {self.pii_categories!r})"
            )
        return self


class ExampleQueriesResult(BaseModel):
    """Return shape for `get_example_queries`. v0.5: `queries` is
    populated only for tables that already have rows written by the
    upcoming `pg_stat_statements` mining feature. Until mining lands,
    every table returns `queries=[]` and the MCP tool wraps the result
    in a `status="empty"` envelope.
    """

    model_config = ConfigDict(frozen=True)

    qualified_name: str
    queries: list[ExampleQueryItem]
    token_estimate: int
