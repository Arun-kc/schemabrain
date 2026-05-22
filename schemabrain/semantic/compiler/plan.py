"""Compiler IR + error hierarchy.

The closed-grammar dataclasses the resolver produces and the emitter
consumes. Every type here is frozen so the plan is hashable (the audit
layer fingerprints `MetricPlan` instances in a future PR) and immutable
across the emit boundary.

The compiler error hierarchy is structured so callers can either:
  - catch `MetricCompilerError` to handle "the metric couldn't be
    compiled" generically, or
  - catch a specific subclass to branch on the failure mode (the MCP
    tool surface uses this to map each subclass to a charter envelope
    `kind`).

Errors carry structured fields (candidate names, allowed values)
beyond the exception message so the MCP layer can populate `recovery`
hints without re-parsing the message.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from schemabrain.core.join import Cardinality, JoinColumnPair
from schemabrain.core.metric import Metric, TimeGrain
from schemabrain.pii import PIICategory

# Closed set of operators the compiler supports at v1. `is_null` and
# `not_null` are unary (value is ignored at emit time); `in` / `not_in`
# expect list-shaped values; the rest are scalar. Open-ended operators
# (regex, like) defer to v2's expression layer.
Operator = Literal[
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

# Closed direction grammar for ORDER BY. Lowercase on the wire (closed
# Literal, agent-shaped); uppercased at SQL emission. Default "asc"
# matches Postgres default but is emitted explicitly so the SQL is
# self-documenting and survives a future engine swap.
OrderDirection = Literal["asc", "desc"]


# ----- compiler errors -------------------------------------------------------


class MetricCompilerError(ValueError):
    """Base for all compiler-side metric errors.

    Subclass of `ValueError` so callers that just want "compilation
    failed" can catch broadly. The MCP tool layer catches each
    subclass and maps it to a charter envelope `kind`.
    """


class UnknownMetricError(MetricCompilerError):
    """Raised when the requested metric name isn't in the store."""


class MalformedColumnError(MetricCompilerError):
    """Raised when a group_by or filter column reference is not in
    `entity.column` form (one dot, identifier parts).

    Distinct from `UnknownColumnError` because the message tells the
    user the SHAPE is wrong, not that the column doesn't exist.
    """


class UnknownColumnError(MetricCompilerError):
    """Raised when an `entity.column` reference parses but the entity
    doesn't exist in the store.

    Distinct from `UnreachableEntityError`: the entity is genuinely
    missing, not just unreachable from the metric's anchor.
    """


@dataclass(frozen=True)
class UnknownGroupByColumnError(MetricCompilerError):
    """Raised when `group_by=['<entity>.<column>']` references an entity
    that exists but a column that does NOT exist on that entity's table.

    Without this check the compiler used to emit `"<entity>"."<column>"`
    verbatim and let Postgres surface `UndefinedColumn` at execution
    time, which the MCP layer wrapped as `internal_error` — a useless
    response for an agent that just made a typo.

    Carries the requested entity + column + the actual column list on
    that entity so the MCP layer can populate `recovery` with the
    allowed names. The 2026-05-21 stress test caught the silent-failure
    path (`user.bogus_column` → `internal_error`); this error class
    closes it.
    """

    entity: str
    column: str
    allowed_columns: tuple[str, ...]

    def __post_init__(self) -> None:
        super().__init__(
            f"group_by column {self.entity!r}.{self.column!r} does not "
            f"exist on entity {self.entity!r}. Allowed columns: "
            f"{list(self.allowed_columns)}"
        )

    def __reduce__(self) -> tuple[type, tuple[str, str, tuple[str, ...]]]:
        return (self.__class__, (self.entity, self.column, self.allowed_columns))


@dataclass(frozen=True)
class UnknownFilterColumnError(MetricCompilerError):
    """Raised when a `filters` entry references an entity that exists
    but a column that does NOT exist on that entity's table.

    Parallel to `UnknownGroupByColumnError` — same compile-time check
    moved earlier in the pipeline so the agent gets a clean envelope
    instead of an `internal_error` masking a Postgres UndefinedColumn.
    """

    entity: str
    column: str
    allowed_columns: tuple[str, ...]

    def __post_init__(self) -> None:
        super().__init__(
            f"filter column {self.entity!r}.{self.column!r} does not "
            f"exist on entity {self.entity!r}. Allowed columns: "
            f"{list(self.allowed_columns)}"
        )

    def __reduce__(self) -> tuple[type, tuple[str, str, tuple[str, ...]]]:
        return (self.__class__, (self.entity, self.column, self.allowed_columns))


