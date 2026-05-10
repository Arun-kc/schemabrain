"""Tests for the cryptic-name detector that drives Haiku/Sonnet routing.

The pipeline sends "clear" column names to cheap Haiku and routes only
the cryptic ones to Sonnet. The heuristic does NOT consult a dictionary
— it works purely on name shape — but it should match the plan's intent
on the canonical examples.
"""

from __future__ import annotations

import pytest

from schemabrain.enrichment.routing import is_cryptic


class TestSimpleNames:
    """0 or 1 underscore: never cryptic — Haiku is the right home."""

    @pytest.mark.parametrize(
        "name",
        [
            "id",
            "sku",
            "name",
            "email",
            "address",
            "createdat",
            "customer",
            "x",
        ],
    )
    def test_zero_underscore_names_are_never_cryptic(self, name: str) -> None:
        assert is_cryptic(name) is False

    @pytest.mark.parametrize(
        "name",
        [
            "user_id",
            "email_address",
            "created_at",
            "total_cents",
            "placed_at",
            "full_name",
            "is_active",
            "first_name",
        ],
    )
    def test_single_underscore_compounds_are_never_cryptic(self, name: str) -> None:
        # Even abbreviated halves (like `id` in `user_id`) still get
        # Haiku because the other half carries enough signal.
        assert is_cryptic(name) is False


class TestCanonicalCrypticExamples:
    """The plan calls out `acct_dim_v3` and `pmt_fct_h` as cryptic. Pin them."""

    def test_acct_dim_v3(self) -> None:
        assert is_cryptic("acct_dim_v3") is True

    def test_pmt_fct_h(self) -> None:
        assert is_cryptic("pmt_fct_h") is True


class TestRecognizableSegmentEscapesCryptic:
    """If ANY segment is long enough to be a real word, trust Haiku."""

    @pytest.mark.parametrize(
        "name",
        [
            "cust_first_nm",  # `first` is 5 chars → recognizable
            "created_at_utc",  # `created` (7) → recognizable
            "is_email_v2",  # `email` (5) → recognizable
            "tbl_orders_log",  # `orders` (6) → recognizable
        ],
    )
    def test_long_segment_makes_name_clear(self, name: str) -> None:
        assert is_cryptic(name) is False


class TestHeavilyAbbreviated:
    """≥2 underscores, no long segment, low average → cryptic."""

    @pytest.mark.parametrize(
        "name",
        [
            "cst_fst_nm",  # 3,3,2 — avg 2.67
            "acc_no_id",  # 3,2,2 — avg 2.33
            "is_b2b_v2",  # 2,3,2 — avg 2.33
            "tx_ts_ms",  # 2,2,2 — avg 2.0
        ],
    )
    def test_short_segment_compounds_are_cryptic(self, name: str) -> None:
        assert is_cryptic(name) is True


class TestEdgeCases:
    def test_empty_name_is_not_cryptic(self) -> None:
        # Defensive — we never expect an empty column name (Pydantic
        # NonEmptyStr blocks them upstream), but the function shouldn't
        # explode if one slips through.
        assert is_cryptic("") is False

    def test_all_underscore_only_segments(self) -> None:
        # `___` splits to ['', '', '', ''] — 4 zero-length segments,
        # avg=0, max=0. Per the rule (avg < 3.5, no segment >=5), this
        # would be classified cryptic. That's fine — a name like this
        # is itself a bug to flag, and Sonnet's reasoning is more
        # likely to produce a usable description than Haiku's.
        assert is_cryptic("___") is True

    def test_high_average_three_segments_not_cryptic(self) -> None:
        # 3 segments where the avg is exactly the boundary. avg=3.5 is
        # NOT cryptic (strict less-than).
        # 'abc_defg_hijk' → 3,4,4 → avg 3.67 → not cryptic.
        assert is_cryptic("abc_defg_hijk") is False

    def test_borderline_average_just_under(self) -> None:
        # 'abc_def_hij' → 3,3,3 → avg 3.0 → cryptic.
        assert is_cryptic("abc_def_hij") is True

    def test_two_segments_with_one_long_is_not_cryptic(self) -> None:
        # 'ab_customer' → 2,8 → max 8 ≥ 5 → not cryptic.
        # (This was already implied by the 1-underscore rule, but pin
        # the path through the multi-segment branch too.)
        assert is_cryptic("ab_customer_xyz") is False

    def test_case_insensitive_treatment(self) -> None:
        # Uppercase columns (rare in Postgres but possible via quoted
        # identifiers) should classify the same as their lowercase form
        # — segment-length is the only signal we're using.
        assert is_cryptic("ACCT_DIM_V3") == is_cryptic("acct_dim_v3")

    def test_two_underscores_three_short_segments_below_average(self) -> None:
        # `pmt_fct_h` is the canonical case; this duplicates it but
        # exercises the path explicitly.
        assert is_cryptic("pmt_fct_h") is True
