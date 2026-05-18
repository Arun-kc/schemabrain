"""Integration tests for PostgresProfiler against a real Postgres."""

from __future__ import annotations

import pytest

from schemabrain.connectors.errors import TableNotFoundError
from schemabrain.connectors.postgres import PostgresDataSource
from schemabrain.core.models import Column, Table
from schemabrain.profiler.postgres import PostgresProfiler

pytestmark = pytest.mark.integration


def _make_table(schema: str, name: str, column_specs: list[tuple[str, str]]) -> Table:
    """Build a minimal `Table` whose columns the profiler can iterate over.

    `column_specs` is a list of (name, data_type) pairs in declaration order.
    Other Column fields take innocuous defaults — the profiler only consults
    `name`, `table_name`, `schema_name`.
    """
    columns = tuple(
        Column(
            name=col_name,
            table_name=name,
            schema_name=schema,
            data_type=dtype,
            nullable=True,
            ordinal_position=i + 1,
        )
        for i, (col_name, dtype) in enumerate(column_specs)
    )
    return Table(name=name, schema_name=schema, columns=columns)


class TestProfileTablePopulated:
    def test_id_column_no_nulls(self, profiling_pg_url: str) -> None:
        table = _make_table(
            "profiling",
            "users_profile",
            [
                ("id", "BIGINT"),
                ("email", "TEXT"),
                ("middle_name", "TEXT"),
                ("phone", "TEXT"),
                ("bio", "TEXT"),
            ],
        )
        with PostgresProfiler(profiling_pg_url) as profiler:
            stats = profiler.profile_table(table)
        id_stats = stats["id"]
        assert id_stats.total_rows == 6
        assert id_stats.null_count == 0
        assert id_stats.distinct_count == 6
        assert id_stats.null_pct == 0.0

    def test_email_column_pii_redacted_in_samples(self, profiling_pg_url: str) -> None:
        table = _make_table("profiling", "users_profile", [("email", "TEXT")])
        with PostgresProfiler(profiling_pg_url) as profiler:
            stats = profiler.profile_table(table)
        email_stats = stats["email"]
        assert email_stats.total_rows == 6
        assert email_stats.null_count == 0
        assert email_stats.distinct_count == 6
        # Every sample value must have been redacted before reaching us.
        for sample in email_stats.sample_values:
            assert "@" not in sample or "<EMAIL>" in sample
            assert "acme.com" not in sample
        # Shape patterns are computed from the raw (truncated, unredacted)
        # samples — so we should see canonical email shapes like
        # `aaaaa@aaaa.aaa`, never literal addresses.
        assert email_stats.shape_patterns
        for shape in email_stats.shape_patterns:
            assert "@" in shape
            assert "acme" not in shape  # raw content must not survive
            assert "alice" not in shape

    def test_all_null_column(self, profiling_pg_url: str) -> None:
        table = _make_table("profiling", "users_profile", [("middle_name", "TEXT")])
        with PostgresProfiler(profiling_pg_url) as profiler:
            stats = profiler.profile_table(table)
        mn_stats = stats["middle_name"]
        assert mn_stats.total_rows == 6
        assert mn_stats.null_count == 6
        assert mn_stats.distinct_count == 0
        assert mn_stats.null_pct == 1.0
        assert mn_stats.sample_values == ()
        assert mn_stats.shape_patterns == ()

    def test_mixed_null_column(self, profiling_pg_url: str) -> None:
        table = _make_table("profiling", "users_profile", [("phone", "TEXT")])
        with PostgresProfiler(profiling_pg_url) as profiler:
            stats = profiler.profile_table(table)
        phone_stats = stats["phone"]
        assert phone_stats.total_rows == 6
        assert phone_stats.null_count == 3
        assert phone_stats.distinct_count == 3
        assert phone_stats.null_pct == 0.5
        # Phone shape signature should appear among top patterns.
        assert "999-999-9999" in phone_stats.shape_patterns

    def test_long_value_truncated_in_samples(self, profiling_pg_url: str) -> None:
        table = _make_table("profiling", "users_profile", [("bio", "TEXT")])
        with PostgresProfiler(profiling_pg_url) as profiler:
            stats = profiler.profile_table(table)
        bio_stats = stats["bio"]
        # All sample values must be <= 50 chars, regardless of source length.
        for sample in bio_stats.sample_values:
            assert len(sample) <= 50

    def test_pii_inside_long_value_is_redacted_not_split(self, profiling_pg_url: str) -> None:
        # Regression: row id=6 has a 1100+ char bio with a CC at offset ~40.
        # The naive `value[:50]` then `redact_pii` order would chop the CC
        # in half and leak its trailing digits into `display_samples`.
        table = _make_table("profiling", "users_profile", [("bio", "TEXT")])
        with PostgresProfiler(profiling_pg_url) as profiler:
            stats = profiler.profile_table(table)
        for sample in stats["bio"].sample_values:
            # Neither the CC nor any partial digit run from it must survive.
            assert "4111111111111111" not in sample
            assert "411111" not in sample
            assert "1111111111111" not in sample

    def test_sample_order_is_deterministic(self, profiling_pg_url: str) -> None:
        # Determinism is required for the content-addressable cache built on
        # top of these stats. Two profiles of an unchanged table must return
        # byte-identical sample tuples.
        table = _make_table(
            "profiling",
            "users_profile",
            [("email", "TEXT"), ("phone", "TEXT")],
        )
        with PostgresProfiler(profiling_pg_url) as profiler:
            first = profiler.profile_table(table)
            second = profiler.profile_table(table)
        assert first["email"].sample_values == second["email"].sample_values
        assert first["email"].shape_patterns == second["email"].shape_patterns
        assert first["phone"].sample_values == second["phone"].sample_values
        # And the order is sorted (since we ORDER BY 1).
        assert first["email"].sample_values == tuple(sorted(first["email"].sample_values))

    def test_returns_one_entry_per_requested_column(self, profiling_pg_url: str) -> None:
        table = _make_table(
            "profiling",
            "users_profile",
            [("id", "BIGINT"), ("email", "TEXT"), ("phone", "TEXT")],
        )
        with PostgresProfiler(profiling_pg_url) as profiler:
            stats = profiler.profile_table(table)
        assert set(stats.keys()) == {"id", "email", "phone"}


