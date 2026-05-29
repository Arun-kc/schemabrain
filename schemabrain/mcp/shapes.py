"""Pydantic return shapes and exceptions for the MCP tools.

All public response types and the two error classes live here so the
per-tool modules stay focused on logic only.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from schemabrain.core.entity import InferenceMethod, Origin, ValidationState
from schemabrain.core.example_query import ExampleQuerySource
from schemabrain.core.join import JoinOrigin
from schemabrain.core.metric import AggFunction, MetricOrigin, TimeGrain
from schemabrain.pii.categories import PIICategory, Sensitivity


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

    when ``redacted=True``, the ``name`` field carries a
    placeholder (``<redacted_<category>_column_N>``) — NOT the real
    column name. The real name remains in the store and on the
    operator-facing dashboard; only the agent-facing MCP response
    masks it. This closes the firewall-bypass loophole where an
    agent could read a catastrophic-leak column name from
    ``describe_table`` and emit raw SQL referencing it.
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
    redacted: bool = Field(
        default=False,
        description=(
            "True when the real column name is hidden behind a "
            "``<redacted_<category>_column_N>`` placeholder because the "
            "column's PII categories intersect the firewall's effective "
            "block set (operator policy union catastrophic-leak floor). "
            "Agents MUST NOT emit SQL referencing a redacted column."
        ),
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

    ``redacted_columns`` lists the REAL names of columns whose
    PII categories intersect the effective firewall block set. The
    columns appear in the ``columns`` list under
    ``<redacted_<category>_column_N>`` placeholders; the real names live
    here as an audit-trail surface for operators (and for the dashboard
    when it cross-references against the store). Agents should treat
    this as "these column kinds exist but you can't reference them" —
    never as a list to feed into SQL.
    """

    model_config = ConfigDict(frozen=True)

    qualified_name: str
    schema_name: str
    name: str
    columns: list[ColumnInfo]
    foreign_keys: list[ForeignKeyInfo]
    token_estimate: int
    redacted_columns: tuple[str, ...] = Field(
        default=(),
        description=(
            "Real names of columns hidden behind placeholders in "
            "``columns``. Operator-side audit signal; agents should not "
            "echo these names in SQL."
        ),
    )


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

    @model_validator(mode="after")
    def _validate_score(self) -> TableHit:
        # Cosine similarity is mathematically in [-1, 1] but `Store.
        # search_embeddings_topk` filters zero-norm vectors and the
        # per-tool impl drops zero-score rows, so anything that reaches
        # the wire shape must lie in [0, 1]. Pinning the range as a
        # Pydantic invariant means a future ranker that emits a
        # negative or >1 score is caught at envelope construction time
        # rather than silently flowing to the agent.
        if not (0.0 <= self.score <= 1.0):
            raise ValueError(f"score must be in [0, 1], got {self.score}")
        return self


class EntityHit(BaseModel):
    """One ranked hit returned by `find_relevant_entities`.

    Mirrors `TableHit` but anchored on a semantic entity rather than a
    physical table. `score` is the MAX cosine similarity across columns
    of the entity's bound table — same per-table MAX aggregation as
    `find_relevant_tables`, restricted to tables that have a confirmed
    entity binding. `best_column` / `best_column_description` surface
    WHY the entity was matched so the agent can decide whether to dig
    deeper via `describe_entity`.

    `qualified_table` is the bound physical table (e.g. `public.users`)
    so an agent that wants the physical view in addition to the entity
    surface doesn't need a second round-trip through `describe_entity`.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    score: float
    qualified_table: str
    best_column: str
    best_column_description: str
    token_estimate: int

    @model_validator(mode="after")
    def _validate_score(self) -> EntityHit:
        # Cosine similarity invariant — same rationale as `TableHit`.
        # A future ranker that emits a malformed score gets caught at
        # envelope construction, not after the agent consumes it.
        if not (0.0 <= self.score <= 1.0):
            raise ValueError(f"score must be in [0, 1], got {self.score}")
        return self


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

    v14 / charter v1.2: `inference_method` + `validation_state` are
    the 2D trust signal that backs the envelope-level `confidence`.
    Per-row signal lets the agent pick out which entity in a list is
    weak-confidence rather than seeing an aggregate-only label.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    qualified_table: str
    identity: str
    origin: Origin
    inference_method: InferenceMethod = "manually_authored"
    validation_state: ValidationState = "applied"


class EntityColumn(BaseModel):
    """One column on an entity's bound table.

    Mirrors the per-column shape from `describe_table` plus the
    column-granular PII signal:

      - `pii_sensitivity` carries the 4-value closed Literal classified
        at index time by the heuristic PII classifier (`public`,
        `internal`, `confidential`, `pii`).
      - `pii_categories` lists the specific PII category labels
        (`contact`, `financial`, `health`, etc.) — sorted tuple for
        deterministic serialisation.
      - `redacted` is True when this column's categories intersect the
        server's `--pii-block` policy. When True, `description` is
        cleared so an agent cannot read the LLM-enriched semantic
        description for a column it is policy-forbidden to access.

    Charter v1.2 column-granular firewall: refusal moves from "whole
    entity blocked" to "specific columns redacted". An agent asking
    about the `customers` entity with `--pii-block contact` set sees
    the entity and the non-PII columns; the `email`/`phone` columns
    ship with `redacted=True` and an empty description.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    data_type: str
    nullable: bool
    description: str = Field(
        default="",
        description="LLM-generated semantic description, or empty string "
        "if the column was indexed without enrichment OR if the column "
        "was redacted under the server's `--pii-block` policy.",
    )
    pii_sensitivity: Sensitivity = Field(
        default="public",
        description="PII classification carried through to the agent. "
        "Populated from the heuristic PII classifier's `column_pii_tags` "
        "row; defaults to `public` when no classification was stored.",
    )
    pii_categories: tuple[str, ...] = Field(
        default=(),
        description="Sorted tuple of PII category labels this column "
        "carries. Empty when the column has no PII classification.",
    )
    redacted: bool = Field(
        default=False,
        description="True when at least one of this column's "
        "`pii_categories` intersects the server's `--pii-block` set. "
        "When True, `description` is cleared so an agent cannot read "
        "the LLM-enriched semantic description for a policy-blocked "
        "column.",
    )


