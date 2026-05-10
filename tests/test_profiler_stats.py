"""Tests for the pure profiling primitives: ColumnStats, PII redaction, shapes."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from schemabrain.profiler.stats import (
    ColumnStats,
    redact_pii,
    shape_of,
    top_shapes,
)


class TestColumnStats:
    def test_null_pct_zero_when_total_rows_zero(self) -> None:
        stats = ColumnStats(
            column_name="email",
            total_rows=0,
            null_count=0,
            distinct_count=0,
        )
        assert stats.null_pct == 0.0

    def test_null_pct_computed_from_counts(self) -> None:
        stats = ColumnStats(
            column_name="email",
            total_rows=100,
            null_count=25,
            distinct_count=50,
        )
        assert stats.null_pct == 0.25

    def test_null_pct_all_null(self) -> None:
        stats = ColumnStats(
            column_name="middle_name",
            total_rows=10,
            null_count=10,
            distinct_count=0,
        )
        assert stats.null_pct == 1.0

    def test_is_frozen(self) -> None:
        stats = ColumnStats(
            column_name="email",
            total_rows=10,
            null_count=0,
            distinct_count=10,
        )
        with pytest.raises(ValidationError):
            stats.total_rows = 99  # type: ignore[misc]

    def test_negative_total_rows_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ColumnStats(
                column_name="email",
                total_rows=-1,
                null_count=0,
                distinct_count=0,
            )

    def test_negative_null_count_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ColumnStats(
                column_name="email",
                total_rows=10,
                null_count=-1,
                distinct_count=0,
            )

    def test_negative_distinct_count_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ColumnStats(
                column_name="email",
                total_rows=10,
                null_count=0,
                distinct_count=-1,
            )

    def test_null_count_cannot_exceed_total_rows(self) -> None:
        with pytest.raises(ValidationError):
            ColumnStats(
                column_name="email",
                total_rows=5,
                null_count=10,
                distinct_count=0,
            )

    def test_distinct_count_cannot_exceed_non_null_rows(self) -> None:
        with pytest.raises(ValidationError):
            ColumnStats(
                column_name="email",
                total_rows=10,
                null_count=8,
                distinct_count=5,
            )

    def test_empty_column_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ColumnStats(
                column_name="",
                total_rows=0,
                null_count=0,
                distinct_count=0,
            )

    def test_default_samples_and_shapes_empty(self) -> None:
        stats = ColumnStats(
            column_name="email",
            total_rows=0,
            null_count=0,
            distinct_count=0,
        )
        assert stats.sample_values == ()
        assert stats.shape_patterns == ()


class TestRedactPii:
    def test_redacts_email(self) -> None:
        assert redact_pii("contact: alice@example.com please") == "contact: <EMAIL> please"

    def test_redacts_multiple_emails(self) -> None:
        result = redact_pii("a@x.io and b@y.io")
        assert result == "<EMAIL> and <EMAIL>"

    def test_redacts_ssn(self) -> None:
        assert redact_pii("SSN 123-45-6789 here") == "SSN <SSN> here"

    def test_does_not_redact_partial_ssn(self) -> None:
        # Bare 5-digit zip-like string must not be flagged as SSN.
        assert redact_pii("zip 12345") == "zip 12345"

    def test_redacts_credit_card_16_digit(self) -> None:
        assert redact_pii("card 4111111111111111 ok") == "card <CC> ok"

    def test_redacts_credit_card_with_spaces(self) -> None:
        assert redact_pii("card 4111 1111 1111 1111") == "card <CC>"

    def test_redacts_credit_card_with_dashes(self) -> None:
        assert redact_pii("card 4111-1111-1111-1111") == "card <CC>"

    def test_does_not_redact_short_digit_run(self) -> None:
        # 12 digits is below the CC range and should pass through.
        assert redact_pii("id 123456789012") == "id 123456789012"

    def test_combines_patterns_in_one_pass(self) -> None:
        text = "email me at alice@x.com or 4111111111111111 ssn 111-22-3333"
        result = redact_pii(text)
        assert "<EMAIL>" in result
        assert "<CC>" in result
        assert "<SSN>" in result
        assert "alice@x.com" not in result
        assert "4111111111111111" not in result
        assert "111-22-3333" not in result

    def test_passes_through_clean_string(self) -> None:
        assert redact_pii("Acme Corp HQ") == "Acme Corp HQ"

    def test_empty_string(self) -> None:
        assert redact_pii("") == ""

    def test_unicode_digits_do_not_trigger_cc_redaction(self) -> None:
        # Arabic-Indic digits would match Python's default Unicode-aware `\d`.
        # We use ASCII-only patterns so legitimate non-ASCII text passes through
        # unchanged rather than being silently redacted as a credit card.
        arabic_digits = "١٢٣٤٥٦٧٨٩٠١٢٣٤٥٦"  # 16 Arabic-Indic digits
        assert redact_pii(arabic_digits) == arabic_digits

    def test_thirteen_digit_id_is_redacted_intentionally(self) -> None:
        # Locked-in trade-off: any 13-19 digit run is treated as CC-like.
        # A 13-digit barcode or order ID will be redacted. Removing this
        # behavior would create a PII under-redaction risk and requires
        # an explicit policy change.
        assert redact_pii("order 1234567890123 fulfilled") == "order <CC> fulfilled"

    def test_all_digit_uuid_segments_are_redacted_intentionally(self) -> None:
        # An all-numeric UUID (no hex letters) gets multiple matches and
        # ends up partially redacted. This is the same over-redaction
        # trade-off — under-redaction would be unsafe.
        result = redact_pii("00000000-0000-0000-0000-000000000000")
        # We don't assert the exact form (it depends on regex greediness),
        # only that the original digit run does not survive intact.
        assert "00000000-0000-0000-0000-000000000000" not in result
        assert "<CC>" in result

    def test_unicode_word_chars_do_not_trigger_email_redaction(self) -> None:
        # Without `re.ASCII`, `\w` matches Unicode letters and `naïve@é.fr` would
        # be redacted. With ASCII-only patterns, the leading `naï` breaks the
        # email match so non-ASCII addresses are NOT silently redacted.
        # (We accept this trade-off for v0; revisit when targeting i18n schemas.)
        assert redact_pii("naïve@é.fr") != "<EMAIL>"


class TestShapeOf:
    def test_digits_become_nine(self) -> None:
        assert shape_of("12345") == "99999"

    def test_lowercase_become_a(self) -> None:
        assert shape_of("hello") == "aaaaa"

    def test_uppercase_become_A(self) -> None:
        assert shape_of("HELLO") == "AAAAA"

    def test_mixed_case_and_digits(self) -> None:
        assert shape_of("Abc123") == "Aaa999"

    def test_preserves_separators(self) -> None:
        assert shape_of("abc-123") == "aaa-999"
        assert shape_of("a@b.io") == "a@a.aa"

    def test_truncates_at_fifty_chars(self) -> None:
        long = "a" * 100
        result = shape_of(long)
        assert len(result) == 50
        assert result == "a" * 50

    def test_empty_string(self) -> None:
        assert shape_of("") == ""

    def test_unicode_letter_treated_as_other(self) -> None:
        # We classify with ASCII heuristics; non-ASCII letters fall through as literals.
        # This is a deliberate v0 choice; revisit once we test against non-English schemas.
        assert shape_of("naïve") == "aaïaa"


class TestTopShapes:
    def test_returns_most_common_first(self) -> None:
        values = ["abc", "def", "ghi", "123"]
        result = top_shapes(values, limit=2)
        assert result == ("aaa", "999")

    def test_limit_caps_results(self) -> None:
        values = ["a", "1", "A", "."]
        result = top_shapes(values, limit=2)
        assert len(result) == 2

    def test_returns_empty_tuple_for_no_values(self) -> None:
        assert top_shapes([], limit=3) == ()

    def test_default_limit_is_three(self) -> None:
        values = ["a", "1", "A", "."]
        result = top_shapes(values)
        assert len(result) == 3

    def test_dedupes_identical_shapes(self) -> None:
        values = ["abc", "def", "ghi"]  # all shape "aaa"
        result = top_shapes(values, limit=5)
        assert result == ("aaa",)

    def test_ties_broken_lexicographically(self) -> None:
        # Each underlying shape appears exactly once. shape_of("a")="a",
        # shape_of("B")="A", shape_of("1")="9". Sorted by lex on the shapes,
        # ASCII '9' (57) < 'A' (65) < 'a' (97). The output must be the same
        # regardless of input order so cache fingerprints stay stable.
        forward = top_shapes(["a", "B", "1"])
        reverse = top_shapes(["1", "B", "a"])
        assert forward == reverse == ("9", "A", "a")
