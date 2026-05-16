"""sqlglot-based extraction of equi-join predicates from query log SQL.

The the canonical-join suggester reads from two evidence sources: FK
constraints (always present, deterministic) and query-log predicates
(present when `mine-queries` has populated `example_queries`). This
module owns the second source — it walks the AST of each statement and
lifts JOIN predicates of the form `<alias>.<col> = <alias>.<col>` into
direction-normalised `ExtractedJoin` records.

Scope at v1:

  - Explicit `JOIN ... ON` clauses on the top-level SELECT only.
    Comma-joins (`FROM a, b WHERE a.x = b.y`), implicit subquery joins,
    and CTE joins are deferred — they each need a richer FROM-tree
    walker the v1 surface doesn't require.
  - Equi-predicates only. Range joins (`ON a.ts BETWEEN ...`) and
    inequality joins are v2 expression-language territory.
  - Both sides of each equality must be a column reference on a
    joined table; subquery LHS, function calls, and literals are
    skipped (they're filter predicates masquerading as ON clauses).
  - `CROSS JOIN` and `USING (col)` are silently dropped — the
    suggester would have no on-columns to surface as a candidate.

Composite-key joins (`ON a.c1 = b.c1 AND a.c2 = b.c2`) produce ONE
`ExtractedJoin` per JOIN node with N column pairs in `pairs`. The
AND-chain at the top of the ON clause is walked; OR / NOT / nested
boolean structure short-circuits to "no predicate extractable" so a
defensive `WHERE TRUE` doesn't masquerade as a join.

Direction normalisation: each ExtractedJoin sorts its two endpoints
alphabetically on `(schema, table)`, with the column pairs reoriented
to match. This way `users.id = orders.user_id` and `orders.user_id =
users.id` (same join, different SQL author preference) aggregate to
the same `ExtractedJoin` value.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal, TypeAlias

import sqlglot
from sqlglot import expressions as exp
from sqlglot.errors import SqlglotError

# Internal type alias for the alias-resolved column reference shape
# used during equi-predicate extraction: `(column_name, (schema, table))`.
# Pulls a 5-level-deep type nest out of the inner code path.
_ColEndpoint: TypeAlias = tuple[str, tuple[str, str]]

_logger = logging.getLogger(__name__)

# Q3 resolution — module-level constant, NOT exposed as a CLI flag at
# v1. Mining frequency >= this threshold upgrades evidence confidence
# from "low" to "medium." pg_stat_statements `observation_count` sums
# per-statement call counts, so 5 cleanly separates "incidental"
# (1-2 runs of a one-off ad-hoc query) from "real use" (5+ cumulative
# calls across statements that contain the predicate). Recompile-time
# tunable; revisit if a user surfaces a need for runtime config.
_MEDIUM_CONFIDENCE_FREQUENCY = 5


@dataclass(frozen=True)
class ExtractedJoin:
    """One observed JOIN clause, normalised to direction-canonical form.

    `(schema_a, table_a) <= (schema_b, table_b)` alphabetically — the
    direction the SQL author wrote is NOT preserved here because we're
    aggregating evidence about "are these two tables joined?", not
    "in what order did this statement write them." The user picks the
    canonical direction at `joins apply` time.

    `pairs` carries the equi-join column predicates as `(col_on_a,
    col_on_b)` tuples, ordered consistently with the normalised
    `(table_a, table_b)` direction. Composite joins have len >= 2;
    single-key joins have len == 1. The pairs themselves are sorted
    alphabetically on `col_on_a` for deterministic aggregation.

    Construction normalises automatically — callers may pass `(a, b)`
    or `(b, a)` ordering with arbitrary `pairs`; `__post_init__` flips
    sides + pairs as needed so the persisted state is canonical. This
    means direct construction is safe without going through
    `_make_extracted`, which exists for clarity at the AST-walk call
    site (where the inputs carry semantic left/right meaning even
    though the output should not).
    """

    schema_a: str
    table_a: str
    schema_b: str
    table_b: str
    pairs: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if not self.pairs:
            raise ValueError("ExtractedJoin requires at least one column pair")
        # Auto-normalise direction. If the constructor was given the
        # endpoints in the wrong order, swap sides + flip every column
        # pair so the canonical-form invariant holds. The frozen
        # dataclass requires `object.__setattr__` to mutate fields
        # after `__init__`.
        if (self.schema_a, self.table_a) > (self.schema_b, self.table_b):
            old_a = (self.schema_a, self.table_a)
            old_b = (self.schema_b, self.table_b)
            flipped = tuple(sorted((b, a) for a, b in self.pairs))
            object.__setattr__(self, "schema_a", old_b[0])
            object.__setattr__(self, "table_a", old_b[1])
            object.__setattr__(self, "schema_b", old_a[0])
            object.__setattr__(self, "table_b", old_a[1])
            object.__setattr__(self, "pairs", flipped)
        else:
            # Sort pairs alphabetically on the first element for
            # deterministic aggregation regardless of statement-author
            # column ordering.
            object.__setattr__(self, "pairs", tuple(sorted(self.pairs)))


def _make_extracted(
    *,
    left_schema: str,
    left_table: str,
    right_schema: str,
    right_table: str,
    raw_pairs: list[tuple[str, str]],
) -> ExtractedJoin:
    """Build an `ExtractedJoin` from raw predicate parts at the AST-walk
    call site.

    The constructor handles direction normalisation internally — this
    helper exists only because the AST walk produces left/right
    semantically meaningful inputs (the JOIN's left vs right tables)
    that read cleaner via keyword args. The output is direction-
    canonical regardless of input ordering.
    """
    return ExtractedJoin(
        schema_a=left_schema,
        table_a=left_table,
        schema_b=right_schema,
        table_b=right_table,
        pairs=tuple(raw_pairs),
    )


@dataclass(frozen=True)
class AggregatedJoin:
    """An `ExtractedJoin` with its aggregated `pg_stat_statements`-derived
    `observation_count` summed across every statement that produced the
    same predicate set.

    `frequency` is non-negative — the aggregator sums non-negative
    `observation_count` values from `example_queries`. A negative value
    would silently break confidence-bucket ranking, so it's rejected
    at construction.
    """

    join: ExtractedJoin
    frequency: int

    def __post_init__(self) -> None:
        if self.frequency < 0:
            raise ValueError(
                f"AggregatedJoin.frequency must be non-negative (got {self.frequency})"
            )


def extract_join_predicates(sql: str) -> frozenset[ExtractedJoin]:
    """Return the set of equi-join predicates in `sql`.

    Behaviour:
      - Empty / whitespace input → empty set.
      - Parse failure → empty set (logged at DEBUG; one
        unparseable pg_stat_statements row must not blow up the batch).
      - Non-SELECT top-level → empty set (DML other than SELECT may
        contain joins but the v1 surface is read-focused; revisit when
        UPDATE/DELETE JOIN syntax surfaces in a real workload).
      - Each `JOIN ... ON` clause contributes 0 or 1 `ExtractedJoin`s.
        CROSS JOIN and USING contribute 0. An ON clause with no
        extractable equi-predicate contributes 0.

    Returns a frozenset so callers can union results from multiple
    statements without worrying about duplicates within a single
    statement.
    """
    if not sql or not sql.strip():
        return frozenset()
    try:
        parsed = sqlglot.parse_one(sql, read="postgres")
    except SqlglotError as exc:
        # Mirrors `mining/sql_parse.py:extract_table_references` —
        # parse failures are a routine pg_stat_statements signal,
        # not a programming error.
        _logger.debug("sqlglot failed to parse statement for join extraction: %s", exc)
        return frozenset()
    if parsed is None or not isinstance(parsed, exp.Select):  # pragma: no cover
        return frozenset()

    # Build alias → (schema, table) map from the FROM/JOIN tree.
    alias_map = _build_alias_map(parsed)
    if not alias_map:
        # `SELECT 1` and other FROM-less SELECTs (which DO appear in
        # `pg_stat_statements` — e.g. health-check pings, scalar
        # function tests) produce an `exp.Select` with no `from`
        # clause, so `_build_alias_map` returns an empty dict. No
        # JOIN evidence to extract; return early.
        return frozenset()

    result: set[ExtractedJoin] = set()
    for join_node in parsed.find_all(exp.Join):
        extracted = _extract_from_join_node(join_node, alias_map)
        if extracted is not None:
            result.add(extracted)
    return frozenset(result)


def _build_alias_map(select: exp.Select) -> dict[str, tuple[str, str]]:
    """Walk the FROM + JOIN tree, returning `alias_or_name → (schema, table)`.

    Each `exp.Table` in the FROM clause and every JOIN node contributes
    one entry. The lookup key is either the explicit alias (`FROM users
    u`) or — when no alias is given — the table name itself
    (`FROM users` keys to `'users'`).

    Tables without a schema qualifier resolve to `(empty, name)`. The
    mining caller (the suggest pipeline) reconciles these against
    indexed-tables to drop predicates touching un-indexed tables.

    Subqueries inside FROM / JOIN are skipped — they don't contribute
    canonical-join evidence because there's no fixed underlying table
    on the subquery side. Returning an empty alias map for those is
    handled by the top-level loop's "JOIN with unresolved side"
    behaviour.
    """
    alias_map: dict[str, tuple[str, str]] = {}
    # Walk the FROM tree. `select.args.get("from")` is the From node;
    # the table is its `this` attribute. Joins are `select.args.get(
    # "joins")`.
    from_clause = select.args.get("from")
    if from_clause is not None:
        for table_expr in from_clause.find_all(exp.Table):
            _record_table_alias(table_expr, alias_map)
    for join_node in select.args.get("joins") or []:
        # The right-hand side of a JOIN is `join_node.this`; walk for
        # Table nodes (handles aliased subqueries gracefully — they
        # don't contain a Table node at the top, so nothing is added).
        for table_expr in join_node.find_all(exp.Table):
            _record_table_alias(table_expr, alias_map)
    return alias_map


def _record_table_alias(table_expr: exp.Table, alias_map: dict[str, tuple[str, str]]) -> None:
    """Record one `exp.Table` node's alias-or-name → (schema, table) mapping.

    Defensive against weird shapes: a Table node without a name is
    skipped. Schema may be empty (`""`); the suggest pipeline filters
    on indexed-tables membership downstream so unqualified refs that
    don't match anything get dropped naturally.
    """
    name = table_expr.name
    if not name:  # pragma: no cover — defensive; sqlglot never builds an unnamed Table
        return
    schema = table_expr.db if table_expr.db else ""
    alias = table_expr.alias_or_name
    if (
        alias
    ):  # pragma: no branch — sqlglot always populates alias_or_name (falls back to table name)
        alias_map[alias] = (schema, name)


def _extract_from_join_node(
    join: exp.Join, alias_map: dict[str, tuple[str, str]]
) -> ExtractedJoin | None:
    """Pull the equi-join predicate set out of one JOIN node.

    Returns `None` for:
      - CROSS JOIN (no `on` clause)
      - USING clauses (deferred — requires live column resolution)
      - ON clauses with non-equi predicates only
      - ON clauses where either side of every equality is a non-column
        expression (subquery, function call, literal)
      - JOIN nodes whose two endpoints can't be resolved to a single
        consistent (table_a, table_b) pair via the alias map

    Returns one `ExtractedJoin` carrying all equi-predicates between
    the two endpoints; a composite-key JOIN with N AND'd equalities
    produces one ExtractedJoin with N column pairs.
    """
    on = join.args.get("on")
    if on is None:
        # CROSS JOIN, or USING — kind="cross" / `using` arg populated.
        # We treat both as "no equi-predicate" and return None.
        return None

    # Collect all top-level equality predicates joined by AND.
    equalities = list(_walk_and_equalities(on))
    if not equalities:
        return None

    # For each EQ, resolve LHS/RHS into (alias, column) tuples — drop
    # the predicate if either side isn't a column ref on a known alias.
    column_pairs: list[tuple[_ColEndpoint, _ColEndpoint]] = []
    for eq in equalities:
        lhs = _column_ref(eq.left)
        rhs = _column_ref(eq.right)
        if lhs is None or rhs is None:
            continue
        lhs_alias, lhs_col = lhs
        rhs_alias, rhs_col = rhs
        lhs_table = alias_map.get(lhs_alias)
        rhs_table = alias_map.get(rhs_alias)
        if lhs_table is None or rhs_table is None:
            continue
        column_pairs.append(((lhs_col, lhs_table), (rhs_col, rhs_table)))
    if not column_pairs:  # pragma: no cover — covered by `or`/non-column-ref tests at the outer level via `_walk_and_equalities` short-circuit
        return None

    # The two tables in this join clause are well-defined ONLY if all
    # predicates touch the same pair of underlying tables. A composite
    # PK join always has all predicates between the same two tables.
    # If a predicate slips through with a different pair, drop the
    # whole JOIN (defensive — better to lose evidence than emit a
    # bogus mixed-table join).
    first_lhs_table = column_pairs[0][0][1]
    first_rhs_table = column_pairs[0][1][1]
    tables_pair = frozenset([first_lhs_table, first_rhs_table])
    raw_pairs: list[tuple[str, str]] = []
    for (lhs_col, lhs_table), (rhs_col, rhs_table) in column_pairs:
        this_pair = frozenset([lhs_table, rhs_table])
        if this_pair != tables_pair:
            _logger.debug(
                "JOIN clause spans multiple table pairs (got %s vs %s) — skipping",
                tables_pair,
                this_pair,
            )
            return None
        if lhs_table == first_lhs_table:
            raw_pairs.append((lhs_col, rhs_col))
        else:
            # Predicate written with flipped side — normalise to the
            # first-predicate's table order so the pair list stays
            # consistent.
            raw_pairs.append((rhs_col, lhs_col))

    return _make_extracted(
        left_schema=first_lhs_table[0],
        left_table=first_lhs_table[1],
        right_schema=first_rhs_table[0],
        right_table=first_rhs_table[1],
        raw_pairs=raw_pairs,
    )


def _walk_and_equalities(expr: exp.Expression) -> Iterable[exp.EQ]:
    """Yield every top-level `EQ` node reachable via AND chains.

    A predicate like `a.x = b.y AND a.z = b.w AND c.foo = 'bar'` yields
    three EQ nodes; the caller filters them down to those whose sides
    are column references.

    Stops at non-AND boundaries — does NOT descend into OR / NOT / nested
    parentheses with non-AND content. An ON clause like `a.x = b.y OR
    a.z = b.w` is treated as "no canonical equi-predicate" because the
    user's intent isn't a deterministic join condition.
    """
    if isinstance(expr, exp.And):
        yield from _walk_and_equalities(expr.left)
        yield from _walk_and_equalities(expr.right)
    elif isinstance(expr, exp.Paren):
        # `((a.x = b.y))` — peel the parens and recurse.
        inner = expr.this
        if inner is not None:  # pragma: no branch — sqlglot Paren always has an inner expression
            yield from _walk_and_equalities(inner)
    elif isinstance(expr, exp.EQ):
        yield expr
    # Any other shape (OR, NOT, LIKE, function calls, …) yields nothing.


def _column_ref(expr: exp.Expression | None) -> tuple[str, str] | None:
    """Return `(alias_or_table, column)` if `expr` is a simple column ref.

    Returns `None` for:
      - `None` (defensive)
      - subqueries (`(SELECT ...)`)
      - function calls (`COALESCE(...)`)
      - literals
      - column refs without a table qualifier (`x` instead of `a.x`)

    The qualifier-required rule means `WHERE a.x = x` is not extractable
    as a join — that's typically a filter, not a join, and treating
    unqualified columns as joined would surface false positives on real
    workloads.
    """
    if not isinstance(expr, exp.Column):
        return None
    column_name = expr.name
    table_qualifier = expr.table  # the `a` in `a.col`; "" when unqualified
    if not column_name or not table_qualifier:
        return None
    return table_qualifier, column_name


def aggregate_join_predicates(
    extracted_with_count: Iterable[tuple[ExtractedJoin, int]],
) -> list[AggregatedJoin]:
    """Sum `observation_count` across statements producing identical joins.

    Two `ExtractedJoin` values compare equal iff their `(schema_a,
    table_a, schema_b, table_b, pairs)` tuples match — the direction
    normalisation in `_make_extracted` ensures `a→b` and `b→a` of the
    same join aggregate together.

    Output is sorted descending by `frequency`, then alphabetically by
    `(schema_a, table_a, schema_b, table_b)` for deterministic
    candidate ordering downstream.
    """
    totals: dict[ExtractedJoin, int] = defaultdict(int)
    for extracted, count in extracted_with_count:
        totals[extracted] += count
    aggregated = [AggregatedJoin(join=join, frequency=freq) for join, freq in totals.items()]
    aggregated.sort(
        key=lambda a: (
            -a.frequency,
            a.join.schema_a,
            a.join.table_a,
            a.join.schema_b,
            a.join.table_b,
        )
    )
    return aggregated


def confidence_for_frequency(frequency: int) -> Literal["medium", "low"]:
    """Categorical confidence label for a mining-derived join.

    Threshold lives at module scope (`_MEDIUM_CONFIDENCE_FREQUENCY`) so
    the constant is recompile-time tunable. FK-derived candidates get
    `"high"` confidence in the suggest pipeline and never call through
    here — this is the query-log-only path, which never returns
    `"high"`. The return type narrows to `Literal["medium", "low"]`
    to reflect that; the suggest pipeline's `JoinConfidence` is a
    supertype so assignment is straight-through without `type: ignore`.
    """
    if frequency >= _MEDIUM_CONFIDENCE_FREQUENCY:
        return "medium"
    return "low"