class TestProfileTableEdgeCases:
    def test_empty_table(self, profiling_pg_url: str) -> None:
        table = _make_table(
            "profiling",
            "empty_table",
            [("id", "BIGINT"), ("name", "TEXT")],
        )
        with PostgresProfiler(profiling_pg_url) as profiler:
            stats = profiler.profile_table(table)
        for col_stats in stats.values():
            assert col_stats.total_rows == 0
            assert col_stats.null_count == 0
            assert col_stats.distinct_count == 0
            assert col_stats.sample_values == ()
            assert col_stats.shape_patterns == ()

    def test_table_with_no_columns_returns_empty(self, profiling_pg_url: str) -> None:
        table = Table(name="users_profile", schema_name="profiling")
        with PostgresProfiler(profiling_pg_url) as profiler:
            stats = profiler.profile_table(table)
        assert stats == {}

    def test_quoted_identifiers_are_safe(self, profiling_pg_url: str) -> None:
        # Identifier quoting must handle reserved keywords ("select"), spaces
        # in table names ("weird name"), and adversarial characters ("x;DROP").
        # We assert sample contents (not just counts) so a successful injection
        # that *also* dropped or overwrote the table would surface as missing
        # data on the second access.
        table = _make_table(
            "profiling",
            "weird name",
            [("select", "TEXT"), ("x;DROP", "TEXT")],
        )
        with PostgresProfiler(profiling_pg_url) as profiler:
            stats = profiler.profile_table(table)
            # Re-profile the same table — if a DROP had succeeded, this raises.
            stats_again = profiler.profile_table(table)
        assert stats["select"].total_rows == 2
        assert stats["x;DROP"].total_rows == 2
        assert stats["select"].distinct_count == 2
        # Content round-trip — values must reach us intact and sorted.
        assert stats["select"].sample_values == ("a", "c")
        assert stats["x;DROP"].sample_values == ("b", "d")
        assert stats_again["select"].sample_values == ("a", "c")

    def test_percent_in_column_name(self, profiling_pg_url: str) -> None:
        # Regression: psycopg's pyformat paramstyle treats `%` as a
        # parameter marker. Routing identifier-only SQL through `text()`
        # double-escapes any `%` baked into a column name
        # (`%` -> `%%` -> `%%%%`) and Postgres dies in parse. The fix
        # is `conn.exec_driver_sql(sql)` — bypasses SQLAlchemy's
        # parameter handling entirely, appropriate because the
        # profiler SQL has zero bind parameters. Real example:
        # BIRD's California Schools DB has columns like
        # `Percent (%) Eligible Free (K-12)`.
        table = _make_table(
            "profiling",
            "pct_columns",
            [("Win %", "INT"), ("Percent (%) Eligible", "TEXT"), ("no_percent", "INT")],
        )
        with PostgresProfiler(profiling_pg_url) as profiler:
            stats = profiler.profile_table(table)
        # Counts must round-trip — the bug previously raised
        # `psycopg.errors.SyntaxError` before we got here.
        assert stats["Win %"].total_rows == 3
        assert stats["Win %"].null_count == 1
        assert stats["Win %"].distinct_count == 2
        assert stats["Percent (%) Eligible"].total_rows == 3
        assert stats["Percent (%) Eligible"].null_count == 0
        assert stats["Percent (%) Eligible"].distinct_count == 2
        assert stats["no_percent"].total_rows == 3
        # Samples path must also work — same bug exists in `_fetch_samples`.
        assert set(stats["Percent (%) Eligible"].sample_values) == {"yes", "no"}

    def test_unknown_table_raises(self, profiling_pg_url: str) -> None:
        table = _make_table("profiling", "no_such_table", [("id", "BIGINT")])
        with PostgresProfiler(profiling_pg_url) as profiler, pytest.raises(TableNotFoundError):
            profiler.profile_table(table)

    def test_unknown_column_does_not_masquerade_as_missing_table(
        self, profiling_pg_url: str
    ) -> None:
        # A stale Table referencing a dropped/typo'd column triggers Postgres
        # SQLSTATE 42703 (UndefinedColumn). Earlier versions caught all
        # ProgrammingErrors as TableNotFoundError, which would have hidden the
        # real cause. This must propagate as the underlying SQLAlchemy error.
        from sqlalchemy.exc import ProgrammingError

        table = _make_table(
            "profiling",
            "users_profile",
            [("totally_not_a_real_column", "TEXT")],
        )
        with PostgresProfiler(profiling_pg_url) as profiler, pytest.raises(ProgrammingError):
            profiler.profile_table(table)


