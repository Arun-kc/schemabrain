"""MCP tool implementation: get_metric.

The first MCP tool that returns NUMBERS, not metadata. Given a metric
name + optional group_by / filters / time_grain / limit, the tool:

  1. Resolves the request through the compiler to a `MetricPlan`,
     surfacing structured errors as Charter-v1.1 envelope kinds.
  2. Emits parameterised SQL via the compiler's `emit_sql` —
     always single-statement, always `:p_*` bound, always with
     `LIMIT :p_limit`.
  3. Executes the SQL through the injected `MetricExecutor`.
  4. Packages the rows + the SQL + provenance into `MetricResult`.

Error mapping (compiler-side → envelope kind):
  - `UnknownMetricError`           → `unknown_metric`
  - `MalformedColumnError`         → `malformed_name`
  - `UnknownColumnError`           → `unknown_name`
  - `UnreachableEntityError`       → `unreachable_entity`
  - `AmbiguousJoinError`           → `ambiguous_join`
  - `InvalidTimeGrainError`        → `invalid_time_grain`
  - `PiiBlockedError` (reserved)   → `pii_blocked`

`get_metric` only executes the parameterised SQL the compiler emits.
Agent-supplied raw SQL is a v2 wedge (`validate_query` + `execute`).
Read-only enforcement lives at the connection layer (the
`EngineMetricExecutor` opens connections with
`default_transaction_read_only=on`); the compiler invariants are
defense-in-depth.
"""

from __future__ import annotations

from typing import Any

from schemabrain.core.metric import _VALID_GRAINS, TimeGrain
from schemabrain.core.store_protocol import Store
from schemabrain.mcp._helpers import _with_token_estimate
from schemabrain.mcp.metric_executor import MetricExecutor
from schemabrain.mcp.shapes import MetricFilterArg, MetricResult
from schemabrain.pii import PIICategory, propagate
from schemabrain.semantic.compiler import (
    InvalidTimeGrainError,
    PiiBlockedError,
    RequestedFilter,
    emit_sql,
    resolve_metric_plan,
)

# Placeholder fingerprint stamped on the MetricResult before the
# `@instrument` decorator overwrites it with the real `mcp_audit` row's
# fingerprint hex. The placeholder only surfaces when the server is
# wired without an audit writer (test contexts, the `--no-audit` CLI
# path). Production deployments see the real digest.
_FINGERPRINT_UNSET = "fp-unset"


def get_metric_impl(
    *,
    store: Store,
    executor: MetricExecutor,
    source_connection_id: str,
    name: str,
    group_by: tuple[str, ...] = (),
    filters: tuple[MetricFilterArg, ...] = (),
    time_grain: str | None = None,
    limit: int = 1000,
    pii_block: frozenset[PIICategory] = frozenset(),
) -> MetricResult:
    """Resolve, emit, execute. Returns a `MetricResult` on success.

    Raises any of the compiler error classes
    (`MetricCompilerError` subclasses); the MCP server wrapper maps
    each to a Charter-v1.1 envelope kind.

    `RuntimeError` may surface from the executor on database failures
    — the wrapper maps this to `internal_error`.

    `time_grain` accepts `str | None` at the boundary because FastMCP
    callers ship raw strings; we narrow to `TimeGrain` here so an
    invalid grain raises `InvalidTimeGrainError` cleanly at the API
    seam instead of failing several layers down inside the resolver.
    The compiler's `_check_time_grain` is the second layer of
    defense (against programmatic callers that bypass MCP).

    `pii_block` is the server-level `--pii-block` policy set. After
    plan resolution, the compiler looks up the PII tags of every
    column the metric touches, propagates them across the result,
    and raises `PiiBlockedError` if any propagated category appears
    in `pii_block`. Empty `pii_block` disables enforcement (tags
    still flow through to `MetricResult.pii_categories` for audit).
    """
    narrowed_grain: TimeGrain | None = None
    if time_grain is not None:
        if time_grain not in _VALID_GRAINS:
            # Surfacing the same error the resolver would raise keeps
            # the MCP envelope mapping unchanged — the wrapper
            # already routes `InvalidTimeGrainError` to the
            # `invalid_time_grain` kind.
            raise InvalidTimeGrainError(
                requested_grain=time_grain,  # type: ignore[arg-type]
                allowed_grains=(),
            )
        # Literal narrowing is via the runtime membership check.
        narrowed_grain = time_grain  # type: ignore[assignment]

    # Convert Pydantic-side `MetricFilterArg` to compiler-side
    # `RequestedFilter`. The shape is one-to-one — the boundary
    # exists so the compiler doesn't depend on Pydantic.
    compiler_filters = tuple(
        RequestedFilter(column=f.column, op=f.op, value=f.value) for f in filters
    )

    plan = resolve_metric_plan(
        store=store,
        source_connection_id=source_connection_id,
        metric_name=name,
        group_by=group_by,
        filters=compiler_filters,
        time_grain=narrowed_grain,
        limit=limit,
    )

    # Tag propagation. Collect every column the metric will touch
    # (measure column + time-bucket column + group_by columns +
    # filter columns), bulk-fetch each (qualified_table, column)
    # pair's PII tags from the store, propagate, and either raise
    # PiiBlockedError or attach the result to MetricResult.
    pii_categories_sorted = _resolve_pii_categories(
        store=store,
        source_connection_id=source_connection_id,
        plan=plan,
        pii_block=pii_block,
    )

    sql_text, sql_params = emit_sql(plan)
    rows = executor.execute(sql_text, sql_params)

    partial = MetricResult(
        rows=rows,
        row_count=len(rows),
        sql_skeleton=sql_text,
        sql_params=_serialise_params(sql_params),
        fingerprint=_FINGERPRINT_UNSET,
        token_estimate=0,
        required_joins=list(plan.required_join_names),
        fan_out_join_names=list(plan.fan_out_join_names),
        pii_categories=pii_categories_sorted,
    )
    return _with_token_estimate(partial)


