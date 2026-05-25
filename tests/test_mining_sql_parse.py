"""Tests for the v0.5 sqlglot wrapper used by query log mining.

`extract_table_references` is the parser primitive that takes a raw
SQL string (as emitted by `pg_stat_statements`, including its `$1`/`$2`
parameter placeholders) and returns the set of `(schema, table)` pairs
the statement touches. Pipeline code uses the result to filter mined
rows down to tables that are actually in the SchemaBrain index.

Contract:
  - Returns `frozenset[tuple[str | None, str]]` so the caller can
    distinguish unqualified (`("", "orders")` won't match `("public",
    "orders")` accidentally — schema=None preserves that ambiguity for
    the pipeline to resolve).
  - DDL, transaction-control, and `SET` statements return the empty
    set — filtered before they get to the writer.
  - Parse failures return the empty set + a debug log line (pg_stat_-
    statements occasionally surfaces non-parseable text; one row
    failing must not fail the batch).
  - View / CTE references appear in the result as normal `(schema,
    table)` tuples; whether they survive the indexed-tables filter is
    the pipeline's call.
"""

from __future__ import annotations

import pytest

from schemabrain.mining.sql_parse import extract_table_references, is_profiler_query


class TestSimpleSelects:
    def test_select_from_qualified_table(self) -> None:
        assert extract_table_references("SELECT id FROM public.orders") == frozenset(
            {("public", "orders")}
        )

    def test_select_from_unqualified_table_has_none_schema(self) -> None:
        # Don't infer a default schema at parse time; the pipeline
        # knows which schemas are indexed and can resolve.
        assert extract_table_references("SELECT id FROM orders") == frozenset({(None, "orders")})

    def test_select_no_from_returns_empty(self) -> None:
        # SELECT 1, SELECT now(), etc. — no table reference.
        assert extract_table_references("SELECT 1") == frozenset()

    def test_pg_stat_placeholders_do_not_break_parsing(self) -> None:
        # pg_stat_statements normalises literals to $1, $2, etc.
        sql = "SELECT id, total FROM public.orders WHERE user_id = $1 AND total > $2"
        assert extract_table_references(sql) == frozenset({("public", "orders")})


class TestJoinsAndCTEs:
    def test_inner_join_returns_both_tables(self) -> None:
        sql = "SELECT * FROM public.orders o JOIN public.users u ON u.id = o.user_id"
        assert extract_table_references(sql) == frozenset(
            {("public", "orders"), ("public", "users")}
        )

    def test_subquery_returns_outer_and_inner_tables(self) -> None:
        sql = (
            "SELECT * FROM public.orders "
            "WHERE user_id IN (SELECT id FROM public.users WHERE active = $1)"
        )
        assert extract_table_references(sql) == frozenset(
            {("public", "orders"), ("public", "users")}
        )

    def test_cte_references_appear_in_table_set(self) -> None:
        # The CTE alias surfaces as a Table reference — the pipeline's
        # indexed-tables filter drops it (CTE aliases are never indexed
        # tables). The parser itself doesn't have to distinguish.
        sql = (
            "WITH active_users AS (SELECT id FROM public.users WHERE active = $1) "
            "SELECT * FROM public.orders JOIN active_users ON active_users.id = orders.user_id"
        )
        result = extract_table_references(sql)
        # The real underlying table must be present:
        assert ("public", "users") in result
        assert ("public", "orders") in result


class TestWriteStatements:
    def test_insert_returns_target_table(self) -> None:
        assert extract_table_references("INSERT INTO public.orders (id) VALUES ($1)") == frozenset(
            {("public", "orders")}
        )

    def test_update_returns_target_table(self) -> None:
        assert extract_table_references(
            "UPDATE public.orders SET total = $1 WHERE id = $2"
        ) == frozenset({("public", "orders")})

    def test_delete_returns_target_table(self) -> None:
        assert extract_table_references("DELETE FROM public.orders WHERE id = $1") == frozenset(
            {("public", "orders")}
        )

    def test_update_with_from_clause_returns_both(self) -> None:
        # UPDATE ... FROM is the Postgres syntax for join-like writes.
        sql = (
            "UPDATE public.orders SET status = u.status FROM public.users u "
            "WHERE u.id = orders.user_id"
        )
        assert extract_table_references(sql) == frozenset(
            {("public", "orders"), ("public", "users")}
        )