class TestLifecycle:
    def test_close_is_idempotent(self, profiling_pg_url: str) -> None:
        profiler = PostgresProfiler(profiling_pg_url)
        profiler.close()
        profiler.close()  # second call must not raise

    def test_use_after_close_raises(self, profiling_pg_url: str) -> None:
        profiler = PostgresProfiler(profiling_pg_url)
        profiler.close()
        table = _make_table("profiling", "users_profile", [("id", "BIGINT")])
        with pytest.raises(RuntimeError):
            profiler.profile_table(table)

    def test_context_manager_closes(self, profiling_pg_url: str) -> None:
        with PostgresProfiler(profiling_pg_url) as profiler:
            pass
        # After exit, profiler is closed; further use should fail.
        table = _make_table("profiling", "users_profile", [("id", "BIGINT")])
        with pytest.raises(RuntimeError):
            profiler.profile_table(table)


class TestSampleSize:
    def test_default_sample_size_caps_at_five(self, profiling_pg_url: str) -> None:
        # users_profile has 5 distinct emails — sample should include all.
        table = _make_table("profiling", "users_profile", [("email", "TEXT")])
        with PostgresProfiler(profiling_pg_url) as profiler:
            stats = profiler.profile_table(table)
        assert len(stats["email"].sample_values) <= 5

    def test_custom_sample_size(self, profiling_pg_url: str) -> None:
        table = _make_table("profiling", "users_profile", [("email", "TEXT")])
        with PostgresProfiler(profiling_pg_url, sample_size=2) as profiler:
            stats = profiler.profile_table(table)
        assert len(stats["email"].sample_values) <= 2


