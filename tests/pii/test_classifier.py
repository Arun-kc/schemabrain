"""Heuristic classifier tests — per-category positive + negative coverage.

Two top-level goals:
  1. Every PIICategory has at least one column name that classifies to
     it (so the rule table actually exercises the taxonomy).
  2. Names that obviously aren't PII don't get a category.

The parametrize tables below act as a living spec for what we tag.
Adding a category requires a positive example here; adding a rule
requires updating RULE_COUNT in the classifier.
"""

from __future__ import annotations

import pytest

from schemabrain.pii import PII_CATEGORIES, PIICategory, classify_column
from schemabrain.pii.classifier import RULE_COUNT

# (column_name, expected_subset_of_categories)
# We assert *subset* rather than exact equality because patterns are
# permissively broad — `email_address` legitimately matches both the
# `email` rule (contact) and the `address` rule (contact). The subset
# check captures "the right tag is in the result" without locking us
# into over-precise rule wording.
_POSITIVE_CASES: tuple[tuple[str, frozenset[PIICategory]], ...] = (
    # contact
    ("email", frozenset({"contact"})),
    ("email_address", frozenset({"contact"})),
    ("user_email", frozenset({"contact"})),
    ("phone", frozenset({"contact"})),
    ("mobile_number", frozenset({"contact"})),
    ("first_name", frozenset({"contact"})),
    ("last_name", frozenset({"contact"})),
    ("full_name", frozenset({"contact"})),
    ("home_address", frozenset({"contact"})),
    ("address_line_1", frozenset({"contact"})),
    ("street", frozenset({"contact"})),
    ("zip", frozenset({"contact"})),
    ("zip_code", frozenset({"contact"})),
    ("postal_code", frozenset({"contact"})),
    ("city", frozenset({"contact"})),
    ("state", frozenset({"contact"})),
    ("country", frozenset({"contact"})),
    ("name", frozenset({"contact"})),
    ("user_name", frozenset({"contact"})),
    ("customer_name", frozenset({"contact"})),
    ("display_name", frozenset({"contact"})),
    ("legal_name", frozenset({"contact"})),
    ("dob", frozenset({"contact"})),
    ("date_of_birth", frozenset({"contact"})),
    ("birth_date", frozenset({"contact"})),
    # financial
    ("salary", frozenset({"financial"})),
    ("wage", frozenset({"financial"})),
    ("annual_income", frozenset({"financial"})),
    ("balance", frozenset({"financial"})),
    ("revenue", frozenset({"financial"})),
    ("transaction_amount", frozenset({"financial"})),
    ("order_amount", frozenset({"financial"})),
    # payment_card
    ("card_number", frozenset({"payment_card"})),
    ("card_num", frozenset({"payment_card"})),
    ("card_pan", frozenset({"payment_card"})),
    ("pan", frozenset({"payment_card"})),
    ("cvv", frozenset({"payment_card"})),
    ("cvc", frozenset({"payment_card"})),
    ("iban", frozenset({"payment_card"})),
    ("account_number", frozenset({"payment_card"})),
    ("bank_account", frozenset({"payment_card"})),
    ("routing_number", frozenset({"payment_card"})),
    ("sort_code", frozenset({"payment_card"})),
    # health
    ("diagnosis", frozenset({"health"})),
    ("medication", frozenset({"health"})),
    ("treatment", frozenset({"health"})),
    ("icd10", frozenset({"health"})),
    ("icd_10", frozenset({"health"})),
    ("cpt_code", frozenset({"health"})),
    ("phi", frozenset({"health"})),
    ("mrn", frozenset({"health"})),
    ("medical_record", frozenset({"health"})),
    ("allergy", frozenset({"health"})),
    ("blood_type", frozenset({"health"})),
    ("prescription", frozenset({"health"})),
    ("lab_result", frozenset({"health"})),
    ("symptom", frozenset({"health"})),
    ("bmi", frozenset({"health"})),
    ("vital_sign", frozenset({"health"})),
    # genetic
    ("genome", frozenset({"genetic"})),
    ("genotype", frozenset({"genetic"})),
    ("dna_seq", frozenset({"genetic"})),
    ("rsid", frozenset({"genetic"})),
    # biometric
    ("fingerprint", frozenset({"biometric"})),
    ("face_id", frozenset({"biometric"})),
    ("face_hash", frozenset({"biometric"})),
    ("voiceprint", frozenset({"biometric"})),
    ("iris", frozenset({"biometric"})),
    ("biometric_token_hash", frozenset({"biometric"})),
    # behavioral
    ("purchase_history", frozenset({"behavioral"})),
    ("browse_history", frozenset({"behavioral"})),
    ("clickstream", frozenset({"behavioral"})),
    ("event_log", frozenset({"behavioral"})),
    # online_identifier
    ("ip", frozenset({"online_identifier"})),
    ("ip_addr", frozenset({"online_identifier"})),
    ("ip_address", frozenset({"online_identifier"})),
    ("client_ip", frozenset({"online_identifier"})),
    ("remote_ip", frozenset({"online_identifier"})),
    ("src_ip", frozenset({"online_identifier"})),
    ("cookie_id", frozenset({"online_identifier"})),
    ("device_id", frozenset({"online_identifier"})),
    ("idfa", frozenset({"online_identifier"})),
    ("gaid", frozenset({"online_identifier"})),
    ("aid", frozenset({"online_identifier"})),
    ("advertising_aid", frozenset({"online_identifier"})),
    # credential
    ("password", frozenset({"credential"})),
    ("passwd", frozenset({"credential"})),
    ("pwd", frozenset({"credential"})),
    ("api_key", frozenset({"credential"})),
    ("secret", frozenset({"credential"})),
    ("token", frozenset({"credential"})),
    ("refresh_token", frozenset({"credential"})),
    ("access_token", frozenset({"credential"})),
    ("session_id", frozenset({"credential"})),
    ("pass_hash", frozenset({"credential"})),
    ("pw_hash", frozenset({"credential"})),
    # government_id
    ("ssn", frozenset({"government_id"})),
    ("tin", frozenset({"government_id"})),
    ("nino", frozenset({"government_id"})),
    ("passport", frozenset({"government_id"})),
    ("driver_license", frozenset({"government_id"})),
    ("dl_number", frozenset({"government_id"})),
    ("tax_id", frozenset({"government_id"})),
    ("national_id", frozenset({"government_id"})),
    ("aadhar", frozenset({"government_id"})),
    ("aadhaar", frozenset({"government_id"})),
    ("voter_id", frozenset({"government_id"})),
    ("ein", frozenset({"government_id"})),
    # location
    ("lat", frozenset({"location"})),
    ("latitude", frozenset({"location"})),
    ("lon", frozenset({"location"})),
    ("lng", frozenset({"location"})),
    ("longitude", frozenset({"location"})),
    ("geolocation", frozenset({"location"})),
    ("gps", frozenset({"location"})),
    ("coords", frozenset({"location"})),
    ("coord", frozenset({"location"})),
    # demographic_protected
    ("race", frozenset({"demographic_protected"})),
    ("ethnicity", frozenset({"demographic_protected"})),
    ("religion", frozenset({"demographic_protected"})),
    ("sexual_orientation", frozenset({"demographic_protected"})),
    ("political_party", frozenset({"demographic_protected"})),
    ("political_affiliation", frozenset({"demographic_protected"})),
    ("trade_union", frozenset({"demographic_protected"})),
    ("gender", frozenset({"demographic_protected"})),
    ("sex", frozenset({"demographic_protected"})),
    ("disability", frozenset({"demographic_protected"})),
    ("marital_status", frozenset({"demographic_protected"})),
    ("nationality", frozenset({"demographic_protected"})),
    ("veteran_status", frozenset({"demographic_protected"})),
)


