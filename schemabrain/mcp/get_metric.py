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
from schemabrain.semantic.compiler import (
    InvalidTimeGrainError,
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
    )
    return _with_token_estimate(partial)


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