class EntityDetail(BaseModel):
    """Return shape for `describe_entity`.

    Includes every column of the bound table — at this release the
    YAML grammar doesn't yet let an entity allowlist a subset, so
    `columns` is the full underlying table. The agent gets one-look
    access to "what does this entity expose?" without a second
    round-trip to `describe_table`.

    v14 / charter v1.2: `inference_method` + `validation_state`
    mirror `EntitySummary` so a single-entity drill carries the
    same 2D trust signal at the data layer as well as the
    envelope-level `confidence`.

    Charter v1.2 column-granular firewall: `redacted_columns` is the
    sorted tuple of column names whose `pii_categories` intersect the
    server's `--pii-block` set. Each such column's
    `EntityColumn.redacted` field is also True and its description
    is cleared. An agent reading `redacted_columns` knows which
    columns are policy-blocked at a glance without scanning every
    column entry.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    qualified_table: str
    identity: str
    origin: Origin
    columns: list[EntityColumn]
    token_estimate: int
    inference_method: InferenceMethod = "manually_authored"
    validation_state: ValidationState = "applied"
    redacted_columns: tuple[str, ...] = ()


# ----- canonical-join shapes ------------------------------------------------


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


class JoinSummary(BaseModel):
    """One canonical join in the `list_joins` response.

    Lean by design: name + description + the pair of entities it
    connects + provenance. The agent uses this to enumerate what
    joins are available; for the SQL skeleton it calls
    `resolve_join(entity_a, entity_b)` (or passes `name=` to
    disambiguate when 2+ canonical joins exist between the same
    entity pair).

    Direction is preserved from the stored row — see
    `CanonicalJoinInfo` for the same orientation convention.

    v14 / charter v1.2: `inference_method` carries the load-bearing
    `fk_constraint` vs `llm_suggested` vs `manually_authored`
    distinction. An agent listing joins gets a clear signal of
    which joins are DB-validated vs LLM-guessed.

    Junction bridges: when the join is a synthesised bridge through a
    junction (M:N pivot table), `via_junction` names the middle entity
    and `via_joins` carries the two underlying canonical-join names the
    agent can `resolve_join` against for the actual SQL skeleton. Both
    fields default to `None`/empty for direct canonical joins; an agent
    that ignores them still gets the entity pair and provenance.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    source_entity: str
    target_entity: str
    origin: JoinOrigin
    inference_method: InferenceMethod = "manually_authored"
    validation_state: ValidationState = "applied"
    via_junction: str | None = None
    via_joins: tuple[str, ...] = ()


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
    # v14 / charter v1.2: the load-bearing FK-vs-LLM-guess
    # distinction. An agent resolving a join sees whether it can
    # trust the on-clause blindly (`fk_constraint`) or should
    # treat it as an unverified LLM guess (`llm_suggested`).
    inference_method: InferenceMethod = "manually_authored"
    validation_state: ValidationState = "applied"


# ----- metric shapes --------------------------------------------------------
#
# The boundary between the agent (caller of `get_metric`) and the
# SchemaBrain compiler. The Pydantic shapes here are the wire format
# FastMCP exposes; the compiler's `RequestedFilter` dataclass is the
# internal IR — there's a one-to-one mapping between them.