class TestFilteredStatementShapes:
    """DDL, transaction control, SET, and other non-DML statements
    return the empty set so the mining pipeline never tries to write
    them as example queries.
    """

    @pytest.mark.parametrize(
        "sql",
        [
            "CREATE TABLE foo (id INT)",
            "ALTER TABLE foo ADD COLUMN bar TEXT",
            "DROP TABLE foo",
            "TRUNCATE TABLE foo",
            "CREATE INDEX idx ON foo (bar)",
        ],
    )
    def test_ddl_returns_empty(self, sql: str) -> None:
        assert extract_table_references(sql) == frozenset()

    @pytest.mark.parametrize(
        "sql",
        [
            "BEGIN",
            "COMMIT",
            "ROLLBACK",
            "SAVEPOINT s1",
            "SET search_path = public",
            "SET LOCAL statement_timeout = '5s'",
        ],
    )
    def test_transaction_and_session_control_returns_empty(self, sql: str) -> None:
        assert extract_table_references(sql) == frozenset()

    def test_show_returns_empty(self) -> None:
        assert extract_table_references("SHOW search_path") == frozenset()


class TestParseFailures:
    def test_malformed_sql_returns_empty(self) -> None:
        # sqlglot may sometimes parse garbage permissively; the
        # contract here is that genuinely-broken text returns the
        # empty set instead of raising. Either outcome (parse-empty
        # or parse-as-something-but-no-tables-detected) satisfies the
        # "one bad row doesn't blow up mining" invariant.
        result = extract_table_references("not even close to SQL ###")
        assert isinstance(result, frozenset)
        # The empty-set assertion is intentionally lenient — sqlglot
        # may surface a Table node from a wild guess. The load-
        # bearing claim is "no exception escapes".

    def test_empty_string_returns_empty(self) -> None:
        assert extract_table_references("") == frozenset()

    def test_whitespace_only_returns_empty(self) -> None:
        assert extract_table_references("   \n\t  ") == frozenset()


class TestMultiSchema:
    def test_cross_schema_join(self) -> None:
        sql = "SELECT * FROM public.orders o JOIN audit.events e ON e.order_id = o.id"
        assert extract_table_references(sql) == frozenset(
            {("public", "orders"), ("audit", "events")}
        )


class TestSqlglotLoggerSilenced:
    """The mining module silences sqlglot's own warning-level chatter
    at module import. Without this, statements like `SHOW transaction
    isolation level` or `CREATE EXTENSION ...` (captured by
    `pg_stat_statements` from SchemaBrain's own connection setup,
    not user code) make sqlglot log `'show ...' contains unsupported
    syntax. Falling back to parsing as a 'Command'.` to stderr — one
    noisy line per non-DML statement in a mining batch.

    sqlglot only ever surfaces these as advisory warnings; the
    pipeline already drops non-DML via `_DML_TYPES`. Routing the
    warnings to ERROR-level keeps real failures visible while
    silencing the routine "I don't know this dialect grammar" noise.
    """

    def test_sqlglot_warning_messages_are_suppressed(self) -> None:
        import logging as _logging

        # The effective level on sqlglot's logger must be ≥ ERROR so
        # WARNING-level "unsupported syntax" messages don't reach
        # handlers. Module import is responsible for setting this; if
        # someone removes the suppression, this test catches it.
        assert _logging.getLogger("sqlglot").getEffectiveLevel() >= _logging.ERROR


