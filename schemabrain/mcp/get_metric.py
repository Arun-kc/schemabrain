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
  - `AmbiguousPathError`           → `ambiguous_path`
  - `UnknownViaJoinError`          → `unknown_via_join`
  - `UnknownOrderByColumnError`    → `unknown_order_by_column`
  - `UnknownGroupByColumnError`    → `unknown_group_by_column`
  - `UnknownFilterColumnError`     → `unknown_filter_column`
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

import sys
from typing import Any, cast

from schemabrain.core.entity import InferenceMethod, ValidationState
from schemabrain.core.metric import _VALID_GRAINS, TimeGrain
from schemabrain.core.store_protocol import Store
from schemabrain.mcp._helpers import _with_token_estimate
from schemabrain.mcp.envelope import derive_confidence
from schemabrain.mcp.metric_executor import MetricExecutor
from schemabrain.mcp.shapes import MetricFilterArg, MetricOrderByArg, MetricResult
from schemabrain.pii import PIICategory, Sensitivity, propagate
from schemabrain.semantic.compiler import (
    InvalidTimeGrainError,
    MalformedColumnError,
    PiiBlockedError,
    RequestedFilter,
    RequestedOrderBy,
    emit_sql,
    resolve_metric_plan,
)
from schemabrain.semantic.compiler.plan import MetricPlan

# One-shot stderr warnings for silent-failure protections — keyed on
# `(source_connection_id, tool)` so a single misconfiguration doesn't
# flood logs across many repeated calls.
_empty_tag_table_warned: set[str] = set()

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
    time_dimension: str | None = None,
    limit: int = 1000,
    via: tuple[str, ...] = (),
    order_by: tuple[MetricOrderByArg, ...] = (),
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

    `time_dimension` is the v1.2 disambiguator for time-dim inheritance:
    when a metric carries no local `time_dimension` and 2+ timestamp
    columns are reachable via canonical-join chains, the resolver
    raises `AmbiguousTimeDimensionError` listing the candidates.
    The agent re-calls with `time_dimension="<entity>.<column>"`
    chosen from that list. The arg is silently ignored when the
    metric has a local `time_dimension` (the metric's own declared
    dimension always wins — overriding it would shift semantics).

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
    compiler_order_by = tuple(
        RequestedOrderBy(column=o.column, direction=o.direction) for o in order_by
    )

    plan = resolve_metric_plan(
        store=store,
        source_connection_id=source_connection_id,
        metric_name=name,
        group_by=group_by,
        filters=compiler_filters,
        time_grain=narrowed_grain,
        time_dimension=time_dimension,
        limit=limit,
        via=via,
        order_by=compiler_order_by,
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

    metric_method = plan.metric.inference_method
    metric_state = plan.metric.validation_state
    aggregate_method, aggregate_state = _aggregate_metric_plan_signal(
        store=store,
        source_connection_id=source_connection_id,
        metric_method=metric_method,
        metric_state=metric_state,
        required_join_names=plan.required_join_names,
    )

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
        metric_inference_method=metric_method,
        metric_validation_state=metric_state,
        aggregate_inference_method=aggregate_method,
        aggregate_validation_state=aggregate_state,
        time_dimension_resolution=plan.time_dimension_resolution,
        inherited_time_dimension=plan.inherited_time_dimension,
        time_dimension_inherited_via=plan.time_dimension_inherited_via,
    )
    return _with_token_estimate(partial)


def _aggregate_metric_plan_signal(
    *,
    store: Store,
    source_connection_id: str,
    metric_method: InferenceMethod,
    metric_state: ValidationState,
    required_join_names: tuple[str, ...],
) -> tuple[InferenceMethod, ValidationState]:
    """Worst-case (inference_method, validation_state) across the metric
    AND every canonical join the plan traverses.

    The SQL the agent receives depends on each join's ON-clause. If
    even one join is LLM-suggested rather than FK-derived, the agent
    cannot trust the join columns blindly — the aggregate signal
    inherits the weaker member. An FK-derived metric anchored over
    an LLM-guessed join is therefore MEDIUM, not HIGH: the metric's
    derivation is strong, but the SQL relies on an unverified
    on-clause.

    No-op when `required_join_names` is empty (metric-only path) —
    returns the metric's own signal verbatim.
    """
    _RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
    worst_method, worst_state = metric_method, metric_state
    worst_rank = _RANK[derive_confidence(worst_method, worst_state)]
    for join_name in required_join_names:
        canonical_join = store.get_canonical_join(
            join_name, source_connection_id=source_connection_id
        )
        if canonical_join is None:
            # Unexpected — `required_join_names` came from the
            # resolved plan, so every name MUST exist. Skip
            # defensively rather than crash; the metric's own
            # signal dominates.
            continue
        rank = _RANK[
            derive_confidence(canonical_join.inference_method, canonical_join.validation_state)
        ]
        if rank < worst_rank:
            worst_method = canonical_join.inference_method
            worst_state = canonical_join.validation_state
            worst_rank = rank
    return (worst_method, worst_state)


