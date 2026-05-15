"""Pin the PII taxonomy Literal types and value enumerations.

The taxonomy is the public contract behind `docs/adr/0001-audit-row-and-
pii-taxonomy.md` and is referenced by every PII-carrying type (today:
`ExampleQuery`; later: entity columns, audit rows). Adding or removing
values from either Literal is a deliberate charter event — this file
makes any drift fail CI rather than silently flow through.
"""

from __future__ import annotations

from typing import get_args

from schemabrain.pii.categories import (
    PII_CATEGORIES,
    SENSITIVITIES,
    PIICategory,
    Sensitivity,
)


class TestSensitivityLiteralType:
    def test_sensitivities_tuple_is_ordered(self) -> None:
        # Ordering is load-bearing: MAX propagation uses index position.
        # public < internal < confidential < pii (per ADR Decision 3).
        assert SENSITIVITIES == ("public", "internal", "confidential", "pii")

    def test_sensitivity_literal_args_match_tuple(self) -> None:
        # The Literal type and the runtime tuple must agree exactly.
        # Drift would mean static type checks and runtime validation
        # diverge — a class of bug that's hard to spot at code review.
        assert get_args(Sensitivity) == SENSITIVITIES


class TestPIICategoryLiteralType:
    def test_pii_categories_count_is_twelve(self) -> None:
        # ADR Decision 3 locks the taxonomy at 12 categories. Additions
        # are minor charter bumps; this test fails to force the bump.
        assert len(PII_CATEGORIES) == 12

    def test_pii_categories_exact_membership(self) -> None:
        # The 12 categories named in the ADR. Order doesn't matter
        # (frozenset), but the membership is exact.
        expected = frozenset(
            {
                "contact",
                "financial",
                "payment_card",
                "health",
                "genetic",
                "biometric",
                "behavioral",
                "online_identifier",
                "credential",
                "government_id",
                "location",
                "demographic_protected",
            }
        )
        assert expected == PII_CATEGORIES

    def test_pii_category_literal_args_match_frozenset(self) -> None:
        # Static + runtime symmetry, same as for Sensitivity.
        assert frozenset(get_args(PIICategory)) == PII_CATEGORIES