@dataclass(frozen=True)
class UnknownMeasureColumnError(MetricCompilerError):
    """Raised when a metric's measure references an entity that exists
    but a column that does NOT exist on that entity's table.

    Parallel to `UnknownGroupByColumnError` / `UnknownFilterColumnError`.
    Covers BOTH the v1 bare-column path (single `measure.column`) AND
    the v2 composite-expression path (every column referenced inside
    `measure.expression`) — composite expressions reference multiple
    operands, any of which can be a typo. Without this check, a typo
    surfaces only at Postgres execution time as `UndefinedColumn`,
    which the MCP layer wraps as `internal_error`.

    Carries the entity + the offending column + the actual allowed set
    so the MCP layer can populate `recovery.suggested_args.allowed_columns`
    for a mechanical retry.
    """

    entity: str
    column: str
    allowed_columns: tuple[str, ...]

    def __post_init__(self) -> None:
        super().__init__(
            f"measure column {self.entity!r}.{self.column!r} does not "
            f"exist on entity {self.entity!r}. Allowed columns: "
            f"{list(self.allowed_columns)}"
        )

    def __reduce__(self) -> tuple[type, tuple[str, str, tuple[str, ...]]]:
        return (self.__class__, (self.entity, self.column, self.allowed_columns))


@dataclass(frozen=True)
class UnreachableEntityError(MetricCompilerError):
    """Raised when a group_by or filter column targets an entity that
    has no canonical join from the metric's anchor.

    Carries both names so the MCP layer can populate `recovery` with
    a `resolve_join` suggestion or a `joins suggest` pointer.
    """

    anchor_entity: str
    target_entity: str

    def __post_init__(self) -> None:
        # dataclass __init__ overrides Exception.__init__; we set
        # `args` so `str(exc)` and `repr` carry useful information.
        super().__init__(
            f"entity {self.target_entity!r} is not reachable from "
            f"metric anchor {self.anchor_entity!r}; no canonical join exists. "
            f"Run `schemabrain joins suggest` to surface candidate joins."
        )

    def __reduce__(self) -> tuple[type, tuple[str, str]]:
        # The default `Exception.__reduce__` pickles via `args` only,
        # which would drop the structured fields on the round-trip.
        # Explicit reducer preserves the dataclass shape so the a future PR
        # audit layer (which may serialise refusal events for log
        # shipping) round-trips correctly.
        return (self.__class__, (self.anchor_entity, self.target_entity))


@dataclass(frozen=True)
class AmbiguousJoinError(MetricCompilerError):
    """Raised when the metric anchor and a group_by/filter entity have
    2+ canonical joins between them (e.g. billing vs shipping address).

    Carries the candidate join names so the MCP layer can include them
    in the `recovery` envelope. The agent can pass `via=(join_name,)`
    through `get_metric` to disambiguate (one element per ambiguous
    hop in the chain — for single-hop ambiguity it's a single name).
    """

    anchor_entity: str
    target_entity: str
    candidate_join_names: tuple[str, ...]

    def __post_init__(self) -> None:
        super().__init__(
            f"multiple canonical joins exist between {self.anchor_entity!r} "
            f"and {self.target_entity!r}: {list(self.candidate_join_names)}. "
            f"Pass `via=({self.candidate_join_names[0]!r},)` (or another name "
            f"from this list) to disambiguate."
        )

    def __reduce__(self) -> tuple[type, tuple[str, str, tuple[str, ...]]]:
        return (
            self.__class__,
            (self.anchor_entity, self.target_entity, self.candidate_join_names),
        )


