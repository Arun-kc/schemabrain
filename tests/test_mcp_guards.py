"""Tests for `schemabrain.mcp._guards.forbid_sql`.

This module is built pre-emptively for the future SQL-execution
boundary (v2 `execute` / `validate_query`). It is NOT wired into the
current MCP tool surface — no tool today accepts a string that flows
toward a SQL engine. The guard exists so the moment that boundary
lands, defense-in-depth is already in place.

Tagging discipline: every check line in the module is annotated
`# DEFENSE-IN-DEPTH ONLY — never the primary control`. The PRIMARY
controls at the future execution boundary will be: typed
`CompiledQuery` tokens, single-statement parameterised execution,
and a read-only role on the database. This guard is *belt-and-braces*
— if any of the primaries slip, the guard buys one more refusal
before SQL ever reaches Postgres.

These tests pin both the happy path (innocent strings pass) and the
known-bad shapes from the public Datadog Security Labs case study on
the archived Postgres MCP server.
"""

from __future__ import annotations

import pytest

from schemabrain.mcp._guards import ForbiddenSQLError, forbid_sql


class TestAcceptsInnocentStrings:
    """Strings that look like normal agent inputs pass cleanly."""

    @pytest.mark.parametrize(
        "good",
        [
            "orders",
            "public.users",
            "public.orders.user_id",
            "total_revenue",
            "How many customers placed an order this month?",  # NL question
            "monthly_active_users",
            "",  # empty string is not by itself dangerous
            "customer_lifetime_value",
            "country = USA",  # parameter-ish; no SQL constructs
        ],
    )
    def test_accepts(self, good: str) -> None:
        forbid_sql(good)  # must not raise


class TestRejectsStatementTerminators:
    """`;` is the Datadog V2 vector — statement-stacking."""

    def test_rejects_semicolon(self) -> None:
        with pytest.raises(ForbiddenSQLError):
            forbid_sql("SELECT 1; DROP TABLE users")

    def test_rejects_trailing_semicolon(self) -> None:
        with pytest.raises(ForbiddenSQLError):
            forbid_sql("orders;")


class TestRejectsSqlComments:
    """`--` and `/* */` are classic SQL-comment-out attacks."""

    def test_rejects_line_comment(self) -> None:
        with pytest.raises(ForbiddenSQLError):
            forbid_sql("orders -- AND 1=1")

    def test_rejects_block_comment_open(self) -> None:
        with pytest.raises(ForbiddenSQLError):
            forbid_sql("orders /* comment */")

    def test_rejects_block_comment_close_only(self) -> None:
        # An attacker who knows the input is wrapped in a comment can
        # close it with `*/`.
        with pytest.raises(ForbiddenSQLError):
            forbid_sql("evil */ AND 1=1")


class TestRejectsQuotes:
    """Any single-quote in input is suspicious — agents shouldn't be
    composing SQL literals here."""

    def test_rejects_single_quote(self) -> None:
        with pytest.raises(ForbiddenSQLError):
            forbid_sql("orders' OR '1'='1")

    def test_rejects_balanced_quotes(self) -> None:
        with pytest.raises(ForbiddenSQLError):
            forbid_sql("'admin'")


class TestRejectsTransactionKeywords:
    """`BEGIN` / `COMMIT` / `ROLLBACK` / `SAVEPOINT` / `SET` are the
    Datadog V3 vector — escaping the read-only transaction wrapper."""

    @pytest.mark.parametrize(
        "kw",
        ["BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "SET"],
    )
    def test_rejects_keyword_uppercase(self, kw: str) -> None:
        with pytest.raises(ForbiddenSQLError):
            forbid_sql(f"foo {kw} bar")

    @pytest.mark.parametrize(
        "kw",
        ["begin", "commit", "rollback", "savepoint", "set"],
    )
    def test_rejects_keyword_lowercase(self, kw: str) -> None:
        # Case-insensitive matching is mandatory; Postgres accepts any
        # case for keywords.
        with pytest.raises(ForbiddenSQLError):
            forbid_sql(f"foo {kw} bar")

    def test_rejects_mixed_case(self) -> None:
        with pytest.raises(ForbiddenSQLError):
            forbid_sql("CoMmIt;")


class TestRejectsDdlKeywords:
    """DDL keywords are the Datadog V8 vector — schema mutation under
    an agent-controlled string."""

    @pytest.mark.parametrize(
        "kw",
        ["DROP", "ALTER", "CREATE", "TRUNCATE", "GRANT", "REVOKE"],
    )
    def test_rejects_ddl_uppercase(self, kw: str) -> None:
        with pytest.raises(ForbiddenSQLError):
            forbid_sql(f"foo {kw} bar")

    def test_rejects_ddl_lowercase(self) -> None:
        with pytest.raises(ForbiddenSQLError):
            forbid_sql("drop table users")


class TestKeywordIsWordBoundary:
    """Keyword check uses word boundaries so `set` rejects but
    `subset` doesn't (false-positive guard).

    This trade-off matters because the guard is defense-in-depth, NOT
    the primary check. Over-broad rejection (false positives on
    innocent column names containing keyword substrings) would hurt
    UX more than the (already-mitigated) attack it adds defense against.
    """

    @pytest.mark.parametrize(
        "innocent",
        [
            "subset_id",  # contains "set"
            "creator",  # contains "create"
            "altered_at",  # contains "alter"
            "regrants_log",  # contains "grant"
            "truncation_factor",  # contains "truncate"
            "rollback_table",  # contains "rollback" — but we accept it
        ],
    )
    def test_substring_does_not_trigger(self, innocent: str) -> None:
        # `rollback_table` is the edge case: literally contains
        # "rollback". The contract here is that the guard treats
        # underscored identifiers as a single token — the user named
        # a table after a concept, not issued a ROLLBACK.
        forbid_sql(innocent)  # must not raise

    def test_keyword_as_standalone_word_does_trigger(self) -> None:
        # Same shape as `rollback_table` above but with a space — the
        # keyword stands alone now and rejection fires.
        with pytest.raises(ForbiddenSQLError):
            forbid_sql("ROLLBACK transaction")


class TestErrorMessage:
    """The raised error names which check fired so reviewers and
    operators can debug rejection without rerunning a verbose log.
    Per the v2-defense-in-depth-tagged-line discipline, the message
    must point at the FORBIDDEN construct, not the input."""

    def test_error_names_the_construct(self) -> None:
        with pytest.raises(ForbiddenSQLError, match="semicolon"):
            forbid_sql("orders;")

    def test_error_does_not_echo_unbounded_input(self) -> None:
        attack = "x" * 100_000 + ";"
        with pytest.raises(ForbiddenSQLError) as exc_info:
            forbid_sql(attack)
        assert len(str(exc_info.value)) < 2_000


class TestForbiddenSqlErrorIsValueError:
    """Callers may catch `ValueError` generically. `ForbiddenSQLError`
    is a `ValueError` subclass so existing catch sites in the eventual
    v2 executor pick it up without a separate import."""

    def test_isinstance_value_error(self) -> None:
        with pytest.raises(ValueError):
            forbid_sql("orders;")
