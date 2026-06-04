"""Tests for the deterministic index-time PII-confidence classifier.

The contract is documented in `docs/adr/0009-trust-surface-confidence-
data-contract.md`. Confidence is advisory matrix metadata, never an
enforcement gate, and is computed from two index-time signals already
collected: the matched categories (the name-regex output) and the
value-shape signatures (`ColumnStats.shape_patterns`).
"""

from __future__ import annotations

import pytest

from schemabrain.pii.confidence import classify_pii_confidence


class TestPublicColumns:
    def test_no_categories_returns_none_band_and_none_score(self) -> None:
        # A public column is not on the PII matrix — no band, no score.
        assert classify_pii_confidence(frozenset()) == (None, None)


class TestFloorLocked:
    @pytest.mark.parametrize(
        "category",
        ["credential", "payment_card", "government_id"],
    )
    def test_catastrophic_floor_category_is_floor_locked(self, category: str) -> None:
        band, score = classify_pii_confidence(frozenset({category}))
        assert band == "floor_locked"
        # `floor_locked` is band-only — the tag is locked regardless of
        # any numeric corroboration, so the score is NULL.
        assert score is None

    def test_floor_dominates_when_mixed_with_non_floor(self) -> None:
        # A column carrying both a floor and a non-floor category locks.
        band, score = classify_pii_confidence(frozenset({"credential", "contact"}))
        assert band == "floor_locked"
        assert score is None

    def test_floor_locked_ignores_shape_patterns(self) -> None:
        band, score = classify_pii_confidence(
            frozenset({"payment_card"}),
            shape_patterns=("9999999999999999",),
        )
        assert band == "floor_locked"
        assert score is None


class TestNonFloorBaseline:
    def test_name_match_without_shapes_is_medium(self) -> None:
        # A bare name-regex match is real evidence (medium), but without
        # value-shape corroboration we do not claim high.
        band, score = classify_pii_confidence(frozenset({"financial"}))
        assert band == "medium"
        assert score == pytest.approx(0.6)

    def test_contact_without_corroborating_shape_is_medium(self) -> None:
        band, score = classify_pii_confidence(
            frozenset({"contact"}),
            shape_patterns=("aaaa",),  # free-text (a name) — no @ or phone shape
        )
        assert band == "medium"
        assert score == pytest.approx(0.6)

    def test_empty_string_shape_does_not_corroborate(self) -> None:
        # A blank value shape (`shape_of("")`) must not be read as a
        # phone/email match — it stays medium, never crashes.
        band, score = classify_pii_confidence(
            frozenset({"contact"}),
            shape_patterns=("",),
        )
        assert band == "medium"
        assert score == pytest.approx(0.6)

    def test_category_without_shape_family_is_always_medium(self) -> None:
        # `financial` has no characteristic value shape — a decimal must
        # NOT corroborate it (only `location` owns the coordinate family).
        band, score = classify_pii_confidence(
            frozenset({"financial"}),
            shape_patterns=("9.99",),
        )
        assert band == "medium"
        assert score == pytest.approx(0.6)


class TestShapeCorroboration:
    def test_email_shape_corroborates_contact_to_high(self) -> None:
        band, score = classify_pii_confidence(
            frozenset({"contact"}),
            shape_patterns=("aaa@aaa.aaa",),
        )
        assert band == "high"
        assert score == pytest.approx(0.9)

    def test_phone_shape_corroborates_contact_to_high(self) -> None:
        band, _ = classify_pii_confidence(
            frozenset({"contact"}),
            shape_patterns=("999-999-9999",),
        )
        assert band == "high"

    def test_coordinate_shape_corroborates_location_to_high(self) -> None:
        band, _ = classify_pii_confidence(
            frozenset({"location"}),
            shape_patterns=("-99.9999",),
        )
        assert band == "high"

    def test_ipv4_shape_corroborates_online_identifier_to_high(self) -> None:
        band, _ = classify_pii_confidence(
            frozenset({"online_identifier"}),
            shape_patterns=("999.999.9.9",),
        )
        assert band == "high"

    def test_uuid_shape_corroborates_online_identifier_to_high(self) -> None:
        # `shape_of` maps hex letters to 'a'; a UUID is 36 chars, 4 dashes.
        band, _ = classify_pii_confidence(
            frozenset({"online_identifier"}),
            shape_patterns=("999a9999-a99a-99a9-a999-999999999999",),
        )
        assert band == "high"

    def test_corroboration_checks_all_shapes_not_just_first(self) -> None:
        # The corroborating shape may not be the most common one.
        band, _ = classify_pii_confidence(
            frozenset({"contact"}),
            shape_patterns=("aaaa", "aaa@aaa.aaa"),
        )
        assert band == "high"

    def test_score_is_clamped_at_one(self) -> None:
        _, score = classify_pii_confidence(
            frozenset({"contact"}),
            shape_patterns=("aaa@aaa.aaa",),
        )
        assert score is not None
        assert score <= 1.0


class TestNeverEmitsLow:
    """v1 withholds `low` — a low PII claim we cannot positively refute
    would under-state real sensitivity, the dangerous direction."""

    @pytest.mark.parametrize(
        "categories",
        [
            frozenset({"contact"}),
            frozenset({"financial"}),
            frozenset({"health"}),
            frozenset({"location"}),
            frozenset({"online_identifier"}),
            frozenset({"behavioral"}),
        ],
    )
    def test_non_floor_never_returns_low(self, categories: frozenset[str]) -> None:
        band, _ = classify_pii_confidence(categories, shape_patterns=("9999",))
        assert band in ("high", "medium")
