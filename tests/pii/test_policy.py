"""Tests for the Policy + ColumnOverride dataclasses.

Validation invariants live in `__post_init__`, so construction-time
exercises drive the test surface — anyone reading these tests gets
the field-shape contract front and centre.
"""

from __future__ import annotations

import pytest

from schemabrain.pii.policy import ColumnOverride, Policy


class TestColumnOverride:
    def test_minimum_valid_override_constructs(self) -> None:
        override = ColumnOverride(
            qualified_column="public.users.email",
            sensitivity="internal",
            categories=frozenset(),
        )
        assert override.qualified_column == "public.users.email"
        assert override.sensitivity == "internal"
        assert override.categories == frozenset()

    def test_categories_default_to_empty_frozenset(self) -> None:
        override = ColumnOverride(
            qualified_column="public.users.email",
            sensitivity="public",
        )
        assert override.categories == frozenset()

    def test_qualified_column_must_be_three_dotted_parts(self) -> None:
        with pytest.raises(ValueError, match="qualified_column"):
            ColumnOverride(qualified_column="users.email", sensitivity="internal")

    def test_qualified_column_rejects_four_part_form(self) -> None:
        with pytest.raises(ValueError, match="qualified_column"):
            ColumnOverride(
                qualified_column="db.public.users.email",
                sensitivity="internal",
            )

    def test_qualified_column_rejects_leading_digit_in_any_part(self) -> None:
        with pytest.raises(ValueError, match="qualified_column"):
            ColumnOverride(
                qualified_column="public.users.9email",
                sensitivity="internal",
            )

    def test_qualified_column_rejects_dashes(self) -> None:
        with pytest.raises(ValueError, match="qualified_column"):
            ColumnOverride(
                qualified_column="public.users.user-id",
                sensitivity="internal",
            )

    def test_sensitivity_must_be_in_the_closed_literal(self) -> None:
        with pytest.raises(ValueError, match="sensitivity"):
            ColumnOverride(
                qualified_column="public.users.email",
                sensitivity="totally_secret",  # type: ignore[arg-type]
            )

    def test_categories_rejected_when_not_in_closed_set(self) -> None:
        with pytest.raises(ValueError, match="categories"):
            ColumnOverride(
                qualified_column="public.users.email",
                sensitivity="internal",
                categories=frozenset({"not_a_real_category"}),  # type: ignore[arg-type]
            )

    def test_qualified_table_slices_off_the_column(self) -> None:
        override = ColumnOverride(
            qualified_column="public.users.email",
            sensitivity="internal",
        )
        assert override.qualified_table == "public.users"

    def test_column_name_slices_off_the_table(self) -> None:
        override = ColumnOverride(
            qualified_column="public.users.email",
            sensitivity="internal",
        )
        assert override.column_name == "email"


class TestPolicy:
    def test_minimum_valid_policy_constructs(self) -> None:
        policy = Policy(block=frozenset({"credential"}))
        assert policy.block == frozenset({"credential"})
        assert policy.column_overrides == ()
        assert policy.description == ""

    def test_empty_block_set_is_legal(self) -> None:
        # Empty set = explicit opt-out from enforcement (matches the
        # `--pii-block ''` CLI semantic at `cli.py:3112-3113`).
        policy = Policy(block=frozenset())
        assert policy.block == frozenset()

    def test_block_rejects_unknown_categories(self) -> None:
        with pytest.raises(ValueError, match="block"):
            Policy(block=frozenset({"sometimes_secret"}))  # type: ignore[arg-type]

    def test_column_overrides_carry_through(self) -> None:
        override = ColumnOverride(
            qualified_column="public.users.email",
            sensitivity="internal",
        )
        policy = Policy(
            block=frozenset({"credential"}),
            column_overrides=(override,),
        )
        assert policy.column_overrides == (override,)

    def test_duplicate_column_override_rejected(self) -> None:
        override_a = ColumnOverride(
            qualified_column="public.users.email",
            sensitivity="internal",
        )
        override_b = ColumnOverride(
            qualified_column="public.users.email",
            sensitivity="confidential",
        )
        with pytest.raises(ValueError, match="duplicate"):
            Policy(
                block=frozenset(),
                column_overrides=(override_a, override_b),
            )

    def test_description_is_preserved(self) -> None:
        policy = Policy(
            block=frozenset({"credential"}),
            description="PCI-DSS Q&A 1.1 — last4 alone is not sensitive",
        )
        assert policy.description == "PCI-DSS Q&A 1.1 — last4 alone is not sensitive"