def _resolve_pii_categories(
    *,
    store: Store,
    source_connection_id: str,
    plan: MetricPlan,
    pii_block: frozenset[PIICategory],
) -> tuple[PIICategory, ...]:
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

    Silent-failure protections:
      - When the entire `column_pii_tags` lookup yields zero hits
        across all qualified tables touched, emit a one-shot stderr
        warning per `source_connection_id`: the operator may have
        skipped classification or never indexed this source, and
        without a warning the empty `pii_categories` audit field
        looks identical to a clean "no PII touched" call.
      - When `time_dimension` is set but malformed (no `.`), raise
        `MalformedColumnError` so the server maps it to a clear
        envelope kind instead of letting an `IndexError` fall
        through to `internal_error`.
    """
    by_table: dict[str, set[str]] = {}

    anchor_table = plan.anchor_table
    # Composite-expression measures (`measure.expression is not None`)
    # reference multiple operand columns, all on the anchor table. The
    # `measure_columns` property returns the set for both the v1
    # bare-column path and the v2 composite-expression path so PII
    # propagation covers every touched column symmetrically. Without
    # this, a PII-tagged operand inside a composite expression (e.g.
    # `email_hash * weight`) would silently bypass `--pii-block`.
    by_table.setdefault(anchor_table, set()).update(plan.metric.measure.measure_columns)
    if plan.metric.time_dimension is not None:
        # Form is "<entity>.<column>"; the column belongs to the
        # anchor entity's table per the v1 metric model. Guarded
        # split — a malformed value (no dot) would otherwise raise
        # IndexError and surface as internal_error with no breadcrumb.
        if "." not in plan.metric.time_dimension:
            raise MalformedColumnError(
                f"metric.time_dimension must be '<entity>.<column>' form, "
                f"got {plan.metric.time_dimension!r}"
            )
        time_column = plan.metric.time_dimension.split(".", 1)[1]
        by_table[anchor_table].add(time_column)

    for resolved in plan.group_by_columns:
        by_table.setdefault(resolved.qualified_table, set()).add(resolved.column)
    for predicate in plan.filter_predicates:
        by_table.setdefault(predicate.column.qualified_table, set()).add(predicate.column.column)

    # Include canonical-join ON-clause columns. Multi-hop chains touch
    # FK columns on every intermediate hop even when no group_by or
    # filter explicitly references those columns. Without this loop,
    # a PII-tagged FK used only in a JOIN predicate (e.g.
    # `email = email` between two tables) would silently bypass
    # `--pii-block` enforcement — contradicting the "every column
    # touched by the SQL is tagged" invariant the agent envelope
    # claims. `plan.joins` is in topological chain order so each hop's
    # `source_alias` already has a known qualified table by the time
    # we reach it.
    alias_to_table: dict[str, str] = {plan.anchor_alias: plan.anchor_table}
    for resolved_join in plan.joins:
        source_table = alias_to_table.get(resolved_join.source_alias, plan.anchor_table)
        for pair in resolved_join.on_pairs:
            by_table.setdefault(source_table, set()).add(pair.source_column)
            by_table.setdefault(resolved_join.target_table, set()).add(pair.target_column)
        alias_to_table[resolved_join.target_alias] = resolved_join.target_table

    per_column: list[tuple[Sensitivity, frozenset[PIICategory]]] = []
    total_hits = 0
    for qualified_table, columns in by_table.items():
        tags = store.get_column_pii_tags(
            source_connection_id=source_connection_id,
            qualified_table=qualified_table,
            columns=columns,
        )
        total_hits += len(tags)
        # Columns absent from `tags` are untagged → treat as
        # ("public", frozenset()), per the propagation helper's
        # empty-input contract. Iterating `columns` (not `tags`)
        # ensures we account for the absent ones explicitly.
        for col in columns:
            tag: tuple[Sensitivity, frozenset[PIICategory]] = tags.get(col, ("public", frozenset()))
            per_column.append(tag)

    if total_hits == 0 and source_connection_id not in _empty_tag_table_warned:
        _empty_tag_table_warned.add(source_connection_id)
        print(
            f"schemabrain get_metric: no PII tags found for any column "
            f"touched by source {source_connection_id!r}. Run "
            f"`schemabrain index` without `--no-pii-classify` to "
            f"populate tags; otherwise --pii-block enforcement is a "
            f"no-op and audit rows record pii_categories=''.",
            file=sys.stderr,
        )

    _, propagated = propagate(per_column)

    if pii_block:
        blocked = propagated & pii_block
        if blocked:
            attempted_tuple = cast(tuple[PIICategory, ...], tuple(sorted(propagated)))
            blocked_tuple = cast(tuple[PIICategory, ...], tuple(sorted(blocked)))
            # Message references `blocked_tuple` — the policy
            # intersection that actually triggered refusal — not
            # `attempted_tuple` (the full propagated set). Surfacing
            # `attempted` to the agent would be a probe oracle: an
            # adversarial agent could iterate `get_metric` calls with
            # different `group_by` columns and reconstruct the full
            # PII tagging in O(columns) calls without ever calling
            # `describe_entity`. `attempted_tuple` stays in the audit
            # row (operator-visible) via the `PiiBlockedError` field.
            raise PiiBlockedError(
                f"get_metric refused: metric touches PII categories "
                f"{list(blocked_tuple)} that this server policy blocks",
                attempted_categories=attempted_tuple,
                blocked_categories=blocked_tuple,
                anchor_entity=plan.metric.entity,
            )

    return cast(tuple[PIICategory, ...], tuple(sorted(propagated)))


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