@dataclass(frozen=True)
class AmbiguousPathError(MetricCompilerError):
    """Raised when 2+ shortest canonical-join paths exist from the
    metric's anchor to a group_by/filter target entity.

    Distinct from `AmbiguousJoinError`: ambiguity is at the PATH level
    (e.g. `order_item → order → billing_address → user`
    vs. `order_item → order → shipping_address → user`), not at a
    single edge between an entity pair.

    `candidate_paths` is a tuple of tuples — each inner tuple is the
    ordered canonical-join names for one candidate path. The agent
    disambiguates by passing `via=(join_name, ...)` on `get_metric`;
    each element constrains a hop the chain MUST traverse.
    """

    anchor_entity: str
    target_entity: str
    candidate_paths: tuple[tuple[str, ...], ...]

    def __post_init__(self) -> None:
        # Render up to the first 4 paths in the message to bound echo
        # under a prompt-injection scenario where the join graph is
        # synthesised to balloon the candidate set.
        shown = list(self.candidate_paths[:4])
        more = f" (+{len(self.candidate_paths) - 4} more)" if len(self.candidate_paths) > 4 else ""
        # Render the suggestion as an inline tuple literal so the
        # agent's copy-paste retry stays valid against the
        # `via: tuple[str, ...]` contract — anything that nested a list
        # inside a tuple (`via=(['n1','n2'],)`) would TypeError when
        # the resolver iterates `via`.
        first_path_literal = tuple(shown[0]) if shown else ()
        super().__init__(
            f"multiple canonical-join paths exist from {self.anchor_entity!r} "
            f"to {self.target_entity!r}: {shown}{more}. "
            f"Pass `via={first_path_literal!r}` (or another path's "
            f"names) on `get_metric` to disambiguate."
        )

    def __reduce__(
        self,
    ) -> tuple[type, tuple[str, str, tuple[tuple[str, ...], ...]]]:
        return (
            self.__class__,
            (self.anchor_entity, self.target_entity, self.candidate_paths),
        )


@dataclass(frozen=True)
class UnknownViaJoinError(MetricCompilerError):
    """Raised when `via=` references a canonical-join name that isn't
    defined for this source, or is defined but doesn't appear on any
    candidate path from anchor to target.

    Distinct from `AmbiguousPathError` (multiple valid paths) and
    `UnreachableEntityError` (no path exists at all): the path graph
    is well-formed but the via constraint can't be satisfied.
    """

    anchor_entity: str
    target_entity: str
    requested_via: tuple[str, ...]
    available_join_names: tuple[str, ...]

    def __post_init__(self) -> None:
        super().__init__(
            f"`via={list(self.requested_via)}` does not match any "
            f"canonical-join path from {self.anchor_entity!r} to "
            f"{self.target_entity!r}. Available joins on candidate "
            f"paths: {list(self.available_join_names)}"
        )

    def __reduce__(
        self,
    ) -> tuple[type, tuple[str, str, tuple[str, ...], tuple[str, ...]]]:
        return (
            self.__class__,
            (
                self.anchor_entity,
                self.target_entity,
                self.requested_via,
                self.available_join_names,
            ),
        )


@dataclass(frozen=True)
class InvalidTimeGrainError(MetricCompilerError):
    """Raised when a caller requests a time_grain the metric doesn't
    declare in its `time_grains`, or when a non-temporal metric is
    called with a time_grain.
    """

    requested_grain: TimeGrain
    allowed_grains: tuple[TimeGrain, ...]

    def __post_init__(self) -> None:
        if self.allowed_grains:
            super().__init__(
                f"time_grain {self.requested_grain!r} is not declared on this "
                f"metric; allowed grains: {list(self.allowed_grains)}"
            )
        else:
            super().__init__(
                f"time_grain {self.requested_grain!r} was requested but this "
                f"metric is non-temporal (no time_dimension declared)"
            )

    def __reduce__(self) -> tuple[type, tuple[TimeGrain, tuple[TimeGrain, ...]]]:
        return (self.__class__, (self.requested_grain, self.allowed_grains))


@dataclass(frozen=True)
class UnknownOrderByColumnError(MetricCompilerError):
    """Raised when an `order_by` entry references a column that is
    neither the metric's name (the SELECT-aliased measure) nor any of
    the `group_by` columns.

    Restricting ORDER BY to SELECTed columns is an explicit safety
    contract: it prevents smuggling an arbitrary entity column into
    ORDER BY without putting it in `group_by` first (which would
    change row counts and silently confuse the agent). Carries both
    the requested reference and the allowed set so the MCP layer can
    populate `recovery` with the allowed names.
    """

    requested_column: str
    allowed_columns: tuple[str, ...]

    def __post_init__(self) -> None:
        super().__init__(
            f"order_by column {self.requested_column!r} is not a "
            f"selected column; ORDER BY may only reference the metric's "
            f"name or one of the group_by columns. Allowed: "
            f"{list(self.allowed_columns)}"
        )

    def __reduce__(self) -> tuple[type, tuple[str, tuple[str, ...]]]:
        return (self.__class__, (self.requested_column, self.allowed_columns))


