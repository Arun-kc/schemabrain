"""Metric compiler — `Metric` definition + caller args → parameterised SQL.

Three layers:

  - `plan.py` — closed-grammar dataclass IR (`MetricPlan`,
    `ResolvedColumn`, `ResolvedPredicate`, `RequestedFilter`,
    `Operator`) + the compiler error hierarchy.

  - `resolve.py` — turns a metric name + caller arguments into a
    `MetricPlan` by looking up entities, canonical joins, and the
    metric itself in the store. Raises structured compiler errors
    when the request can't be satisfied.

  - `emit.py` — turns a `MetricPlan` into a `(sql_text, params,
    fan_out_join_names)` triple. SQL is parameterised
    (SQLAlchemy `text()` style with `:p_*` placeholders) and
    single-statement (the emitter never produces a `;`).

The IR is v2-substrate per architectural seam 2; v2's
`validate_query` extends `resolve.py` with more rules, while `emit.py`
stays as the canonical safe-SQL emitter.
"""

from schemabrain.semantic.compiler.emit import emit_sql
from schemabrain.semantic.compiler.plan import (
    AmbiguousJoinError,
    AmbiguousPathError,
    AmbiguousTimeDimensionError,
    GrainMismatchError,
    InvalidTimeGrainError,
    MalformedColumnError,
    MetricCompilerError,
    MetricPlan,
    Operator,
    OrderDirection,
    PiiBlockedError,
    RequestedFilter,
    RequestedOrderBy,
    ResolvedColumn,
    ResolvedJoin,
    ResolvedOrderBy,
    ResolvedPredicate,
    UnknownColumnError,
    UnknownFilterColumnError,
    UnknownGroupByColumnError,
    UnknownMeasureColumnError,
    UnknownMetricError,
    UnknownOrderByColumnError,
    UnknownViaJoinError,
    UnreachableEntityError,
)
from schemabrain.semantic.compiler.resolve import resolve_metric_plan

__all__ = [
    "AmbiguousJoinError",
    "AmbiguousPathError",
    "AmbiguousTimeDimensionError",
    "GrainMismatchError",
    "InvalidTimeGrainError",
    "MalformedColumnError",
    "MetricCompilerError",
    "MetricPlan",
    "Operator",
    "OrderDirection",
    "PiiBlockedError",
    "RequestedFilter",
    "RequestedOrderBy",
    "ResolvedColumn",
    "ResolvedJoin",
    "ResolvedOrderBy",
    "ResolvedPredicate",
    "UnknownColumnError",
    "UnknownFilterColumnError",
    "UnknownGroupByColumnError",
    "UnknownMeasureColumnError",
    "UnknownMetricError",
    "UnknownOrderByColumnError",
    "UnknownViaJoinError",
    "UnreachableEntityError",
    "emit_sql",
    "resolve_metric_plan",
]
