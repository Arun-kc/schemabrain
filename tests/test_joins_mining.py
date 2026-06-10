"""Tests for `joins/mining.py` — sqlglot-driven equi-join predicate extraction.

Pins the mining design decisions:

  - Equi-join predicates only — CROSS JOIN, USING, non-equi all yield
    empty contribution
  - Both sides of each equality must be column refs on joined tables
    (subquery LHS, function calls, literals dropped silently)
  - Composite-key joins produce ONE `ExtractedJoin` with N pairs
  - Direction normalisation: `a.x = b.y` and `b.y = a.x` aggregate to
    the same `ExtractedJoin` value
  - AND-chains walked; OR / NOT short-circuit to "no extractable join"
  - Parse failures → empty set (DEBUG-logged, batch survives)
  - Frequency aggregation sums `observation_count` across identical
    `ExtractedJoin` records; output sorted descending by frequency
  - `_MEDIUM_CONFIDENCE_FREQUENCY = 5` separates `"low"` from `"medium"`
"""

from __future__ import annotations

import pytest

from schemabrain.joins.mining import (
    _MEDIUM_CONFIDENCE_FREQUENCY,
    ExtractedJoin,
    aggregate_join_predicates,
    confidence_for_frequency,
    extract_join_predicates,
)

# ----- ExtractedJoin dataclass -----------------------------------------------


class TestExtractedJoin:
    def test_constructs_with_normalised_direction(self) -> None:
        # `(public, addresses)` < `(public, orders)` alphabetically.
        join = ExtractedJoin(
            schema_a="public",
            table_a="addresses",
            schema_b="public",
            table_b="orders",
            pairs=(("id", "billing_address_id"),),
        )
        assert join.schema_a == "public"
        assert join.table_a == "addresses"

    def test_auto_normalises_backwards_direction(self) -> None:
        # Constructor accepts any (schema, table) ordering and
        # auto-normalises in `__post_init__` — swapping sides AND
        # flipping every column pair. The output is direction-canonical
        # regardless of input.
        join = ExtractedJoin(
            schema_a="public",
            table_a="orders",
            schema_b="public",
            table_b="addresses",  # < orders alphabetically
            pairs=(("billing_address_id", "id"),),
        )
        # Sides swapped to canonical (addresses < orders).
        assert join.table_a == "addresses"
        assert join.table_b == "orders"
        # Pair flipped to follow the new direction.
        assert join.pairs == (("id", "billing_address_id"),)

    def test_auto_normalises_already_canonical_direction(self) -> None:
        # Same-direction construction is a no-op on sides but sorts
        # the pair list alphabetically on first element.
        join = ExtractedJoin(
            schema_a="public",
            table_a="orders",
            schema_b="public",
            table_b="users",
            pairs=(("z_col", "z_target"), ("a_col", "a_target")),
        )
        # Pairs sorted alphabetically on first element.
        assert join.pairs[0] == ("a_col", "a_target")
        assert join.pairs[1] == ("z_col", "z_target")

    def test_rejects_empty_pairs(self) -> None:
        with pytest.raises(ValueError, match="at least one column pair"):
            ExtractedJoin(
                schema_a="public",
                table_a="addresses",
                schema_b="public",
                table_b="orders",
                pairs=(),
            )


# ----- extract_join_predicates: happy paths ----------------------------------


