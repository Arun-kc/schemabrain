"""Parameterised SQL emitter.

Turns a `MetricPlan` into a `(sql_text, params)` pair. Every literal
input enters via a `:p_*` placeholder bound in `params`; no caller
data is interpolated into the SQL text. The emitter never produces a
`;` and never emits more than one statement — these are v2-substrate
invariants per `project_v2_security_invariants.md` (I1 single-statement
+ I3 parameterisation).

Identifier interpolation is safe because every identifier in a
`MetricPlan` (entity names, column names, qualified tables) originates
from store-validated rows whose shape was checked by the dataclass
`__post_init__` and/or SQL CHECK constraints. The emitter does NOT
sanitize them — it trusts the contract.

Postgres dialect (date_trunc, comparison operators). v1 ships one
engine; v2 dialect-switching is a future seam.
"""

from __future__ import annotations

import re
from typing import Any

from schemabrain.semantic.compiler.plan import (
    MetricPlan,
    ResolvedPredicate,
)

# Defense-in-depth assertion: emitted SQL must not contain `;`. The
# emitter never generates one, but a future maintainer adding a code
# path that does will trip this assertion in CI before it ships.
# Identifiers and parameter values are kept out of SQL text via the
# parameterised binding API, so this regex is purely a sanity check
# on the emitter's output — NOT a sanitization step on input.
_SEMICOLON_RE = re.compile(r";")

# Maps the closed `Operator` Literal to its SQL operator. `is_null` /
# `not_null` are emitted as `IS NULL` / `IS NOT NULL` and don't bind
# a parameter. `in` / `not_in` expand to `IN (:p1, :p2, ...)` with N
# parameters; the predicate's `param_names` carry the names.
_OP_TO_SQL: dict[str, str] = {
    "eq": "=",
    "ne": "<>",
    "lt": "<",
    "lte": "<=",
    "gt": ">",
    "gte": ">=",
}


