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
    ("patient_id", frozenset({"health"})),
    ("patient_name", frozenset({"health", "contact"})),
    ("insurance_id", frozenset({"health"})),
    ("health_record_id", frozenset({"health"})),
    # genetic
    ("genome", frozenset({"genetic"})),
    ("genotype", frozenset({"genetic"})),
    ("dna_seq", frozenset({"genetic"})),
    ("rsid", frozenset({"genetic"})),
    # biometric
    ("fingerprint", frozenset({"biometric"})),
    ("face_id", frozenset({"biometric"})),
    ("face_hash", frozenset({"biometric"})),
    ("face_embedding", frozenset({"biometric"})),
    ("face_print", frozenset({"biometric"})),
    ("face_vector", frozenset({"biometric"})),
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
    ("drivers_license", frozenset({"government_id"})),
    ("driver_licence", frozenset({"government_id"})),
    ("drivers_license_number", frozenset({"government_id"})),
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
    # DOB and birthdate variants live here (post-S3) — HIPAA Safe
    # Harbor identifier + GDPR Article 9 demographic attribute.
    ("dob", frozenset({"demographic_protected"})),
    ("date_of_birth", frozenset({"demographic_protected"})),
    ("birth_date", frozenset({"demographic_protected"})),
    ("birthdate", frozenset({"demographic_protected"})),
    # `age` is a HIPAA Safe Harbor demographic attribute.
    ("age", frozenset({"demographic_protected"})),
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
        #
        # Post-S1-S4 (2026-05-18) accounting:
        #   - DOB rule moved from contact to demographic_protected.
        #     It was always a standalone tuple, so the migration is
        #     net 0.
        #   - New `patient`/`insurance_id`/`health_record` health rule
        #     added: net +1.
        #   - Other extensions (face_embedding, drivers_license
        #     plurals, age) widened existing rules rather than adding
        #     new ones: net 0.
        # Total: 40 + 1 = 41.
        assert RULE_COUNT == 41

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


class TestS1NonPiiNameDenylist:
    """Suppression of the `_name`/`name` rule for non-PII `<noun>_name`
    shapes — the bug surfaced in the 2026-05-18 production-DB smoke
    when `product_name` in catalog tables tagged as `pii (contact)`."""

    @pytest.mark.parametrize(
        "name",
        [
            "product_name",
            "brand_name",
            "category_name",
            "language_name",
            "currency_name",
            "device_name",
            "file_name",
            "service_name",
            "tag_name",
            "event_name",
        ],
    )
    def test_denylist_name_shapes_classify_as_public(self, name: str) -> None:
        sensitivity, cats = classify_column(name)
        assert sensitivity == "public", f"{name!r} should not classify"
        assert cats == frozenset()

    @pytest.mark.parametrize(
        "name",
        [
            # These shapes can be a person's attribute in HR/CRM
            # contexts (employer, team affiliation, geographic region).
            # The denylist deliberately excludes them — they keep their
            # `contact` tag rather than risk under-reporting.
            "company_name",
            "organization_name",
            "team_name",
            "department_name",
            "region_name",
            "zone_name",
        ],
    )
    def test_ambiguous_name_shapes_keep_contact_tag(self, name: str) -> None:
        sensitivity, cats = classify_column(name)
        assert sensitivity == "pii"
        assert "contact" in cats

    @pytest.mark.parametrize(
        ("name", "expected_cat"),
        [
            # People-noun `_name` shapes — still classify.
            ("customer_name", "contact"),
            ("user_name", "contact"),
            ("display_name", "contact"),
            ("legal_name", "contact"),
            ("contact_name", "contact"),
            ("first_name", "contact"),
            ("last_name", "contact"),
            ("full_name", "contact"),
        ],
    )
    def test_pii_named_shapes_still_classify(self, name: str, expected_cat: PIICategory) -> None:
        sensitivity, cats = classify_column(name)
        assert sensitivity == "pii"
        assert expected_cat in cats

    def test_denylist_case_insensitive(self) -> None:
        # Postgres canonicalises identifiers to lowercase but mixed-
        # case names appear in MS SQL / camelCase migrations.
        assert classify_column("Product_Name") == ("public", frozenset())
        assert classify_column("PRODUCT_NAME") == ("public", frozenset())

    def test_known_limitation_bare_name_still_classifies(self) -> None:
        # `category.name` / `language.name` (bare `name` in a lookup
        # table) cannot be disambiguated from `customers.name` without
        # table-level context. The bare-name case remains an
        # intentional over-tag at v1; this test documents the gap so
        # a future fix (operator-asserted classification, value
        # sampling) has a natural failing-test home.
        sensitivity, cats = classify_column("name")
        assert sensitivity == "pii"
        assert "contact" in cats

    def test_other_rules_still_fire_on_denylist_shapes(self) -> None:
        # The denylist only suppresses the `_name` rule. A column like
        # `country_name` still matches the `country` rule (contact)
        # via the independent city/state/country rule. Documenting the
        # narrow scope of the S1 fix.
        sensitivity, cats = classify_column("country_name")
        assert sensitivity == "pii"
        assert "contact" in cats