class TestExtractHappyPath:
    def test_simple_equi_join(self) -> None:
        sql = "SELECT * FROM public.users u JOIN public.orders o ON u.id = o.user_id"
        result = extract_join_predicates(sql)
        assert len(result) == 1
        extracted = next(iter(result))
        # Normalised direction: orders < users alphabetically.
        assert extracted.table_a == "orders"
        assert extracted.table_b == "users"
        # Pair reoriented to follow the (a, b) order.
        assert extracted.pairs == (("user_id", "id"),)

    def test_direction_normalised_across_equivalent_sql(self) -> None:
        # Same join, written two ways — should extract to the same value.
        a = "SELECT * FROM public.users u JOIN public.orders o ON u.id = o.user_id"
        b = "SELECT * FROM public.orders o JOIN public.users u ON o.user_id = u.id"
        c = "SELECT * FROM public.users u JOIN public.orders o ON o.user_id = u.id"
        result_a = extract_join_predicates(a)
        result_b = extract_join_predicates(b)
        result_c = extract_join_predicates(c)
        assert result_a == result_b == result_c

    def test_multiple_join_clauses_in_one_select(self) -> None:
        sql = (
            "SELECT * FROM public.orders o "
            "JOIN public.users u ON u.id = o.user_id "
            "JOIN public.products p ON p.id = o.product_id"
        )
        result = extract_join_predicates(sql)
        assert len(result) == 2
        # Both joins are direction-normalised; orders is alphabetically
        # before users and products.
        tables = {(j.table_a, j.table_b) for j in result}
        assert tables == {("orders", "users"), ("orders", "products")}

    def test_composite_key_join_one_extracted_join_n_pairs(self) -> None:
        # The canonical junction-table shape.
        sql = (
            "SELECT * FROM public.org_members m "
            "JOIN public.users u ON u.id = m.user_id AND u.org_id = m.org_id"
        )
        result = extract_join_predicates(sql)
        assert len(result) == 1
        extracted = next(iter(result))
        # Two pairs, sorted alphabetically by left col.
        assert len(extracted.pairs) == 2

    def test_alias_resolution_works(self) -> None:
        # The ON clause refers to aliases, not table names. Resolution
        # must map back to the underlying (schema, table).
        sql = "SELECT * FROM public.users x JOIN public.orders y ON x.id = y.user_id"
        result = extract_join_predicates(sql)
        assert len(result) == 1
        extracted = next(iter(result))
        assert extracted.table_a == "orders"
        assert extracted.table_b == "users"

    def test_unqualified_table_resolves_to_empty_schema(self) -> None:
        # No schema prefix — alias_map records schema as "". Mining still
        # extracts the join; the suggest pipeline drops it later when it
        # can't be matched to an indexed table+entity.
        sql = "SELECT * FROM users u JOIN orders o ON u.id = o.user_id"
        result = extract_join_predicates(sql)
        assert len(result) == 1
        extracted = next(iter(result))
        assert extracted.schema_a == ""
        assert extracted.schema_b == ""


class TestSqlglotVersionRegression:
    """Tripwire for the sqlglot major-version drift behind the `<27.0` pin.

    sqlglot 30.x renamed the `exp.Select` arg key from `"from"` to
    `"from_"`. `mining.py:_build_alias_map` reads the FROM-side table via
    `select.args.get("from")`, so on 30.x that lookup returns None: the
    FROM-side table never enters the alias map, every ON predicate that
    references it is dropped, and `extract_join_predicates` returns an
    EMPTY set with no error raised (a silent semantic break, not a parse
    failure).

    This test asserts the FROM-side table is resolved — it passes on the
    pinned 26.x and FAILS on sqlglot 30.x. The lock-free CI smoke job
    runs it against whatever a fresh `pip install` resolves, so widening
    the pin to admit 30.x without porting the AST walk off `args.get(
    "from")` (e.g. to `select.find(exp.From)`) fails loudly here rather
    than silently emptying the join-mining path for end users.
    """

    def test_from_side_table_is_resolved_not_silently_dropped(self) -> None:
        # `users` is the FROM-side table (the one read via the renamed
        # arg key); `orders` is the JOIN-side table. A correct extraction
        # resolves BOTH and yields exactly one join between them.
        sql = "SELECT * FROM public.users u JOIN public.orders o ON u.id = o.user_id"
        result = extract_join_predicates(sql)
        assert result != frozenset(), (
            "join-mining returned an empty set — sqlglot likely renamed the "
            "FROM arg key (30.x: 'from' -> 'from_'); see the pyproject pin"
        )
        extracted = next(iter(result))
        tables = {extracted.table_a, extracted.table_b}
        assert tables == {"orders", "users"}


# ----- extract_join_predicates: skip cases -----------------------------------


