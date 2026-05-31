"""Freeze-gate pins for the v0.5.0 `saas` demo schema.

The saas demo DB schema is FROZEN per
`docs/internal/v0.5.0_saas_demo_db_design_spec.md` (operator-signed).
Its whole value proposition is that the bundled, zero-config demo fires
the firewall on all three catastrophic-leak legs. That only holds if the
column names the schema froze still classify into the expected
categories. These tests pin that contract directly against the live
classifier (name-only — exactly how the get_metric / describe_* gates
evaluate a column), so a future classifier change that would let a
frozen demo column leak fails CI here.

See also `tests/test_demo_fixture_consistency.py::
test_saas_fixture_present_with_all_three_catastrophic_legs`, which pins
that the columns exist in the shipped SQL.
"""

from __future__ import annotations

import pytest

from schemabrain.pii.classifier import classify_column

# Frozen catastrophic columns: (column, expected_category). Each MUST
# classify (pii, {category}) so the default --pii-block floor refuses it.
_CATASTROPHIC: list[tuple[str, str]] = [
    ("password_hash", "credential"),
    ("api_key_hash", "credential"),
    ("session_token", "credential"),
    ("card_number_last4", "payment_card"),
    ("tax_id", "government_id"),
    ("national_id", "government_id"),
]

# Frozen benign columns that LOOK adjacent to PII but must stay public so
# safe get_metric leaderboards + group_by dimensions are not over-blocked.
_BENIGN: list[str] = [
    "card_brand",
    "card_exp_month",
    "card_exp_year",
    "key_prefix",
    "mrr_cents",
    "invoice_total_cents",
    "unit_price_cents",
    "monthly_price_cents",
    "seat_count",
    "included_seats",
    "plan_tier",
    "sla_tier",
    "plan_code",
    "legal_entity",
    "slug",
    "role",
    "status",
    "event_type",
    "region",
    "scope",
    "quantity",
]


@pytest.mark.parametrize("column,expected_category", _CATASTROPHIC)
def test_catastrophic_columns_classify_into_floor(column: str, expected_category: str) -> None:
    """Every frozen catastrophic column fires its catastrophic leg."""
    sensitivity, categories = classify_column(column)
    assert sensitivity == "pii", f"{column!r} -> {sensitivity!r}, expected pii"
    assert categories == frozenset({expected_category}), (
        f"{column!r} -> {sorted(categories)}, expected {{{expected_category!r}}}"
    )


@pytest.mark.parametrize("column", _BENIGN)
def test_benign_companions_classify_public(column: str) -> None:
    """Every frozen benign companion / dimension stays public (no over-block)."""
    sensitivity, categories = classify_column(column)
    assert (sensitivity, categories) == ("public", frozenset()), (
        f"{column!r} -> ({sensitivity!r}, {sorted(categories)}); a frozen benign "
        f"column must classify public so safe leaderboards are not over-blocked"
    )


def test_section_2_4_refusal_and_positive_control_split() -> None:
    """The §2.4 NON-NEGOTIABLE beat: password_hash refuses, role allows."""
    # group_by user.password_hash -> blocked (credential floor).
    assert classify_column("password_hash") == ("pii", frozenset({"credential"}))
    # paired positive control group_by user.role -> allowed (public).
    assert classify_column("role") == ("public", frozenset())