# Names that should NOT match any rule. Watch for accidental
# over-broad patterns here — `id` alone would otherwise match the
# `aid` rule unless we use `\b`.
_NEGATIVE_CASES: tuple[str, ...] = (
    "id",
    "uuid",
    "row_id",
    "created_at",
    "updated_at",
    "deleted_at",
    "is_active",
    "status",
    "version",
    "sku",
    "quantity",
    "rank",
    "score",
    "title",
    "summary",
    "category",
    "tag",
    "comment",
    "description",
    "url",
    "domain",
)


@pytest.mark.parametrize(("name", "expected_subset"), _POSITIVE_CASES)
def test_positive_case_classifies_to_expected_category(
    name: str, expected_subset: frozenset[PIICategory]
) -> None:
    sensitivity, categories = classify_column(name)
    assert sensitivity == "pii"
    assert expected_subset.issubset(categories), (
        f"{name!r} → got {sorted(categories)}, expected superset of {sorted(expected_subset)}"
    )


@pytest.mark.parametrize("name", _NEGATIVE_CASES)
def test_negative_case_does_not_classify(name: str) -> None:
    sensitivity, categories = classify_column(name)
    assert sensitivity == "public"
    assert categories == frozenset()


class TestSensitivityDerivation:
    def test_no_match_yields_public(self) -> None:
        assert classify_column("widget_count") == ("public", frozenset())

    def test_any_match_yields_pii(self) -> None:
        sensitivity, _ = classify_column("email")
        assert sensitivity == "pii"


