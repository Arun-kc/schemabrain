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
    in the `recovery` envelope. At v1 the agent has no way to pass a
    specific join through `get_metric` — the recovery hint is
    informational only (the user must narrow the join graph or define
    a more specific metric). v2 expression layer adds the disambiguator.
    """

    anchor_entity: str
    target_entity: str
    candidate_join_names: tuple[str, ...]

    def __post_init__(self) -> None:
        super().__init__(
            f"multiple canonical joins exist between {self.anchor_entity!r} "
            f"and {self.target_entity!r}: {list(self.candidate_join_names)}. "
            f"`get_metric` cannot disambiguate at v1; narrow the canonical-join "
            f"set or author a more specific metric."
        )

    def __reduce__(self) -> tuple[type, tuple[str, str, tuple[str, ...]]]:
        return (
            self.__class__,
            (self.anchor_entity, self.target_entity, self.candidate_join_names),
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
    second store round-trip at emit time. `target_alias` is the SQL
    alias to use for the joined table; `on_pairs` is the equi-join
    predicate set (length >= 1 by the `CanonicalJoin` invariant).
    `cardinality` is the persisted value (None means worst-case
    many_to_many treatment for fan-out detection).
    """

    canonical_name: str
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
    # Canonical joins traversed, in alphabetical order by name. The
    # emit layer iterates this list to produce JOIN clauses; the
    # audit layer reads it for provenance (just the names).
    joins: tuple[ResolvedJoin, ...] = field(default=())

    @property
    def required_join_names(self) -> tuple[str, ...]:
        """Canonical-join names, for the result envelope's
        `required_joins` provenance field."""
        return tuple(j.canonical_name for j in self.joins)

    @property
    def fan_out_join_names(self) -> tuple[str, ...]:
        """Subset of `required_join_names` with one_to_many /
        many_to_many cardinality (or unspecified, treated as
        worst-case). The MCP envelope surfaces these as fan-out
        warnings."""
        return tuple(
            j.canonical_name
            for j in self.joins
            if j.cardinality in ("one_to_many", "many_to_many") or j.cardinality is None
        )