class TestExtractSkipCases:
    @pytest.mark.parametrize(
        "sql",
        [
            "",
            "   ",
            "\n\t",
        ],
    )
    def test_empty_or_whitespace_yields_empty_set(self, sql: str) -> None:
        assert extract_join_predicates(sql) == frozenset()

    def test_parse_failure_yields_empty_set(self) -> None:
        # Deliberately malformed SQL — sqlglot raises ParseError.
        # Logged at DEBUG; the batch survives the bad row.
        sql = "SELECT FROM JOIN ON ="
        assert extract_join_predicates(sql) == frozenset()

    def test_non_select_yields_empty_set(self) -> None:
        # UPDATE / DELETE / INSERT may contain joins but v1 scope is
        # SELECT only. Pinning the limit so a future relaxation is
        # a deliberate decision, not a silent expansion.
        sql = "INSERT INTO public.users (id, name) VALUES (1, 'a')"
        assert extract_join_predicates(sql) == frozenset()

    def test_select_with_no_join_yields_empty_set(self) -> None:
        sql = "SELECT * FROM public.users WHERE id = 1"
        assert extract_join_predicates(sql) == frozenset()

    def test_from_less_select_yields_empty_set(self) -> None:
        # `SELECT 1`, `SELECT now()`, and friends appear in
        # `pg_stat_statements` from health-check pings + scalar
        # function calls. They have no FROM clause, so the alias_map
        # is empty and the early-return branch in
        # `extract_join_predicates` fires.
        assert extract_join_predicates("SELECT 1") == frozenset()
        assert extract_join_predicates("SELECT now()") == frozenset()

    def test_cross_join_yields_empty_set(self) -> None:
        # CROSS JOIN has no ON clause → no predicate to extract.
        sql = "SELECT * FROM public.users u CROSS JOIN public.orders o"
        assert extract_join_predicates(sql) == frozenset()

    def test_using_clause_yields_empty_set(self) -> None:
        # USING requires live-column resolution; deferred per design lock.
        sql = "SELECT * FROM public.users JOIN public.orders USING (user_id)"
        assert extract_join_predicates(sql) == frozenset()

    def test_non_equi_predicate_yields_empty_set(self) -> None:
        sql = (
            "SELECT * FROM public.events e "
            "JOIN public.windows w ON e.ts BETWEEN w.start_ts AND w.end_ts"
        )
        assert extract_join_predicates(sql) == frozenset()

    def test_or_predicate_short_circuits(self) -> None:
        # OR in the ON clause means the user's join condition isn't
        # deterministic in the canonical-join sense — we refuse to
        # surface either side as canonical.
        sql = (
            "SELECT * FROM public.users u "
            "JOIN public.orders o ON u.id = o.user_id OR u.email = o.email"
        )
        assert extract_join_predicates(sql) == frozenset()

    def test_function_call_on_lhs_yields_empty_set(self) -> None:
        # `LOWER(u.email) = o.email` — function call on one side,
        # not a simple column ref.
        sql = "SELECT * FROM public.users u JOIN public.orders o ON LOWER(u.email) = o.email"
        assert extract_join_predicates(sql) == frozenset()

    def test_literal_rhs_yields_empty_set(self) -> None:
        # `u.id = 5` is a filter, not a join. The literal is not a
        # column ref so the predicate is dropped.
        sql = "SELECT * FROM public.users u JOIN public.orders o ON u.id = 5"
        assert extract_join_predicates(sql) == frozenset()

    def test_unqualified_column_yields_empty_set(self) -> None:
        # `id = user_id` without a table prefix — not extractable.
        sql = "SELECT * FROM public.users u JOIN public.orders o ON id = user_id"
        assert extract_join_predicates(sql) == frozenset()


# ----- extract_join_predicates: composite predicates -------------------------


class TestExtractCompositePredicates:
    def test_filter_in_and_chain_dropped_join_kept(self) -> None:
        # `u.id = o.user_id AND o.status = 'paid'` — the first is a
        # join predicate, the second is a filter on a literal. The
        # function-call/literal filter is dropped; the join survives.
        sql = (
            "SELECT * FROM public.users u "
            "JOIN public.orders o ON u.id = o.user_id AND o.status = 'paid'"
        )
        result = extract_join_predicates(sql)
        assert len(result) == 1
        extracted = next(iter(result))
        assert len(extracted.pairs) == 1

    def test_mixed_table_pair_in_composite_dropped(self) -> None:
        # `u.id = o.user_id AND o.id = p.order_id` — second predicate
        # introduces a third table; can't form a coherent two-table
        # join. We drop the entire JOIN rather than emit a mixed
        # predicate set.
        sql = (
            "SELECT * FROM public.users u "
            "JOIN public.orders o ON u.id = o.user_id AND o.id = u.user_id"
        )
        # Both predicates are between (users, orders) — composite-PK
        # shape. Should extract one ExtractedJoin with 2 pairs.
        result = extract_join_predicates(sql)
        assert len(result) == 1


# ----- aggregate_join_predicates ---------------------------------------------


def _join_for_test(*, pairs: tuple[tuple[str, str], ...]) -> ExtractedJoin:
    return ExtractedJoin(
        schema_a="public",
        table_a="orders",
        schema_b="public",
        table_b="users",
        pairs=pairs,
    )


