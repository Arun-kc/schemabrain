"""Pydantic return shapes and exceptions for the MCP tools.

All public response types and the two error classes live here so the
per-tool modules stay focused on logic only.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from schemabrain.core.entity import Origin
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


class EntityNotFoundError(LookupError):
    """Raised by `describe_entity_impl` when the requested entity name
    has no matching row in the store for the given
    `source_connection_id`. Mirrors `TableNotFoundError` so callers
    can catch either via `LookupError` when they don't care which.
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


class EntitySummary(BaseModel):
    """One entity in the `list_entities` response.

    Lean by design: no `columns` or token estimate. An agent surveying
    "what entities are defined?" wants a short list; it then calls
    `describe_entity(name)` to drill into one. `qualified_table` is the
    `schema.table` form already joined (matches the YAML grammar's
    `binding.single_table` syntax).

    `origin: Origin` is enforced by Pydantic at construction time —
    a wrong value raises `ValidationError`, so callers building
    summaries from raw dicts get the same closed-set guarantee that
    `Entity.__post_init__` provides at the storage layer.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    qualified_table: str
    identity: str
    origin: Origin


class EntityColumn(BaseModel):
    """One column on an entity's bound table.

    Mirrors the per-column shape from `describe_table` plus a
    `pii_sensitivity` field that future PII-redaction work will
    populate. Today every column ships with the default `"public"` —
    the field is inert at this stage but the wire shape is locked
    so the redaction layer can fill it without retrofitting the
    envelope.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    data_type: str
    nullable: bool
    description: str = Field(
        default="",
        description="LLM-generated semantic description, or empty string "
        "if the column was indexed without enrichment.",
    )
    pii_sensitivity: Sensitivity = Field(
        default="public",
        description="PII classification carried through to the agent. "
        "Currently hardcoded to 'public' for every column; a future "
        "release will populate from column-level classification.",
    )


class EntityDetail(BaseModel):
    """Return shape for `describe_entity`.

    Includes every column of the bound table — at this release the
    YAML grammar doesn't yet let an entity allowlist a subset, so
    `columns` is the full underlying table. The agent gets one-look
    access to "what does this entity expose?" without a second
    round-trip to `describe_table`.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    qualified_table: str
    identity: str
    origin: Origin
    columns: list[EntityColumn]
    token_estimate: int


# ----- canonical-join shapes (wk-13) ----------------------------------------


class NoCanonicalJoinError(LookupError):
    """Raised by `resolve_join_impl` when no canonical join exists
    between the requested entity pair.

    Distinct from `EntityNotFoundError` (one or both entities don't
    exist) so the MCP wrapper can route to different error kinds —
    `no_canonical_join` vs `unknown_name`.
    """


class AmbiguousJoinError(LookupError):
    """Raised by `resolve_join_impl` when 2+ canonical joins exist
    between the entity pair and no `name` arg was passed to
    disambiguate.

    Carries the list of candidate join names on `.candidate_names` so
    the MCP wrapper can surface them in the recovery hint.
    """

    def __init__(self, message: str, *, candidate_names: tuple[str, ...]) -> None:
        super().__init__(message)
        self.candidate_names = candidate_names


class JoinNameMismatchError(LookupError):
    """Raised by `resolve_join_impl` when exactly one canonical join
    exists between the entity pair, but the caller passed a `name`
    arg that doesn't match it.

    Carries `.canonical_name` so the recovery message can show the
    one actually-canonical name.
    """

    def __init__(self, message: str, *, canonical_name: str) -> None:
        super().__init__(message)
        self.canonical_name = canonical_name


class UnknownJoinNameError(LookupError):
    """Raised by `resolve_join_impl` when 2+ canonical joins exist
    between the entity pair and the `name` arg doesn't match any of
    them.

    Carries `.candidate_names` so the recovery message can list all
    available names.
    """

    def __init__(self, message: str, *, candidate_names: tuple[str, ...]) -> None:
        super().__init__(message)
        self.candidate_names = candidate_names


class JoinColumnPairInfo(BaseModel):
    """One equi-join column pair in `CanonicalJoinInfo`. Mirrors the
    persisted `JoinColumnPair` shape but as Pydantic (not dataclass)
    so it serialises cleanly through the MCP envelope.
    """

    model_config = ConfigDict(frozen=True)

    source_column: str
    target_column: str


class CanonicalJoinInfo(BaseModel):
    """Return shape for `resolve_join`.

    `source_entity` / `target_entity` preserve the STORED direction —
    the `resolve_join` lookup is direction-insensitive, but the
    response orients per how the user originally confirmed the join
    so the `sql_skeleton` field renders predictably.

    `sql_skeleton` is a ready-to-paste `JOIN <target> AS <alias> ON
    ...` clause. Composite-key joins render with `AND`-joined column
    predicates. The agent doesn't need a second round-trip to
    `describe_entity` to learn the physical table — the skeleton
    embeds it.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    source_entity: str
    target_entity: str
    # Mirror of the persisted `CanonicalJoin.on` non-empty invariant
    # at the Pydantic envelope layer. Without `min_length=1`, a
    # `CanonicalJoinInfo` constructed from a raw dict (test, future
    # deserialiser) could carry an empty pair list while the persisted
    # row never can.
    on: list[JoinColumnPairInfo] = Field(min_length=1)
    sql_skeleton: str
    token_estimate: int


# ----- metric shapes --------------------------------------------------------
#
# The boundary between the agent (caller of `get_metric`) and the
# Schema Brain compiler. The Pydantic shapes here are the wire format
# FastMCP exposes; the compiler's `RequestedFilter` dataclass is the
# internal IR — there's a one-to-one mapping between them.


class MetricFilterArg(BaseModel):
    """One filter predicate the agent wants applied to `get_metric`.

    `column` is `<entity>.<column>` form. `op` is the closed set the
    compiler supports. `value` is null for the unary operators
    `is_null` / `not_null`, a list for `in` / `not_in`, and a scalar
    otherwise. The compiler validates op/value coherence in `resolve`.
    """

    model_config = ConfigDict(frozen=True)

    column: str
    op: Literal[
        "eq",
        "ne",
        "lt",
        "lte",
        "gt",
        "gte",
        "in",
        "not_in",
        "is_null",
        "not_null",
    ]
    value: Any = None


class MetricResult(BaseModel):
    """Return shape for `get_metric` (success path).

    `rows` is the materialised result set — one dict per row, keyed by
    column alias. `sql_skeleton` is the parameterised SQL emitted by
    the compiler (with `:p_*` placeholders); `sql_params` carries the
    bound values. An agent can audit or compose against either.

    `fingerprint` is the lowercase-hex sha256 digest of the
    `mcp_audit` row written for this call, when the server is wired
    with an `AuditWriter`. Without an audit writer (test contexts, the
    `--no-audit` CLI path), it carries `"fp-unset"` so consumers can
    distinguish "no audit row exists" from "audit row exists with hex".

    `fan_out_join_names` surfaces the canonical-join names whose
    cardinality means the result rows may be inflated by JOIN expansion
    (one_to_many or many_to_many from the metric anchor). Empty when
    no fan-out joins were traversed.
    """

    model_config = ConfigDict(frozen=True)

    rows: list[dict[str, Any]]
    row_count: int
    sql_skeleton: str
    sql_params: dict[str, Any]
    fingerprint: str
    token_estimate: int
    required_joins: list[str]
    fan_out_join_names: list[str]