class GrainMismatchError(MetricCompilerError):
    """Reserved for v2 grain-aware metric work.

    Claimed in the error-kind Literal but NOT raised by the v1
    compiler — fan-out is surfaced as `status="degraded"` instead.
    The class exists so the future audit layer can attach a fingerprint
    to grain-mismatch refusals without a follow-up taxonomy change.
    """


class PiiBlockedError(MetricCompilerError):
    """Raised when a metric touches columns whose PII categories
    intersect the server's `--pii-block` policy set.

    `attempted_categories` carries the full set of categories the
    metric would touch (the propagation result of all column tags).
    `blocked_categories` is the subset that triggered the refusal
    (the intersection with the server's blocked set). Both are
    sorted tuples of `PIICategory` Literal values for deterministic
    wire serialisation; the audit row stores `attempted_categories`
    so the audit trail shows *what was touched*, not just *that
    something was blocked*.

    Both kwargs are required — defaulting to empty tuples would let
    a future caller silently produce a malformed audit row (refused
    status with empty pii_categories).
    """

    def __init__(
        self,
        message: str,
        *,
        attempted_categories: tuple[PIICategory, ...],
        blocked_categories: tuple[PIICategory, ...],
    ) -> None:
        super().__init__(message)
        self.attempted_categories = attempted_categories
        self.blocked_categories = blocked_categories


# ----- IR --------------------------------------------------------------------


@dataclass(frozen=True)
class RequestedFilter:
    """One predicate the caller wants applied.

    `column` is a `<entity>.<column>` reference (compiler validates the
    shape during resolve). `value` is `None` for the unary operators
    `is_null` and `not_null`. List shape (`list[Any]`) is required for
    `in` and `not_in`; the resolver enforces this.
    """

    column: str
    op: Operator
    value: Any = None


@dataclass(frozen=True)
class RequestedOrderBy:
    """One ORDER BY clause the caller wants emitted.

    `column` is EITHER the metric's `name` (referencing the
    SELECT-aliased measure aggregate) OR a `<entity>.<column>` form
    that names one of the `group_by` columns. The resolver enforces
    "only select-list columns may appear in ORDER BY" so callers can't
    smuggle an arbitrary column into ORDER BY without putting it in
    `group_by` first (which would change row counts).

    `direction` is `asc` or `desc`; default `asc` matches SQL default
    but is emitted explicitly for self-documentation.
    """

    column: str
    direction: OrderDirection = "asc"


@dataclass(frozen=True)
class ResolvedOrderBy:
    """Resolved ORDER BY clause ready for SQL emission.

    `expression` is the bare SELECT-alias name the emitter quotes at
    emit time (e.g. `total_items_sold` for a measure ref, `group_col_0`
    for the first group-by column). Using SELECT aliases (not the raw
    column references) keeps the ORDER BY clause unambiguous and matches
    how the emitter already groups by alias.

    `direction` is the resolved (still lowercase) direction; the emitter
    uppercases at SQL-write time.

    `auto_appended_tie_break` flags clauses the resolver added
    automatically for deterministic ordering — the audit layer reads
    this to distinguish caller-asked ordering from compiler-added
    tie-breaks, and the MCP layer can surface the explicit clauses
    back to the caller without leaking the tie-break.
    """

    expression: str
    direction: OrderDirection
    auto_appended_tie_break: bool = False


@dataclass(frozen=True)
class ResolvedColumn:
    """A `<entity>.<column>` reference resolved against the store.

    `entity` is the Schema Brain entity name; `column` is the bare
    column on that entity's bound table. `qualified_table` is the
    `schema.table` form ready for FROM/JOIN emission. `alias` is the
    SQL alias the emitter uses (defaults to the entity name) so
    composite-key joins and multiple references to the same column
    stay unambiguous.

    `quoted_alias` is the SQL-safe form of `alias` — entity names
    like `order`, `user`, `select` are valid Schema Brain identifiers
    but reserved keywords in Postgres. Double-quoting makes them safe
    as alias names while preserving case. Column names from
    `entities` rows are validated by `_IDENT_RE` and also need
    quoting at emit time when they might collide (e.g. `column` is
    a Postgres reserved word).
    """

    entity: str
    column: str
    qualified_table: str
    alias: str

    @property
    def quoted_alias(self) -> str:
        return f'"{self.alias}"'

    @property
    def quoted_column(self) -> str:
        return f'"{self.column}"'

    @property
    def column_ref(self) -> str:
        """`"alias"."column"` — the form used inside SELECT / WHERE / GROUP BY.

        Both alias AND column are double-quoted so reserved-keyword
        names (`order.user`, `customer.from`, etc.) survive emission.
        """
        return f"{self.quoted_alias}.{self.quoted_column}"


