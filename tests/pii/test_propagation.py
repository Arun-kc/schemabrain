"""Propagation helper tests — parametrized + hypothesis property tests.

The ADR-prescribed invariants:
  - empty input → ("public", frozenset())
  - sensitivity propagates by MAX under the canonical ordering
  - categories propagate by UNION
  - order of inputs does not matter (commutativity)
  - adding a row never lowers sensitivity or shrinks categories (monotonicity)
  - output sensitivity ∈ SENSITIVITIES, categories ⊆ PII_CATEGORIES (closure)

Hypothesis exercises the algebraic identities on arbitrary inputs;
the parametrize block locks specific ADR examples.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from schemabrain.pii import (
    PII_CATEGORIES,
    SENSITIVITIES,
    PIICategory,
    Sensitivity,
    propagate,
)

# Hypothesis strategies built from the canonical enumerations so they
# stay aligned with the runtime contract automatically.
_sensitivity_st: st.SearchStrategy[Sensitivity] = st.sampled_from(SENSITIVITIES)
_category_st: st.SearchStrategy[PIICategory] = st.sampled_from(sorted(PII_CATEGORIES))
_categories_st: st.SearchStrategy[frozenset[PIICategory]] = st.frozensets(
    _category_st, max_size=len(PII_CATEGORIES)
)
_per_column_st = st.lists(st.tuples(_sensitivity_st, _categories_st), max_size=10)


class TestEmptyInput:
    def test_empty_input_returns_public_and_empty_categories(self) -> None:
        assert propagate([]) == ("public", frozenset())


class TestSensitivityMax:
    @pytest.mark.parametrize(
        ("inputs", "expected_sensitivity"),
        [
            ([("public", frozenset())], "public"),
            ([("internal", frozenset())], "internal"),
            ([("confidential", frozenset())], "confidential"),
            ([("pii", frozenset())], "pii"),
            ([("public", frozenset()), ("internal", frozenset())], "internal"),
            (
                [("internal", frozenset()), ("confidential", frozenset())],
                "confidential",
            ),
            (
                [("public", frozenset()), ("pii", frozenset())],
                "pii",
            ),
            (
                [("confidential", frozenset()), ("pii", frozenset())],
                "pii",
            ),
            (
                [
                    ("public", frozenset()),
                    ("internal", frozenset()),
                    ("confidential", frozenset()),
                    ("pii", frozenset()),
                ],
                "pii",
            ),
        ],
    )
    def test_sensitivity_max(
        self,
        inputs: list[tuple[Sensitivity, frozenset[PIICategory]]],
        expected_sensitivity: Sensitivity,
    ) -> None:
        sensitivity, _ = propagate(inputs)
        assert sensitivity == expected_sensitivity


class TestCategoryUnion:
    def test_two_distinct_categories_union(self) -> None:
        _, cats = propagate(
            [
                ("pii", frozenset({"contact"})),
                ("pii", frozenset({"financial"})),
            ]
        )
        assert cats == frozenset({"contact", "financial"})

    def test_overlapping_categories_dedup(self) -> None:
        _, cats = propagate(
            [
                ("pii", frozenset({"contact", "financial"})),
                ("pii", frozenset({"financial", "location"})),
            ]
        )
        assert cats == frozenset({"contact", "financial", "location"})

    def test_categories_propagate_even_when_sensitivity_is_public(self) -> None:
        # Defensive — heuristic classifier never emits this shape, but
        # propagation is independent of the relationship between the
        # two layers. If a future producer emits this combination, we
        # still want categories to propagate cleanly.
        _, cats = propagate(
            [
                ("public", frozenset({"contact"})),
                ("public", frozenset({"financial"})),
            ]
        )
        assert cats == frozenset({"contact", "financial"})


class TestPropertyInvariants:
    @given(_per_column_st)
    def test_output_sensitivity_is_in_canonical_set(
        self,
        per_column: list[tuple[Sensitivity, frozenset[PIICategory]]],
    ) -> None:
        sensitivity, _ = propagate(per_column)
        assert sensitivity in SENSITIVITIES

    @given(_per_column_st)
    def test_output_categories_subset_of_canonical_set(
        self,
        per_column: list[tuple[Sensitivity, frozenset[PIICategory]]],
    ) -> None:
        _, categories = propagate(per_column)
        assert categories <= PII_CATEGORIES

    @given(_per_column_st)
    def test_commutative_under_shuffle(
        self,
        per_column: list[tuple[Sensitivity, frozenset[PIICategory]]],
    ) -> None:
        forward = propagate(per_column)
        reverse = propagate(list(reversed(per_column)))
        assert forward == reverse

    @given(_per_column_st, st.tuples(_sensitivity_st, _categories_st))
    def test_monotonic_under_addition(
        self,
        per_column: list[tuple[Sensitivity, frozenset[PIICategory]]],
        extra: tuple[Sensitivity, frozenset[PIICategory]],
    ) -> None:
        before_sensitivity, before_categories = propagate(per_column)
        after_sensitivity, after_categories = propagate([*per_column, extra])
        # Sensitivity never decreases.
        before_rank = SENSITIVITIES.index(before_sensitivity)
        after_rank = SENSITIVITIES.index(after_sensitivity)
        assert after_rank >= before_rank
        # Categories never shrink.
        assert before_categories <= after_categories

    @given(st.tuples(_sensitivity_st, _categories_st))
    def test_idempotent_on_duplicate(
        self,
        single: tuple[Sensitivity, frozenset[PIICategory]],
    ) -> None:
        # propagate([x, x]) must equal propagate([x]) — duplicates carry
        # no additional information by either rule (MAX or UNION).
        assert propagate([single, single]) == propagate([single])

    @given(_per_column_st)
    def test_single_iteration_only_required(
        self,
        per_column: list[tuple[Sensitivity, frozenset[PIICategory]]],
    ) -> None:
        # The helper accepts any Iterable, but must consume the
        # iterable exactly once — a generator passed twice would
        # otherwise be empty on the second run.
        gen = (item for item in per_column)
        result = propagate(gen)
        # Second consumption of the same gen yields nothing — propagate
        # was called once, no leak.
        assert list(gen) == []
        # Result should match the list-based version.
        assert result == propagate(per_column)