def emit_sql(plan: MetricPlan) -> tuple[str, dict[str, Any]]:
    """Emit the parameterised SQL + parameter map for `plan`.

    Returns `(sql_text, params)` where `sql_text` is a single
    statement with `:p_*` placeholders and `params` carries the
    bound values keyed by placeholder name (without the colon).

    The emitted shape:

        SELECT
          [date_trunc(:p_bucket, <time_dim>) AS time_bucket,]
          [<group_col> AS group_col_N,]+
          <agg>(<measure_col>) AS <metric_name>
        FROM <anchor_table> AS <anchor_alias>
        [JOIN <target_table> AS <target_alias>
            ON <pair_0> [AND <pair_N>]]+
        [WHERE <predicate> [AND <predicate>]+]
        [GROUP BY <bucket_col>, group_col_0, ...]
        LIMIT :p_limit

    The GROUP BY is emitted iff there's a time bucket OR group_by
    columns — a metric called with neither produces a single-row
    aggregate (the SQL standard implicit-GROUP-BY shape).

    LIMIT is always emitted as a defense against pathological
    group_by combinations exploding row count.
    """
    select_parts: list[str] = []
    group_by_parts: list[str] = []
    params: dict[str, Any] = {}

    # Defense-in-depth invariant: a time bucket requires a time
    # dimension on the metric. The resolver enforces this — but
    # asserting here converts a future resolver bug into a crash
    # instead of a silently-wrong SQL emission.
    assert plan.time_bucket is None or plan.metric.time_dimension is not None, (
        "compiler invariant: time_bucket requires metric.time_dimension"
    )

    # Time bucket — date_trunc('day' | 'week' | ...) — emitted only
    # when a grain was requested AND the metric has a time_dimension.
    # The time dimension is on the anchor entity by Decision 2 (v1
    # cross-entity time dimensions defer to v2), so the alias is
    # always the anchor alias.
    #
    # `time_bucket` is interpolated as a SQL LITERAL rather than a
    # parameter binding. This is safe-by-construction because
    # `TimeGrain` is a closed-grammar Literal of 5 values
    # (day/week/month/quarter/year) that the resolver verified against
    # `metric.time_grains` before reaching the emitter. Postgres's
    # `date_trunc(field text, source timestamp)` ACCEPTS a bound text
    # parameter at runtime, but driver-side type adaptation has been
    # observed to surface confusingly across psycopg / SQLAlchemy
    # versions when the field argument is parameter-bound vs literal-
    # quoted. Inlining is unambiguous, has zero injection surface,
    # and matches how dbt's semantic layer emits the same construct.
    # Identifiers in the emitted SQL are double-quoted so Schema Brain
    # entity / column names that happen to be SQL reserved words
    # (`order`, `user`, `select`, `from`, `where`, `group`, etc.) stay
    # safe at execution. The store-validated `_IDENT_RE` shape rules
    # out injection; quoting handles the keyword case Postgres rejects.
    quoted_anchor_alias = f'"{plan.anchor_alias}"'

    if plan.time_bucket is not None and plan.metric.time_dimension is not None:
        _, time_column = plan.metric.time_dimension.split(".", 1)
        select_parts.append(
            f"date_trunc('{plan.time_bucket}', "
            f'{quoted_anchor_alias}."{time_column}") AS time_bucket'
        )
        group_by_parts.append("time_bucket")

    # Group_by columns (resolved upstream so each is a ResolvedColumn).
    # SELECT alias is `group_col_N` so a GROUP BY referencing the
    # alias is unambiguous regardless of the underlying column name.
    for index, group_col in enumerate(plan.group_by_columns):
        alias = f"group_col_{index}"
        select_parts.append(f"{group_col.column_ref} AS {alias}")
        group_by_parts.append(alias)

    # The measure — `agg("anchor"."column") AS metric_name`. The
    # measure column is always on the anchor entity by Decision 2.
    # `count_distinct` becomes `count(DISTINCT ...)` — the SQL idiom.
    measure = plan.metric.measure
    measure_ref = f'{quoted_anchor_alias}."{measure.column}"'
    if measure.agg == "count_distinct":
        agg_expr = f"count(DISTINCT {measure_ref})"
    else:
        agg_expr = f"{measure.agg}({measure_ref})"
    select_parts.append(f'{agg_expr} AS "{plan.metric.name}"')

    # FROM + JOIN clauses. The resolver carries the full ResolvedJoin
    # per traversed entity (sorted by canonical name) so emission is
    # deterministic and doesn't need a second store round-trip.
    # Qualified-table names (`schema.table`) are NOT quoted as a
    # single token — Postgres requires `"schema"."table"`, not
    # `"schema.table"`. We split on the dot (entity validation
    # guarantees exactly one) and quote each half.
    from_clause = f"FROM {_quote_qualified_table(plan.anchor_table)} AS {quoted_anchor_alias}"
    # Defense-in-depth on the topological-order invariant.
    # `plan.joins` is contracted to be topologically ordered by the
    # resolver. If a future refactor (or a malformed plan from a
    # programmatic caller) violates that contract, the JOIN emission
    # below would silently produce SQL that references an alias before
    # it is defined — and the database would reject it with a confusing
    # "table not found" error several layers away from the bug. Track
    # the set of aliases already introduced (anchor + previously emitted
    # joins) and assert each join's `source_alias` is already known
    # before emitting it.
    known_aliases: set[str] = {plan.anchor_alias}
    join_clauses: list[str] = []
    for resolved_join in plan.joins:
        if resolved_join.source_alias not in known_aliases:
            raise RuntimeError(
                f"MetricPlan.joins is not topologically ordered: "
                f"join {resolved_join.canonical_name!r} references source "
                f"alias {resolved_join.source_alias!r} which has not yet "
                f"been introduced. Known aliases at this point: "
                f"{sorted(known_aliases)}. This is a compiler-invariant "
                f"violation, not an agent-input error."
            )
        # Multi-hop chains: each JOIN's left-side alias is the previous
        # hop's alias (carried on `resolved_join.source_alias`), not the
        # metric anchor. The resolver also pre-orients `on_pairs` so
        # `source_column` always refers to the LEFT side, regardless of
        # which direction the canonical join was traversed.
        quoted_source_alias = f'"{resolved_join.source_alias}"'
        quoted_target_alias = f'"{resolved_join.target_alias}"'
        on_clause = " AND ".join(
            f'{quoted_source_alias}."{pair.source_column}" = '
            f'{quoted_target_alias}."{pair.target_column}"'
            for pair in resolved_join.on_pairs
        )
        join_clauses.append(
            f"JOIN {_quote_qualified_table(resolved_join.target_table)} "
            f"AS {quoted_target_alias} "
            f"ON {on_clause}"
        )
        known_aliases.add(resolved_join.target_alias)

    # WHERE predicates.
    where_parts: list[str] = []
    for predicate in plan.filter_predicates:
        sql, sql_params = _emit_predicate(predicate)
        where_parts.append(sql)
        params.update(sql_params)
    where_clause = ""
    if where_parts:
        where_clause = "WHERE " + " AND ".join(where_parts)

    # GROUP BY.
    group_by_clause = ""
    if group_by_parts:
        group_by_clause = "GROUP BY " + ", ".join(group_by_parts)

    # LIMIT — always emitted.
    params["p_limit"] = plan.limit

    sql_text = "\n".join(
        line
        for line in (
            "SELECT " + ",\n       ".join(select_parts),
            from_clause,
            *join_clauses,
            where_clause,
            group_by_clause,
            "LIMIT :p_limit",
        )
        if line
    )

    if _SEMICOLON_RE.search(sql_text):  # pragma: no cover — defensive
        raise RuntimeError(
            "compiler emitted SQL contains ';'; this violates the "
            "single-statement invariant. This is a compiler bug."
        )

    return sql_text, params


def _quote_qualified_table(qualified: str) -> str:
    """Quote each half of a `schema.table` qualified name.

    Postgres rejects `"schema.table"` (one quoted token containing a
    dot) — the dot is a separator, not part of the identifier. Each
    side must be quoted independently: `"schema"."table"`.

    Schema Brain entity validation guarantees exactly one dot via the
    `_QUALIFIED_TABLE_RE` regex, so splitting on `.` is safe.
    """
    schema, table = qualified.split(".", 1)
    return f'"{schema}"."{table}"'


def _emit_predicate(predicate: ResolvedPredicate) -> tuple[str, dict[str, Any]]:
    column_ref = predicate.column.column_ref
    if predicate.op == "is_null":
        return f"{column_ref} IS NULL", {}
    if predicate.op == "not_null":
        return f"{column_ref} IS NOT NULL", {}
    if predicate.op in ("in", "not_in"):
        placeholders = ", ".join(f":{name}" for name in predicate.param_names)
        op_sql = "IN" if predicate.op == "in" else "NOT IN"
        params = {name: predicate.value[idx] for idx, name in enumerate(predicate.param_names)}
        return f"{column_ref} {op_sql} ({placeholders})", params
    sql_op = _OP_TO_SQL[predicate.op]
    name = predicate.param_names[0]
    return f"{column_ref} {sql_op} :{name}", {name: predicate.value}