def _resolve_pii_categories(
    *,
    store: Store,
    source_connection_id: str,
    plan: Any,
    pii_block: frozenset[PIICategory],
) -> tuple[str, ...]:
    """Look up tags for every column the plan touches, propagate,
    enforce `pii_block` policy, and return a sorted category tuple.

    Touched columns:
      - `plan.metric.measure.column` on `plan.anchor_table`
      - `plan.metric.time_dimension` (entity.column form) on
        `plan.anchor_table` when set — anchored on the same entity
        per v1 metric model
      - every `plan.group_by_columns[*]` (already carries
        `qualified_table`)
      - every `plan.filter_predicates[*].column` (same)

    Grouped per qualified_table so the store's bulk read sees one
    SQL per table (a metric anchored on one entity issues exactly
    one lookup at v1 — joins are not part of v1's compiler emission
    for this PR's surface).
    """
    by_table: dict[str, set[str]] = {}

    anchor_table = plan.anchor_table
    by_table.setdefault(anchor_table, set()).add(plan.metric.measure.column)
    if plan.metric.time_dimension is not None:
        # Form is "<entity>.<column>"; the column belongs to the
        # anchor entity's table per the v1 metric model.
        time_column = plan.metric.time_dimension.split(".", 1)[1]
        by_table[anchor_table].add(time_column)

    for resolved in plan.group_by_columns:
        by_table.setdefault(resolved.qualified_table, set()).add(resolved.column)
    for predicate in plan.filter_predicates:
        by_table.setdefault(predicate.column.qualified_table, set()).add(predicate.column.column)

    per_column: list[tuple[str, frozenset[str]]] = []
    for qualified_table, columns in by_table.items():
        tags = store.get_column_pii_tags(
            source_connection_id=source_connection_id,
            qualified_table=qualified_table,
            columns=columns,
        )
        # Columns absent from `tags` are untagged → treat as
        # ("public", frozenset()), per the propagation helper's
        # empty-input contract. Iterating `columns` (not `tags`)
        # ensures we account for the absent ones explicitly.
        for col in columns:
            sensitivity, categories = tags.get(col, ("public", frozenset()))
            per_column.append((sensitivity, categories))

    _, propagated = propagate(per_column)

    if pii_block:
        blocked = propagated & pii_block
        if blocked:
            attempted_tuple = tuple(sorted(propagated))
            blocked_tuple = tuple(sorted(blocked))
            raise PiiBlockedError(
                f"get_metric refused: metric touches PII categories "
                f"{list(attempted_tuple)} which intersect server policy "
                f"--pii-block {list(blocked_tuple)}",
                attempted_categories=attempted_tuple,
                blocked_categories=blocked_tuple,
            )

    return tuple(sorted(propagated))


def _serialise_params(params: dict[str, Any]) -> dict[str, Any]:
    """Convert binding params into JSON-serialisable form for the
    envelope.

    The compiler emits Python primitives; the envelope ships JSON over
    the MCP wire. Datetimes / dates would need an `.isoformat()` —
    but the v1 filter operators only emit scalars / lists of scalars
    the compiler accepted as `RequestedFilter.value`, so this is the
    identity function in practice. The function exists so a future
    operator (e.g. date filters) has a place to plug serialisation
    without spreading conversion logic.
    """
    return params