@dataclass(frozen=True)
class ResolvedPredicate:
    """A resolved filter predicate ready for SQL emission.

    `param_names` carries the placeholder name(s) the emitter assigns;
    scalar operators get one, `in`/`not_in` get N (one per list item),
    `is_null`/`not_null` get zero. The list keeps emission a single
    pass over `filter_predicates`.
    """

    column: ResolvedColumn
    op: Operator
    param_names: tuple[str, ...]
    value: Any


@dataclass(frozen=True)
class ResolvedJoin:
    """One canonical join resolved against the request.

    Carries enough to emit a `JOIN ... ON ...` clause WITHOUT a
    second store round-trip at emit time. `source_alias` is the SQL
    alias on the LEFT side of the ON clause — the metric anchor for
    direct joins, or the previous hop's entity for multi-hop chains.
    `target_alias` is the alias on the RIGHT side (the joined table).
    `on_pairs` is the equi-join predicate set (length >= 1 by the
    `CanonicalJoin` invariant); for joins traversed in the reverse
    of their stored direction, `on_pairs` carries SWAPPED columns
    so emit can use them verbatim. `cardinality` is the persisted
    value (None means worst-case many_to_many treatment for fan-out
    detection).
    """

    canonical_name: str
    source_alias: str
    target_entity: str
    target_table: str
    target_alias: str
    on_pairs: tuple[JoinColumnPair, ...]
    cardinality: Cardinality | None


@dataclass(frozen=True)
class MetricPlan:
    """The fully-resolved metric plan the emitter consumes.

    Frozen + hashable so the future audit layer can fingerprint plans
    without re-resolving. Field order is canonical so two plans for
    the same caller arguments compare equal regardless of how the
    resolver computed them.
    """

    metric: Metric
    anchor_table: str
    anchor_alias: str
    group_by_columns: tuple[ResolvedColumn, ...]
    time_bucket: TimeGrain | None
    filter_predicates: tuple[ResolvedPredicate, ...]
    limit: int
    # Canonical joins traversed, in TOPOLOGICAL chain order — the
    # emitter relies on this ordering: each JOIN clause references
    # the previous hop's alias on the left side, so reordering would
    # produce SQL that references undefined aliases. The audit layer
    # reads this list for provenance (just the names, see
    # `required_join_names`).
    joins: tuple[ResolvedJoin, ...] = field(default=())

    # ORDER BY clauses, in emission order. Includes any compiler-added
    # tie-breaking clauses (see `ResolvedOrderBy.auto_appended_tie_break`).
    # Empty tuple = no ORDER BY (the emitter omits the clause entirely).
    # A field default keeps every existing test constructor working
    # without the new kwarg.
    order_by_clauses: tuple[ResolvedOrderBy, ...] = field(default=())

    @property
    def required_join_names(self) -> tuple[str, ...]:
        """Canonical-join names, for the result envelope's
        `required_joins` provenance field."""
        return tuple(j.canonical_name for j in self.joins)

    @property
    def fan_out_join_names(self) -> tuple[str, ...]:
        """Subset of `required_join_names` whose effective cardinality
        multiplies rows on the anchor side of the chain.

        Cardinality on `ResolvedJoin` is direction-aware (the resolver
        flips the stored value when BFS walked the canonical join in
        reverse), so this check reads it verbatim:

          - `one_to_many` — the source side (already in the chain,
            carrying anchor rows) has 1 row per N target rows; adding
            target multiplies anchor rows. Fan-out.
          - `many_to_many` — always multiplies. Fan-out.
          - `None` — unknown, defensive worst case. Fan-out.
          - `many_to_one` — N source rows per 1 target row; adding
            target preserves source-side row count. NOT fan-out.
          - `one_to_one` — symmetric; NOT fan-out.

        The MCP envelope surfaces these as the `fan_out_join`
        degradation reason so the agent can switch to `count_distinct`
        on an identity column when the metric requires it.
        """
        return tuple(
            j.canonical_name
            for j in self.joins
            if j.cardinality in ("one_to_many", "many_to_many") or j.cardinality is None
        )