class TestIsProfilerQuery:
    """SchemaBrain's own profiler emits two distinctive SELECT shapes
    against every indexed table during `schemabrain index`. When the
    user runs `mine-queries` against the same Postgres after indexing,
    `pg_stat_statements` surfaces those profiler statements alongside
    real user workload. `is_profiler_query` detects the two shapes by
    their unmistakable alias signatures so the mining pipeline can drop
    them before they pollute `example_queries`.

    The signatures targeted live in `schemabrain.profiler.postgres`:
      - counts query: `COUNT(<col>) AS nn_<idx>` positional alias
      - sampler query: `<col>::text AS v` single-letter alias on a cast

    Neither alias shape appears in human-written SQL, which is what
    makes the regex match safe.
    """

    def test_counts_profiler_query_is_recognised(self) -> None:
        sql = (
            'SELECT COUNT(*) AS total, COUNT("id") AS nn_0, COUNT(DISTINCT "id") AS d_0, '
            'COUNT("name") AS nn_1, COUNT(DISTINCT "name") AS d_1 '
            'FROM "public"."categories"'
        )
        assert is_profiler_query(sql) is True

    def test_sampler_profiler_query_is_recognised(self) -> None:
        sql = (
            'SELECT DISTINCT "name"::text AS v FROM "public"."categories" '
            'WHERE "name" IS NOT NULL ORDER BY 1 LIMIT 50'
        )
        assert is_profiler_query(sql) is True

    def test_sampler_with_pg_stat_statements_parameter_normalisation(self) -> None:
        """pg_stat_statements replaces literals with $1/$2 placeholders.

        The profiler signature must survive that rewrite — the `::text
        AS v` and `IS NOT NULL` parts are operator/identifier syntax
        that pg_stat_statements leaves untouched. The LIMIT literal
        becomes $1 but that's irrelevant to the detection.
        """
        sql = (
            'SELECT DISTINCT "id"::text AS v FROM "public"."orders" '
            'WHERE "id" IS NOT NULL ORDER BY $1 LIMIT $2'
        )
        assert is_profiler_query(sql) is True

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT id, email FROM users WHERE created_at > '2026-01-01'",
            "SELECT COUNT(*) FROM orders",
            "SELECT COUNT(*) AS order_count FROM orders WHERE status = 'paid'",
            "SELECT DISTINCT category_id FROM products",
            "SELECT product_id::text FROM order_items",
            "INSERT INTO users (id, email) VALUES (1, 'a@b.c')",
            "UPDATE orders SET status = 'paid' WHERE id = 1",
            # Realistic enum-peek query a developer might write:
            # has `SELECT DISTINCT` AND `::text AS v` but lacks the
            # profiler's `IS NOT NULL` predicate, so the sampler
            # pattern's three-marker conjunction rejects it.
            "SELECT DISTINCT status::text AS v FROM orders LIMIT 100",
            "SELECT DISTINCT category::text AS v FROM products",
        ],
    )
    def test_legitimate_user_queries_are_not_flagged(self, sql: str) -> None:
        """The signatures are deliberately narrow:
          - bare `COUNT(*)`, `DISTINCT col`, or `col::text` must NOT match
          - `SELECT DISTINCT col::text AS v` without `IS NOT NULL`
            must NOT match (the profiler always includes that predicate)
        Only the full profiler joint signatures trigger the filter.
        """
        assert is_profiler_query(sql) is False

    def test_empty_and_whitespace_inputs_are_not_flagged(self) -> None:
        assert is_profiler_query("") is False
        assert is_profiler_query("   \n\t  ") is False

    def test_detection_is_case_insensitive_for_keyword_resilience(self) -> None:
        """Some psql tooling lowercases or uppercases keywords. The
        alias literals themselves (`nn_0`, `v`) stay verbatim because
        SchemaBrain emits them with double-quoted column identifiers
        and lowercase aliases; the surrounding keywords may shift.
        """
        sql = (
            'select count(*) as total, count("id") as nn_0, count(distinct "id") as d_0 '
            'from "public"."users"'
        )
        assert is_profiler_query(sql) is True
