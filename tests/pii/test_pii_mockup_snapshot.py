"""Regression snapshot test for the synthetic PII mockup.

Pins the desired `(column → (sensitivity, categories))` mapping for
every column in `schemabrain/eval/fixtures/pii_mockup.sql`. The
mockup encodes the four bug shapes the 2026-05-18 production-DB smoke
surfaced (S1-S4 in `docs/internal/manual_smoke_2026_05_18.md`):

  S1  `<noun>_name` in non-PII tables — `product_name`, `brand_name`,
      `category_name`, `language_name` must classify as `public`.
  S2  `<token>_id` INTEGER FKs — `address_id BIGINT` must classify
      as `public` (the `address` keyword match is suppressed by the
      integer-FK guard).
  S3  `date_of_birth` / `birthdate` must classify as
      `pii (demographic_protected)`, not `pii (contact)`.
  S4  False negatives — `drivers_license`, `face_embedding`,
      `insurance_id`, `age`, `patient_id` must all classify.

This snapshot is the authoritative pin: any future rule change that
silently regresses against these findings fails CI before merge. When
the desired mapping legitimately needs to change, edit this test
deliberately alongside the rule change so the diff is visible in
review.

The mapping is checked against the classifier's output for every
column whose declaration we parse out of the SQL. Columns that should
NOT match any rule are explicitly listed with `("public", frozenset())`
— silence in the expected map means "we forgot to think about it,"
which a strict comparison would let through.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

from schemabrain.pii import PIICategory, Sensitivity, classify_column

FIXTURE_PATH: Final[Path] = (
    Path(__file__).resolve().parent.parent.parent
    / "schemabrain"
    / "eval"
    / "fixtures"
    / "pii_mockup.sql"
)


# Desired mapping: every column in the fixture, with the
# `(sensitivity, frozenset(categories), column_type)` we want the
# classifier to produce. `column_type` is the source-of-truth
# data type the indexer would hand `classify_column`; for the S2
# integer-FK guard the type is load-bearing.
_DESIRED: Final[dict[str, tuple[Sensitivity, frozenset[PIICategory], str]]] = {
    # ----- users -----
    "users.id": ("public", frozenset(), "BIGSERIAL"),
    "users.email": ("pii", frozenset({"contact"}), "TEXT"),
    "users.phone": ("pii", frozenset({"contact"}), "TEXT"),
    "users.full_name": ("pii", frozenset({"contact"}), "TEXT"),
    "users.home_address": ("pii", frozenset({"contact"}), "TEXT"),
    "users.city": ("pii", frozenset({"contact"}), "TEXT"),
    "users.state": ("pii", frozenset({"contact"}), "TEXT"),
    "users.country": ("pii", frozenset({"contact"}), "TEXT"),
    "users.zip_code": ("pii", frozenset({"contact"}), "TEXT"),
    # S3 — DOB to demographic_protected, NOT contact.
    "users.date_of_birth": ("pii", frozenset({"demographic_protected"}), "DATE"),
    # S4 — `age` joins the demographic_protected rule.
    "users.age": ("pii", frozenset({"demographic_protected"}), "INTEGER"),
    "users.gender": ("pii", frozenset({"demographic_protected"}), "TEXT"),
    "users.nationality": ("pii", frozenset({"demographic_protected"}), "TEXT"),
    "users.ssn": ("pii", frozenset({"government_id"}), "TEXT"),
    "users.tax_id": ("pii", frozenset({"government_id"}), "TEXT"),
    "users.passport_number": ("pii", frozenset({"government_id"}), "TEXT"),
    # S4 — `drivers_license` (plural) added to government_id.
    "users.drivers_license": ("pii", frozenset({"government_id"}), "TEXT"),
    "users.fingerprint_hash": ("pii", frozenset({"biometric"}), "TEXT"),
    # S4 — `face_embedding` added to biometric rule.
    "users.face_embedding": ("pii", frozenset({"biometric"}), "BYTEA"),
    "users.last_known_latitude": ("pii", frozenset({"location"}), "REAL"),
    "users.last_known_longitude": ("pii", frozenset({"location"}), "REAL"),
    "users.created_at": ("public", frozenset(), "TIMESTAMPTZ"),
    "users.is_active": ("public", frozenset(), "BOOLEAN"),
    # ----- payments -----
    "payments.id": ("public", frozenset(), "BIGSERIAL"),
    # `user_id` has no PII keyword match today and stays public.
    "payments.user_id": ("public", frozenset(), "BIGINT"),
    "payments.amount_cents": ("pii", frozenset({"financial"}), "BIGINT"),
    "payments.credit_card_number": ("pii", frozenset({"payment_card"}), "TEXT"),
    "payments.iban": ("pii", frozenset({"payment_card"}), "TEXT"),
    "payments.bank_account_number": ("pii", frozenset({"payment_card"}), "TEXT"),
    "payments.routing_number": ("pii", frozenset({"payment_card"}), "TEXT"),
    # S2 — BIGINT FK whose name contains `address`. The integer-FK
    # guard strips `contact` because it's not FK-safe.
    "payments.address_id": ("public", frozenset(), "BIGINT"),
    "payments.created_at": ("public", frozenset(), "TIMESTAMPTZ"),
    # ----- health_records -----
    "health_records.id": ("public", frozenset(), "BIGSERIAL"),
    # S4 — `patient_id` classifies as health. After S2 the integer-FK
    # guard preserves `health` because it's FK-safe.
    "health_records.patient_id": ("pii", frozenset({"health"}), "BIGINT"),
    # S4 — `insurance_id` classifies as health.
    "health_records.insurance_id": ("pii", frozenset({"health"}), "TEXT"),
    "health_records.diagnosis_code": ("pii", frozenset({"health"}), "TEXT"),
    "health_records.medication_list": ("pii", frozenset({"health"}), "TEXT"),
    "health_records.blood_type": ("pii", frozenset({"health"}), "TEXT"),
    "health_records.allergy": ("pii", frozenset({"health"}), "TEXT"),
    "health_records.genome_seq": ("pii", frozenset({"genetic"}), "TEXT"),
    "health_records.purchase_history": ("pii", frozenset({"behavioral"}), "TEXT"),
    # Documented over-tag: `patient_satisfaction_score` matches `patient`
    # → health. Snapshot pins this as the intended behaviour (the
    # docstring posture: over-tag is safer than under-tag at v1).
    "health_records.patient_satisfaction_score": (
        "pii",
        frozenset({"health"}),
        "INTEGER",
    ),
    "health_records.visit_date": ("public", frozenset(), "DATE"),
    # ----- auth_sessions -----
    "auth_sessions.id": ("public", frozenset(), "BIGSERIAL"),
    "auth_sessions.user_id": ("public", frozenset(), "BIGINT"),
    "auth_sessions.session_token": ("pii", frozenset({"credential"}), "TEXT"),
    "auth_sessions.api_key": ("pii", frozenset({"credential"}), "TEXT"),
    "auth_sessions.refresh_token": ("pii", frozenset({"credential"}), "TEXT"),
    "auth_sessions.password_hash": ("pii", frozenset({"credential"}), "TEXT"),
    # `session_id` is online_identifier (FK-safe) — survives S2 guard.
    "auth_sessions.session_id": (
        "pii",
        frozenset({"credential", "online_identifier"}),
        "BIGINT",
    ),
    "auth_sessions.cookie_id": ("pii", frozenset({"online_identifier"}), "TEXT"),
    "auth_sessions.device_id": ("pii", frozenset({"online_identifier"}), "TEXT"),
    # Documented over-tag: `ip_address` matches `address` (contact) AND
    # `ip_address` (online_identifier). Consistent with the classifier's
    # breadth-over-precision posture (`network_address` → contact is
    # explicitly called out in the rule-table docstring).
    "auth_sessions.ip_address": (
        "pii",
        frozenset({"contact", "online_identifier"}),
        "TEXT",
    ),
    "auth_sessions.created_at": ("public", frozenset(), "TIMESTAMPTZ"),
    "auth_sessions.expires_at": ("public", frozenset(), "TIMESTAMPTZ"),
    # ----- non_pii_things (S1 surface) -----
    "non_pii_things.id": ("public", frozenset(), "BIGSERIAL"),
    # S1 — `<denylist>_name` shapes all classify as public after the
    # denylist guard suppresses the `_name` rule match.
    "non_pii_things.product_name": ("public", frozenset(), "TEXT"),
    "non_pii_things.brand_name": ("public", frozenset(), "TEXT"),
    "non_pii_things.category_name": ("public", frozenset(), "TEXT"),
    "non_pii_things.language_name": ("public", frozenset(), "TEXT"),
    "non_pii_things.sku": ("public", frozenset(), "TEXT"),
    "non_pii_things.quantity": ("public", frozenset(), "INTEGER"),
    "non_pii_things.price_cents": ("public", frozenset(), "INTEGER"),
    "non_pii_things.description": ("public", frozenset(), "TEXT"),
    "non_pii_things.is_active": ("public", frozenset(), "BOOLEAN"),
    "non_pii_things.created_at": ("public", frozenset(), "TIMESTAMPTZ"),
}


# ----- Fixture parser ------------------------------------------------
# Pulls `(table, column, type)` triples out of the fixture SQL. Tight
# parser by design — the fixture is hand-authored and stable; a full
# SQL parser would be overkill.

_TABLE_RE: Final[re.Pattern[str]] = re.compile(
    r"CREATE TABLE IF NOT EXISTS\s+public\.(\w+)\s*\((.*?)\);",
    re.DOTALL | re.IGNORECASE,
)

# Match `<name> <TYPE>` where TYPE is the FIRST alphanumeric token after
# the name (we don't care about NULL/NOT NULL/REFERENCES/DEFAULT — the
# classifier only needs the bare type word for the S2 integer-FK guard).
_COLUMN_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*([a-z_][a-z0-9_]*)\s+([A-Z][A-Z0-9_]*)",
    re.IGNORECASE | re.MULTILINE,
)

# Lines beginning these tokens are constraint clauses, not columns.
_CONSTRAINT_PREFIXES: Final[tuple[str, ...]] = (
    "primary",
    "foreign",
    "unique",
    "check",
    "constraint",
)


def _parse_columns(sql: str) -> list[tuple[str, str, str]]:
    """Yield `(table, column, type)` for every column in the fixture."""
    out: list[tuple[str, str, str]] = []
    for table_match in _TABLE_RE.finditer(sql):
        table = table_match.group(1)
        body = table_match.group(2)
        # Strip block comments so a `--`-line inside the CREATE TABLE
        # body doesn't fake a column match.
        body_clean = re.sub(r"--[^\n]*", "", body)
        for col_match in _COLUMN_RE.finditer(body_clean):
            name = col_match.group(1)
            col_type = col_match.group(2).upper()
            if name.lower() in _CONSTRAINT_PREFIXES:
                continue
            out.append((table, name, col_type))
    return out


@pytest.fixture(scope="module")
def parsed_columns() -> list[tuple[str, str, str]]:
    sql = FIXTURE_PATH.read_text(encoding="utf-8")
    cols = _parse_columns(sql)
    # Sanity: the fixture has 5 tables x ~12 cols = ~60 columns. If
    # parsing breaks the table-finding regex we'd see zero here.
    assert len(cols) > 30, f"fixture parser only found {len(cols)} columns"
    return cols


class TestMockupSnapshot:
    def test_every_parsed_column_has_an_expected_entry(
        self, parsed_columns: list[tuple[str, str, str]]
    ) -> None:
        """Forces author-intent on every column.

        If a new column is added to the fixture without a matching
        entry in `_DESIRED`, this test fails — preventing silent gaps
        where a regression slips through because we forgot to assert.
        """
        missing = [
            f"{table}.{col}"
            for (table, col, _ctype) in parsed_columns
            if f"{table}.{col}" not in _DESIRED
        ]
        assert not missing, f"fixture columns missing from _DESIRED: {sorted(missing)}"

    def test_every_expected_entry_has_a_parsed_column(
        self, parsed_columns: list[tuple[str, str, str]]
    ) -> None:
        parsed_keys = {f"{table}.{col}" for (table, col, _ctype) in parsed_columns}
        stale = sorted(set(_DESIRED) - parsed_keys)
        assert not stale, f"_DESIRED entries missing from fixture (stale or typo): {stale}"

    def test_classifier_matches_desired_mapping(
        self, parsed_columns: list[tuple[str, str, str]]
    ) -> None:
        """The load-bearing assertion — every column matches its pin."""
        failures: list[str] = []
        for table, col, ctype in parsed_columns:
            key = f"{table}.{col}"
            expected_sens, expected_cats, expected_type = _DESIRED[key]
            # Sanity-check the parsed type matches what we pinned;
            # catches fixture edits that change types without the
            # snapshot author noticing.
            assert ctype == expected_type, (
                f"{key}: parsed type {ctype!r} != snapshot type {expected_type!r}"
            )
            sens, cats = classify_column(col, column_type=ctype)
            if sens != expected_sens or cats != expected_cats:
                failures.append(
                    f"  {key} (type={ctype}): "
                    f"got ({sens!r}, {sorted(cats)}), "
                    f"expected ({expected_sens!r}, {sorted(expected_cats)})"
                )
        assert not failures, "classifier output diverged from snapshot:\n" + "\n".join(failures)