class TestS2IntegerFkGuard:
    """`<token>_id` integer FKs only carry FK-safe categories — the
    bug surfaced when `address_id BIGINT` tagged as `pii (contact)`
    via the `address` keyword match."""

    @pytest.mark.parametrize(
        "ctype",
        ["INTEGER", "BIGINT", "SMALLINT", "BIGSERIAL", "int4", "int8", "int"],
    )
    def test_address_id_integer_suppressed(self, ctype: str) -> None:
        # `address_id` matches `address` (contact) without the guard.
        # With integer type + FK shape, contact is stripped.
        sensitivity, cats = classify_column("address_id", column_type=ctype)
        assert sensitivity == "public"
        assert cats == frozenset()

    def test_address_id_text_not_suppressed(self) -> None:
        # The guard ONLY fires for integer types. A TEXT column named
        # `address_id` could legitimately store address-shaped data
        # (rare but possible) and stays tagged.
        sensitivity, cats = classify_column("address_id", column_type="TEXT")
        assert sensitivity == "pii"
        assert "contact" in cats

    def test_default_column_type_does_not_suppress(self) -> None:
        # Backwards-compat: callers that pass no `column_type` get the
        # v0.3.0 behaviour (no guard).
        sensitivity, cats = classify_column("address_id")
        assert sensitivity == "pii"
        assert "contact" in cats

    @pytest.mark.parametrize(
        ("name", "expected_cat"),
        [
            # FK-safe categories survive the guard.
            ("patient_id", "health"),
            ("session_id", "credential"),
            ("session_id", "online_identifier"),
            ("cookie_id", "online_identifier"),
            ("device_id", "online_identifier"),
            ("national_id", "government_id"),
        ],
    )
    def test_fk_safe_categories_survive_integer_guard(
        self, name: str, expected_cat: PIICategory
    ) -> None:
        sensitivity, cats = classify_column(name, column_type="BIGINT")
        assert sensitivity == "pii"
        assert expected_cat in cats

    def test_parameterised_integer_type_recognised(self) -> None:
        # MySQL emits `BIGINT(20)` / `INT(11)`. The guard strips the
        # parameter suffix before the integer-type lookup.
        sensitivity, _ = classify_column("address_id", column_type="BIGINT(20)")
        assert sensitivity == "public"
        sensitivity, _ = classify_column("address_id", column_type="INT(11)")
        assert sensitivity == "public"

    def test_integer_array_type_recognised(self) -> None:
        # `int4[]` (Postgres integer array) — guard still applies.
        sensitivity, _ = classify_column("address_id", column_type="int4[]")
        assert sensitivity == "public"

    @pytest.mark.parametrize(
        "ctype",
        [
            "INT UNSIGNED",
            "BIGINT UNSIGNED",
            "INTEGER UNSIGNED",
            "TINYINT UNSIGNED ZEROFILL",
            "BIGINT(20) UNSIGNED",
        ],
    )
    def test_mysql_unsigned_modifier_recognised(self, ctype: str) -> None:
        # MySQL emits `INT UNSIGNED` / `BIGINT UNSIGNED` / etc. The
        # guard must split on whitespace and take the first token
        # so the modifier doesn't bypass it.
        sensitivity, _ = classify_column("address_id", column_type=ctype)
        assert sensitivity == "public", f"{ctype!r} should fire the guard"

    def test_numeric_type_does_not_trigger_guard(self) -> None:
        # `NUMERIC(10,2)` (Postgres) / `NUMBER(5,2)` (Oracle) are
        # decimal types whose FK semantics are too distinct from
        # integer FKs. Skip the guard so `address_id NUMERIC(10,2)`
        # falls back to over-tagging consistent with the breadth-
        # over-precision posture.
        sensitivity, _ = classify_column("address_id", column_type="NUMERIC(10,2)")
        assert sensitivity == "pii"

    def test_primary_key_skips_guard(self) -> None:
        # `health_record_id BIGINT PRIMARY KEY` on the `health_records`
        # table itself is the canonical row identifier, not an FK.
        # The PK exemption preserves the `health` category that would
        # otherwise be the only PII tag on the row.
        sensitivity, cats = classify_column(
            "health_record_id",
            column_type="BIGINT",
            is_primary_key=True,
        )
        assert sensitivity == "pii"
        assert "health" in cats

    def test_non_primary_key_default_still_guards(self) -> None:
        # Default `is_primary_key=False` keeps the guard firing for
        # FK shapes. Document the safe-direction default.
        sensitivity, _ = classify_column("address_id", column_type="BIGINT")
        assert sensitivity == "public"

    @pytest.mark.parametrize(
        ("name", "expected_cat"),
        [
            # `behavioral` and `location` are now FK-safe so the
            # FK's primary tag survives the guard.
            ("clickstream_id", "behavioral"),
            ("event_log_id", "behavioral"),
            ("location_id", "location"),
            ("geolocation_id", "location"),
        ],
    )
    def test_behavioral_and_location_survive_guard(
        self, name: str, expected_cat: PIICategory
    ) -> None:
        sensitivity, cats = classify_column(name, column_type="BIGINT")
        assert sensitivity == "pii"
        assert expected_cat in cats

    def test_bare_id_not_subject_to_guard(self) -> None:
        # `id` alone is not the FK shape — `^[a-z][a-z0-9]*(?:_[a-z0-9]+)*_id$`
        # requires a prefix segment. `id` doesn't match any PII rule
        # in the first place, but verify it's not silently changing.
        assert classify_column("id", column_type="BIGINT") == ("public", frozenset())

    def test_fk_shape_with_no_pii_match_stays_public(self) -> None:
        # `customer_id BIGINT` — `customer` is not a PII keyword;
        # `customer_name` is. The guard is a no-op when there's
        # nothing to suppress.
        assert classify_column("customer_id", column_type="BIGINT") == (
            "public",
            frozenset(),
        )