class TestAggregateJoinPredicates:
    def test_sums_observation_counts(self) -> None:
        join = _join_for_test(pairs=(("user_id", "id"),))
        aggregated = aggregate_join_predicates([(join, 10), (join, 25), (join, 5)])
        assert len(aggregated) == 1
        assert aggregated[0].join == join
        assert aggregated[0].frequency == 40

    def test_different_joins_aggregate_separately(self) -> None:
        join_a = _join_for_test(pairs=(("user_id", "id"),))
        join_b = ExtractedJoin(
            schema_a="public",
            table_a="orders",
            schema_b="public",
            table_b="products",
            pairs=(("product_id", "id"),),
        )
        aggregated = aggregate_join_predicates([(join_a, 30), (join_b, 10)])
        assert len(aggregated) == 2
        # Descending frequency — A first.
        assert aggregated[0].join == join_a
        assert aggregated[0].frequency == 30
        assert aggregated[1].join == join_b
        assert aggregated[1].frequency == 10

    def test_empty_input_yields_empty_list(self) -> None:
        assert aggregate_join_predicates([]) == []

    def test_sort_order_deterministic_on_frequency_tie(self) -> None:
        # Equal frequency → alphabetical by (schema_a, table_a, …).
        join_a = ExtractedJoin(
            schema_a="public",
            table_a="aardvarks",
            schema_b="public",
            table_b="zebras",
            pairs=(("x", "x"),),
        )
        join_b = ExtractedJoin(
            schema_a="public",
            table_a="bears",
            schema_b="public",
            table_b="yaks",
            pairs=(("x", "x"),),
        )
        aggregated = aggregate_join_predicates([(join_b, 10), (join_a, 10)])
        assert aggregated[0].join == join_a
        assert aggregated[1].join == join_b


# ----- confidence_for_frequency ----------------------------------------------


class TestExtractWithParens:
    def test_parenthesised_predicate_extracted(self) -> None:
        # `((a.x = b.y))` — paren walker peels and extracts the
        # underlying EQ node.
        sql = "SELECT * FROM public.users u JOIN public.orders o ON ((u.id = o.user_id))"
        result = extract_join_predicates(sql)
        assert len(result) == 1


class TestExtractWithSubquery:
    def test_subquery_alias_in_from_drops_predicate(self) -> None:
        # `(SELECT ...) a JOIN real_table o ON a.x = o.y` — `a` is a
        # subquery wrapper, not a Table node, so it never enters the
        # alias map. The predicate gets dropped at the alias-resolution
        # step; the JOIN contributes nothing.
        sql = (
            "SELECT * FROM (SELECT id FROM public.users) a JOIN public.orders o ON a.id = o.user_id"
        )
        result = extract_join_predicates(sql)
        assert result == frozenset()


class TestExtractMixedTablePair:
    def test_predicate_spanning_third_table_drops_whole_join(self) -> None:
        # `users JOIN orders ON u.id = o.user_id AND o.id = p.order_id`
        # — the second predicate references `products` which isn't part
        # of this JOIN clause. Defensive: drop the whole JOIN.
        sql = (
            "SELECT * FROM public.users u "
            "JOIN public.orders o ON u.id = o.user_id AND o.id = p.order_id "
            "JOIN public.products p ON p.id = o.product_id"
        )
        # The first JOIN gets dropped (mixed tables); the second is
        # clean and survives.
        result = extract_join_predicates(sql)
        # Only the (orders, products) join from the second clause.
        assert len(result) == 1
        extracted = next(iter(result))
        assert extracted.table_a == "orders"
        assert extracted.table_b == "products"


class TestAggregatedJoinInvariants:
    def test_negative_frequency_rejected(self) -> None:
        # Aggregator sums non-negative `observation_count` values;
        # negative frequency would silently break confidence ranking.
        join = ExtractedJoin(
            schema_a="public",
            table_a="orders",
            schema_b="public",
            table_b="users",
            pairs=(("user_id", "id"),),
        )
        with pytest.raises(ValueError, match="non-negative"):
            from schemabrain.joins.mining import AggregatedJoin

            AggregatedJoin(join=join, frequency=-1)


class TestConfidenceForFrequency:
    def test_below_threshold_yields_low(self) -> None:
        assert confidence_for_frequency(0) == "low"
        assert confidence_for_frequency(1) == "low"
        assert confidence_for_frequency(_MEDIUM_CONFIDENCE_FREQUENCY - 1) == "low"

    def test_at_threshold_yields_medium(self) -> None:
        assert confidence_for_frequency(_MEDIUM_CONFIDENCE_FREQUENCY) == "medium"

    def test_above_threshold_yields_medium(self) -> None:
        assert confidence_for_frequency(_MEDIUM_CONFIDENCE_FREQUENCY + 100) == "medium"

    def test_threshold_is_five(self) -> None:
        # Pin the actual value — Q3 resolution. Recompile-time
        # tunable; any change should be a deliberate decision.
        assert _MEDIUM_CONFIDENCE_FREQUENCY == 5