class TestMultiCategoryUnion:
    def test_payment_token_matches_credential_only(self) -> None:
        # `payment` alone is not a payment_card keyword (the rule uses
        # `card_*` / `pan` / `cvv` / `iban`), so `payment_token` only
        # picks up `credential` via `token`. Documenting this so a
        # future refinement that adds a `payment` keyword has a
        # natural failing test to flip.
        _, cats = classify_column("payment_token")
        assert "credential" in cats

    def test_email_password_matches_contact_and_credential(self) -> None:
        # Exercises the load-bearing set-union behaviour: two
        # underscore-separated segments each match an independent
        # rule, and the result carries both categories.
        _, cats = classify_column("email_password")
        assert "contact" in cats
        assert "credential" in cats


class TestCaseInsensitive:
    @pytest.mark.parametrize(
        "name",
        ["EMAIL", "Email", "eMaIl", "USER_EMAIL", "User_Email"],
    )
    def test_case_variants_match(self, name: str) -> None:
        sensitivity, cats = classify_column(name)
        assert sensitivity == "pii"
        assert "contact" in cats


class TestShapePatternsParameter:
    def test_shape_patterns_ignored_at_v1(self) -> None:
        # v1 ignores shape_patterns — same output regardless of value.
        # When shape-based heuristics land later, this test gets a
        # specific positive case alongside.
        with_shapes = classify_column("email", shape_patterns=("aaaaa@aaa.aaa",))
        without = classify_column("email")
        assert with_shapes == without


class TestRuleTableInvariants:
    def test_rule_count_pinned(self) -> None:
        # If you add or remove a rule, update RULE_COUNT in the
        # classifier and bump this assertion alongside. Pinning the
        # count makes accidental rule churn visible at PR review time.
        assert RULE_COUNT == 40

    def test_every_category_has_at_least_one_rule(self) -> None:
        # Every category in PII_CATEGORIES must be producible by at
        # least one positive case in this file.
        produced: set[PIICategory] = set()
        for name, _ in _POSITIVE_CASES:
            _, cats = classify_column(name)
            produced.update(cats)
        missing = PII_CATEGORIES - produced
        assert not missing, f"categories with no positive coverage: {sorted(missing)}"


class TestEmptyAndUnusualInputs:
    def test_empty_string_is_public(self) -> None:
        assert classify_column("") == ("public", frozenset())

    def test_only_special_chars_is_public(self) -> None:
        # Identifiers can't actually start with these in Postgres, but
        # we should still degrade gracefully.
        assert classify_column("___") == ("public", frozenset())
        assert classify_column("123") == ("public", frozenset())

    def test_long_name_is_handled(self) -> None:
        # 200-char name with `email` embedded. Should still match.
        name = "a" * 100 + "_email_" + "b" * 100
        sensitivity, cats = classify_column(name)
        assert sensitivity == "pii"
        assert "contact" in cats