class TestS3DobInDemographicProtected:
    """DOB and birthdate variants tag as `demographic_protected`, NOT
    `contact` — HIPAA Safe Harbor + GDPR Article 9."""

    @pytest.mark.parametrize("name", ["dob", "date_of_birth", "birth_date", "birthdate"])
    def test_dob_variant_classifies_to_demographic_protected(self, name: str) -> None:
        sensitivity, cats = classify_column(name)
        assert sensitivity == "pii"
        assert "demographic_protected" in cats
        # And critically, NOT contact — this is what the smoke flagged.
        assert "contact" not in cats


class TestS4AddedRules:
    """Rules added in response to the 2026-05-18 smoke false-negatives."""

    @pytest.mark.parametrize(
        ("name", "expected_cat"),
        [
            ("drivers_license", "government_id"),
            ("drivers_license_number", "government_id"),
            ("driver_licence", "government_id"),  # British spelling
            ("face_embedding", "biometric"),
            ("face_print", "biometric"),
            ("face_vector", "biometric"),
            ("patient_id", "health"),
            ("insurance_id", "health"),
            ("health_record_number", "health"),
            ("age", "demographic_protected"),
        ],
    )
    def test_added_rule_classifies_column(self, name: str, expected_cat: PIICategory) -> None:
        sensitivity, cats = classify_column(name)
        assert sensitivity == "pii"
        assert expected_cat in cats


class TestColumnTypeBackwardsCompat:
    def test_omitting_column_type_matches_v0_3_0_behaviour(self) -> None:
        # Anyone still calling classify_column with just the name —
        # the signature is backwards-compat. New parameter is keyword
        # with default None.
        a = classify_column("email")
        b = classify_column("email", column_type=None)
        c = classify_column("email", column_type="TEXT")
        assert a == b == c

    def test_unknown_column_type_does_not_trigger_guard(self) -> None:
        # `column_type="UNKNOWN"` shouldn't crash and shouldn't
        # trigger the integer-FK guard.
        sensitivity, cats = classify_column("address_id", column_type="WIDGET")
        assert sensitivity == "pii"
        assert "contact" in cats