class MetricSummary(BaseModel):
    """One metric in the `list_metrics` response.

    Lean by design: name + anchor entity + aggregation shape + measure
    shape + time bucketing capabilities + provenance. The agent uses
    this to pick a metric to feed to `get_metric`; for the actual
    computation it calls `get_metric(name, ...)`.

    Measure shape is discriminated: exactly one of `measure_column` and
    `measure_expression` is populated, never both, never neither — the
    same XOR the `MetricMeasure` dataclass enforces. Bare-column
    measures (v1 shape, `SUM(amount)`) carry `measure_column` and
    `measure_expression is None`. Composite expressions (v2 shape,
    `SUM(unit_price * quantity)`) carry `measure_expression` (the raw
    expression source) and `measure_column is None`. Reading agents
    can branch on which field is populated to decide whether the
    metric is a simple-column aggregate or a composite computation.

    `time_dimension` and `time_grains` are paired — both set or both
    empty. When `time_dimension` is set, the agent may pass any value
    from `time_grains` as the `time_grain` argument to `get_metric`.
    When unset, the metric is non-temporal and `time_grain` is not
    accepted.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    entity: str
    aggregation: AggFunction
    measure_column: str | None = None
    measure_expression: str | None = None
    time_dimension: str | None
    time_grains: tuple[TimeGrain, ...]
    origin: MetricOrigin
    # v14 / charter v1.2: 2D trust signal at the per-metric level so
    # `list_metrics` makes the LLM-vs-hand-confirmed distinction
    # visible — an LLM-suggested metric is `(llm_suggested, applied)`
    # (MEDIUM), a hand-confirmed one is `(manually_authored,
    # confirmed)` (HIGH).
    inference_method: InferenceMethod = "manually_authored"
    validation_state: ValidationState = "applied"


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


class MetricOrderByArg(BaseModel):
    """One ORDER BY clause the agent wants applied to `get_metric`.

    `column` MUST be either the metric's `name` (the SELECT-aliased
    measure) or one of the `group_by` columns in `<entity>.<column>`
    form. Anything else refuses with `unknown_order_by_column`. This
    constraint exists so callers can't smuggle an arbitrary column
    into ORDER BY without putting it in `group_by` first.

    `direction` is `asc` or `desc`; defaults to `asc` to match SQL
    default. The compiler always emits the direction explicitly so
    the SQL is self-documenting.

    The compiler auto-appends a deterministic tie-breaking secondary
    key (the first `group_by` column, ASC) when ORDER BY is set AND
    `group_by` is non-empty, so identical measure values produce
    identical row order across runs.
    """

    model_config = ConfigDict(frozen=True)

    column: str
    direction: Literal["asc", "desc"] = "asc"


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

    Privacy-by-construction (per ADR 0001): the fingerprint hashes a
    deliberately restricted set — SQL AST shape, PII categories
    touched, refusal reason, cost class, rule id — and explicitly
    excludes row content and identifying schema info. Two distinct
    calls with similar AST shape + identical PII tags + identical
    cost class therefore produce IDENTICAL fingerprints by design.
    This is the intended aggregation primitive ("did anything that
    looks like an unbounded scan run today?") and not a hash
    collision bug; observers seeing repeated fingerprints across
    different metric names are seeing the privacy guarantee at work.

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
    # Sorted tuple of PIICategory values produced by propagating the
    # tags of every column the metric touched. Empty tuple when no
    # tagged column was touched (or no classifier was ever run). The
    # audit row reads this to populate `mcp_audit.pii_categories`
    # and to derive `FingerprintInput.pii_tags_touched`.
    pii_categories: tuple[PIICategory, ...] = ()
    # v14 / charter v1.2: the 2D trust signal aggregated from the
    # metric itself + every canonical join the compiler traversed.
    # `metric_inference_method` reports the metric's own derivation.
    # `aggregate_inference_method` is the worst-case over the metric
    # AND its traversed joins — an FK-validated metric anchored over
    # an LLM-guessed join inherits the join's weaker signal because
    # the SQL the agent sees depends on the unreliable on-clause.
    metric_inference_method: InferenceMethod = "manually_authored"
    metric_validation_state: ValidationState = "applied"
    aggregate_inference_method: InferenceMethod = "manually_authored"
    aggregate_validation_state: ValidationState = "applied"
    # Charter v1.2: time-dimension inheritance outcome. `local` means
    # the metric author declared `time_dimension`; `inherited` means the
    # resolver picked a reachable timestamp column via canonical-join
    # chains; `unavailable` means the caller asked for a grain but no
    # candidate was reachable, so the plan ran unbucketed. When
    # `inherited`, `inherited_time_dimension` carries `<entity>.<column>`
    # and `time_dimension_inherited_via` lists the join chain traversed.
    time_dimension_resolution: Literal["local", "inherited", "unavailable"] = "local"
    inherited_time_dimension: str | None = None
    time_dimension_inherited_via: tuple[str, ...] = ()