class TestConstruction:
    def test_rejects_zero_sample_size(self, profiling_pg_url: str) -> None:
        with pytest.raises(ValueError, match="sample_size must be >= 1"):
            PostgresProfiler(profiling_pg_url, sample_size=0)

    def test_rejects_negative_sample_size(self, profiling_pg_url: str) -> None:
        with pytest.raises(ValueError, match="sample_size must be >= 1"):
            PostgresProfiler(profiling_pg_url, sample_size=-3)


class TestSharedDataSource:
    """The profiler is intended to run after the connector. Confirm they
    coexist on the same Postgres without stepping on each other."""

    def test_profiler_and_connector_can_share_database(self, profiling_pg_url: str) -> None:
        with (
            PostgresDataSource(profiling_pg_url) as ds,
            PostgresProfiler(profiling_pg_url) as profiler,
        ):
            table = ds.get_table("users_profile", "profiling")
            stats = profiler.profile_table(table)
        # Sanity: Table introspected has the columns we expect, and the
        # profiler returned stats for each.
        assert {c.name for c in table.columns} == set(stats.keys())


class TestNoEqualityColumnTypes:
    """Regression: pre-v0.3.1 the profiler emitted `COUNT(DISTINCT col)`
    for every column, so any schema with `xml`, `tsvector`, geometric, or
    other no-equality types crashed init with
    `psycopg.errors.UndefinedFunction: could not identify an equality
    operator for type xml`. Surfaced 2026-05-18 against AdventureWorks-
    for-Postgres (humanresources.jobcandidate.resume + 6 others).
    """

    def test_xml_column_does_not_crash_profile(self, profiling_pg_url: str) -> None:
        # `xml` has no equality operator — the pre-fix profiler crashed
        # mid-`profile_table`. Post-fix: profiles cleanly, distinct count
        # falls back to non_null (max-cardinality assumption).
        table = _make_table(
            "profiling",
            "no_equality_types",
            [("id", "BIGINT"), ("doc", "XML"), ("loc", "POINT")],
        )
        with PostgresProfiler(profiling_pg_url) as profiler:
            stats = profiler.profile_table(table)
        # All three columns must come back.
        assert set(stats.keys()) == {"id", "doc", "loc"}
        # Equality-supporting BIGINT id: real distinct count.
        assert stats["id"].total_rows == 3
        assert stats["id"].null_count == 0
        assert stats["id"].distinct_count == 3
        # xml column: 2 non-null rows, NULL::bigint emitted instead of
        # DISTINCT → distinct_count == non_null (max-cardinality hedge).
        assert stats["doc"].total_rows == 3
        assert stats["doc"].null_count == 1
        assert stats["doc"].distinct_count == 2  # == non_null fallback
        # point column: same path as xml.
        assert stats["loc"].total_rows == 3
        assert stats["loc"].null_count == 1
        assert stats["loc"].distinct_count == 2

    def test_supports_distinct_returns_true_for_equality_types(self) -> None:
        # Sanity that the equality-type allow-list logic doesn't flip the
        # answer for ordinary types.
        from schemabrain.profiler.postgres import _supports_distinct

        for data_type in ("BIGINT", "TEXT", "VARCHAR(255)", "NUMERIC(5, 2)", "TIMESTAMP"):
            assert _supports_distinct(data_type), f"{data_type} should support DISTINCT"

    def test_supports_distinct_returns_false_for_no_equality_types(self) -> None:
        from schemabrain.profiler.postgres import _supports_distinct

        for data_type in ("xml", "XML", "tsvector", "point", "line", "polygon"):
            assert not _supports_distinct(data_type), f"{data_type} should not support DISTINCT"

    def test_supports_distinct_strips_parameterised_suffix(self) -> None:
        # Some Postgres types carry a precision/length suffix —
        # `character varying(255)` strips to `character varying`, etc.
        # Confirm the fix is robust to it.
        from schemabrain.profiler.postgres import _supports_distinct

        assert _supports_distinct("character varying(255)")
        assert _supports_distinct("numeric(10, 2)")
        # No-equality types don't carry parameterised suffixes in practice,
        # but the function should handle them defensively.
        assert not _supports_distinct("xml(unused)")
