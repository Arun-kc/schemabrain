"""Metric — the value side of the semantic layer.

A Metric is a named, validator-backed business measure anchored on one
Entity. It carries a single aggregation over a column on that entity's
table, an optional time dimension (with the time grains it can be
bucketed at), and an origin tag.

Design decisions baked into this module:

  - **One metric = one measure.** The modern dbt Semantic Layer (1.6+)
    split `semantic_models` (which can carry many measures) from
    `metrics` (one measure each). Multi-measure dbt-semantic-models
    import as N metrics — clean mapping. This shape rules out
    derived/ratio/cumulative metrics at v1; those are deferred to v2's
    expression layer.

  - **Closed-grammar aggregation.** `AggFunction = Literal["sum",
    "count", "count_distinct", "avg", "min", "max"]`. Open `agg: str`
    would invite SQL injection at the compiler emit boundary; closed
    literal makes injection impossible by construction. `median`
    (PERCENTILE_CONT) is deferred for portability reasons — it ships
    cleanly on Postgres but not on every engine v2 will support.

  - **Closed-grammar time grain.** `TimeGrain = Literal["day", "week",
    "month", "quarter", "year"]`. Sub-day grains (`hour`, `minute`)
    aren't a v1 use case — when a metric needs them, the time-bucket
    surface gets extended in a future minor release.

  - **`time_dimension` and `time_grains` are paired.** Set both or
    neither. A time dimension without grains is meaningless (the
    compiler wouldn't know how to bucket); grains without a dimension
    is the same incoherence in reverse. The dataclass enforces
    parallel emptiness in `__post_init__`.

  - **`time_grains` is canonically sorted.** Without canonical
    ordering, two YAMLs that list the same grains in different orders
    would round-trip as inequal `Metric` instances. Sort enforced at
    construction; store layer trusts the invariant on round-trip.

  - **`time_dimension` is `<entity>.<column>` form.** Decouples the
    metric anchor (the grain `entity` lives at) from the time bucket
    source. `total_revenue` anchored on `order` with time dimension
    `order.created_at` is the common shape. At v1 we require the time
    dimension on the anchor entity — cross-entity time dimensions
    defer to v2 because the cross-entity grain-check is harder.

  - **`origin` is the same closed Literal as `Entity.origin` and
    `CanonicalJoin.origin`.** All three values (`manual` /
    `suggested` / `dbt_import`) are valid at the dataclass from day
    one. The dbt-metrics importer ships in the same PR (commit 6),
    so unlike `CanonicalJoin.origin` at PR #31, `dbt_import` has a
    real producer immediately. Hand-authored YAML with
    `origin: dbt_import` is still refused at the YAML parse layer to
    keep the dbt-guard pattern coherent.

`DbtOwnedMetricError` lives in this module (not in `core/store.py`)
because it is part of the `Store` Protocol's behavioural contract —
mirrors `DbtOwnedEntityError` from `core/entity.py`. A future hosted
backend implementor can raise it without importing from the concrete
SQLite implementation.

Validation runs in `__post_init__` so the same invariants apply
regardless of construction path (YAML loading, store reads, dbt-import
output, programmatic calls).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, get_args

# Closed sets of provenance values + agg + grain. All three stay in
# lock-step with the SQL CHECK constraints in
# `core/store.py::_DDL_STATEMENTS` — adding a value here without
# bumping the schema version (and updating the CHECK) will silently
# break round-trips.
MetricOrigin = Literal["manual", "suggested", "dbt_import"]
AggFunction = Literal["sum", "count", "count_distinct", "avg", "min", "max"]
TimeGrain = Literal["day", "week", "month", "quarter", "year"]

_VALID_ORIGINS: frozenset[str] = frozenset(get_args(MetricOrigin))
_VALID_AGGS: frozenset[str] = frozenset(get_args(AggFunction))
_VALID_GRAINS: frozenset[str] = frozenset(get_args(TimeGrain))

# Canonical ordering for `time_grains`. Without a fixed order, two
# YAMLs with the same set of grains in different listing orders would
# round-trip as inequal `Metric` instances and break fingerprint
# stability when the audit layer lands later.
_TIME_GRAIN_ORDER: tuple[TimeGrain, ...] = ("day", "week", "month", "quarter", "year")
_TIME_GRAIN_RANK: dict[str, int] = {grain: i for i, grain in enumerate(_TIME_GRAIN_ORDER)}

# Postgres unquoted identifier shape — same alphabet as `Entity.name`
# and `CanonicalJoin.name` so a metric's identifiers satisfy the same
# invariants the bound entity does.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")

# `<entity>.<column>` form for `time_dimension` — exactly one dot, both
# parts identifier-shaped. Rejects `schema.table.column` (two dots)
# and bare identifiers (no dot). Tracks the same alphabet as
# `_IDENT_RE` (including `$`).
_QUALIFIED_COLUMN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*\.[A-Za-z_][A-Za-z0-9_$]*$")


class DbtOwnedMetricError(ValueError):
    """Raised when a manual or LLM-suggested write targets a dbt-owned metric.

    A metric with `origin = "dbt_import"` is owned upstream by the
    user's dbt project. The dbt-metrics importer is the only code
    path allowed to overwrite it; manual edits and LLM-suggested
    edits are refused at the store boundary so a re-import never has
    to litigate "whose version of the metric wins."

    Subclass of `ValueError` so callers that just want to surface
    "this write was refused" can `except ValueError`.

    Lives in this module (`core/metric.py`) — not in `core/store.py` —
    because the `Store` Protocol's `write_metric` contract names it
    explicitly. Mirrors `DbtOwnedEntityError` from `core/entity.py`.
    """


@dataclass(frozen=True)
class MetricMeasure:
    """The aggregation + column pair that defines a metric's value.

    `agg` is one of the six closed-grammar `AggFunction` values;
    `column` is a bare identifier on the anchored entity's table.
    Composite expressions (`case when ... then x end`) are deferred to
    v2's expression layer — at v1 the column is a single identifier
    on the entity's bound table.
    """

    agg: AggFunction
    column: str

    def __post_init__(self) -> None:
        if self.agg not in _VALID_AGGS:
            raise ValueError(
                f"agg must be one of {sorted(_VALID_AGGS)} (got {self.agg!r})"
            )
        if not _IDENT_RE.fullmatch(self.column):
            raise ValueError(f"column must be an identifier (got {self.column!r})")


@dataclass(frozen=True)
class Metric:
    """A named, entity-anchored business measure with optional time bucketing.

    `name` is the primary identifier; `(source_connection_id, name)` is
    the storage PK. `entity` references `entities (source_connection_id,
    name)` — the store enforces it must exist before this metric is
    written.

    `time_dimension` and `time_grains` are paired: either both set or
    both absent. When set, `time_dimension` is `<entity>.<column>`
    form, where the entity is the metric's anchor entity (v1) and the
    column is a timestamp on that entity's table. `time_grains` is the
    set of bucket sizes the compiler is permitted to emit
    `date_trunc()` calls for; it must be canonically sorted and have
    no duplicates.
    """

    name: str
    description: str
    entity: str
    measure: MetricMeasure
    time_dimension: str | None
    time_grains: tuple[TimeGrain, ...]
    origin: MetricOrigin = "manual"

    def __post_init__(self) -> None:
        if not _IDENT_RE.fullmatch(self.name):
            raise ValueError(f"name must be an identifier (got {self.name!r})")
        if not _IDENT_RE.fullmatch(self.entity):
            raise ValueError(f"entity must be an identifier (got {self.entity!r})")

        # Parallel-emptiness invariant: time_dimension and time_grains
        # both set or both absent. A time dimension without grains is
        # meaningless to the compiler (no bucketing strategy); grains
        # without a dimension is the same incoherence in reverse.
        if self.time_dimension is None and self.time_grains:
            raise ValueError(
                f"time_grains must be empty when time_dimension is None "
                f"(got time_grains={self.time_grains})"
            )
        if self.time_dimension is not None and not self.time_grains:
            raise ValueError(
                f"time_dimension is set but time_grains is empty; either set "
                f"both or neither (got time_dimension={self.time_dimension!r})"
            )

        if self.time_dimension is not None and not _QUALIFIED_COLUMN_RE.fullmatch(
            self.time_dimension
        ):
            raise ValueError(
                f"time_dimension must be 'entity.column' form "
                f"(got {self.time_dimension!r})"
            )

        # Validate every grain is in the closed Literal, then enforce
        # canonical sort + uniqueness. Doing the uniqueness check via
        # a set comparison would lose ordering information; we want
        # to give a precise error message when ordering is wrong vs
        # when duplicates are present, so the checks are split.
        for grain in self.time_grains:
            if grain not in _VALID_GRAINS:
                raise ValueError(
                    f"time_grains must be a subset of {sorted(_VALID_GRAINS)} "
                    f"(got {grain!r})"
                )
        if len(set(self.time_grains)) != len(self.time_grains):
            raise ValueError(
                f"time_grains must be unique (got {self.time_grains})"
            )
        ranks = [_TIME_GRAIN_RANK[g] for g in self.time_grains]
        if ranks != sorted(ranks):
            raise ValueError(
                f"time_grains must be in canonical order "
                f"({list(_TIME_GRAIN_ORDER)}); got {list(self.time_grains)}"
            )

        # Origin is type-checked statically via Literal, but the
        # runtime guard catches malformed YAML and untyped callers
        # before the store-side CHECK has a chance to.
        if self.origin not in _VALID_ORIGINS:
            raise ValueError(
                f"origin must be one of {sorted(_VALID_ORIGINS)} (got {self.origin!r})"
            )
