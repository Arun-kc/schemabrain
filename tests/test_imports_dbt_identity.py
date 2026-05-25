"""Tests for dbt identity column resolution — 3-tier priority logic.

Pins the second piece of the dbt import substrate:

  - `resolve_dbt_identity(model)` returns the column to bind as the
    SchemaBrain entity's `identity`, plus a `method` label naming
    which priority tier matched (audit-log surface).
  - 3-tier priority order:
      1. Explicit `primary_key` constraint on a single column
      2. `unique` + `not_null` constraint pair on a single column
      3. `unique` + `not_null` test pair on a single column (older
         dbt projects without `constraints` adoption)
  - Refusal cases (raise `DbtIdentityResolutionError`):
      - No column matches any tier
      - Multiple columns match within the SAME tier (composite-PK
        signal — deferred to v2 multi-table bindings)
  - Higher tiers take precedence over lower tiers. A model with both
    `primary_key` (tier 1) AND a separate column with `unique+not_null`
    (tier 2) resolves to the tier-1 column.

The identity-resolution method label is consumed in two places:
  - Audit log `entity.imported.dbt` event carries the method
  - CLI summary breadcrumb counts use of each method (signal for "do
    your dbt projects rely on tests rather than constraints?")
"""

from __future__ import annotations

import pytest

from schemabrain.imports.dbt import (
    DbtColumn,
    DbtColumnTests,
    DbtConstraint,
    DbtIdentityResolution,
    DbtIdentityResolutionError,
    DbtModelNode,
    resolve_dbt_identity,
)


def _col(
    name: str,
    *,
    constraints: tuple[DbtConstraint, ...] = (),
    is_unique: bool = False,
    is_not_null: bool = False,
) -> DbtColumn:
    return DbtColumn(
        name=name,
        data_type=None,
        description="",
        constraints=constraints,
        tests=DbtColumnTests(is_unique=is_unique, is_not_null=is_not_null),
    )


def _model(*columns: DbtColumn, name: str = "customer_dim") -> DbtModelNode:
    return DbtModelNode(
        unique_id=f"model.demo.{name}",
        name=name,
        database="schemabrain_test",
        schema_name="public",
        identifier=name,
        description="",
        columns=tuple(columns),
        depends_on_sources=(),
    )


# ----- Tier 1: explicit primary_key constraint ------------------------------


class TestTier1PrimaryKeyConstraint:
    def test_single_primary_key_constraint_wins(self) -> None:
        model = _model(
            _col("id", constraints=(DbtConstraint(type="primary_key"),)),
            _col("email"),
        )
        result = resolve_dbt_identity(model)
        assert isinstance(result, DbtIdentityResolution)
        assert result.column_name == "id"
        assert result.method == "primary_key_constraint"

    def test_primary_key_wins_over_unique_not_null_constraints(self) -> None:
        # Tier 1 trumps tier 2 even when both candidate columns exist
        # in the same model.
        model = _model(
            _col("id", constraints=(DbtConstraint(type="primary_key"),)),
            _col(
                "email",
                constraints=(
                    DbtConstraint(type="unique"),
                    DbtConstraint(type="not_null"),
                ),
            ),
        )
        result = resolve_dbt_identity(model)
        assert result.column_name == "id"
        assert result.method == "primary_key_constraint"

    def test_primary_key_wins_over_unique_not_null_tests(self) -> None:
        # Tier 1 trumps tier 3.
        model = _model(
            _col("id", constraints=(DbtConstraint(type="primary_key"),)),
            _col("email", is_unique=True, is_not_null=True),
        )
        result = resolve_dbt_identity(model)
        assert result.column_name == "id"
        assert result.method == "primary_key_constraint"

    def test_two_primary_key_constraints_refuses(self) -> None:
        # Composite PK in dbt — both columns flagged primary_key. v1
        # supports only single-column identity (multi-table bindings
        # deferred to v2), so refuse rather than guess which is right.
        model = _model(
            _col("order_id", constraints=(DbtConstraint(type="primary_key"),)),
            _col("line_no", constraints=(DbtConstraint(type="primary_key"),)),
        )
        with pytest.raises(DbtIdentityResolutionError) as exc_info:
            resolve_dbt_identity(model)
        msg = str(exc_info.value)
        # Names both candidate columns so the user knows what to fix
        # (drop the composite-PK declaration on one, or wait for v2
        # multi-table support).
        assert "order_id" in msg
        assert "line_no" in msg


# ----- Tier 2: unique + not_null constraint pair ----------------------------


class TestTier2UniqueNotNullConstraints:
    def test_unique_plus_not_null_constraints_match(self) -> None:
        model = _model(
            _col(
                "email",
                constraints=(
                    DbtConstraint(type="unique"),
                    DbtConstraint(type="not_null"),
                ),
            ),
            _col("name"),
        )
        result = resolve_dbt_identity(model)
        assert result.column_name == "email"
        assert result.method == "unique_not_null_constraints"

    def test_unique_alone_does_not_match(self) -> None:
        # A unique-only column could still hold NULLs in some DBs.
        # Identity must be both unique AND not-null.
        model = _model(
            _col("email", constraints=(DbtConstraint(type="unique"),)),
        )
        with pytest.raises(DbtIdentityResolutionError):
            resolve_dbt_identity(model)

    def test_not_null_alone_does_not_match(self) -> None:
        # Not-null-only fails the unique half of the contract.
        model = _model(
            _col("email", constraints=(DbtConstraint(type="not_null"),)),
        )
        with pytest.raises(DbtIdentityResolutionError):
            resolve_dbt_identity(model)

    def test_tier_2_wins_over_tier_3(self) -> None:
        # Tier 2 (constraint syntax) is more explicit than tier 3 (test
        # syntax) — even if both candidate columns exist, the constraint
        # column wins.
        model = _model(
            _col(
                "email",
                constraints=(
                    DbtConstraint(type="unique"),
                    DbtConstraint(type="not_null"),
                ),
            ),
            _col("legacy_id", is_unique=True, is_not_null=True),
        )
        result = resolve_dbt_identity(model)
        assert result.column_name == "email"
        assert result.method == "unique_not_null_constraints"

    def test_two_tier_2_columns_refuses(self) -> None:
        # Same shape as tier-1 composite-PK refusal — ambiguity within
        # a tier is unresolvable.
        model = _model(
            _col(
                "email",
                constraints=(
                    DbtConstraint(type="unique"),
                    DbtConstraint(type="not_null"),
                ),
            ),
            _col(
                "username",
                constraints=(
                    DbtConstraint(type="unique"),
                    DbtConstraint(type="not_null"),
                ),
            ),
        )
        with pytest.raises(DbtIdentityResolutionError) as exc_info:
            resolve_dbt_identity(model)
        msg = str(exc_info.value)
        assert "email" in msg
        assert "username" in msg


# ----- Tier 3: unique + not_null test pair ----------------------------------


class TestTier3UniqueNotNullTests:
    def test_unique_plus_not_null_tests_match(self) -> None:
        model = _model(_col("id", is_unique=True, is_not_null=True))
        result = resolve_dbt_identity(model)
        assert result.column_name == "id"
        assert result.method == "unique_not_null_tests"

    def test_unique_test_alone_does_not_match(self) -> None:
        model = _model(_col("id", is_unique=True, is_not_null=False))
        with pytest.raises(DbtIdentityResolutionError):
            resolve_dbt_identity(model)

    def test_not_null_test_alone_does_not_match(self) -> None:
        model = _model(_col("id", is_unique=False, is_not_null=True))
        with pytest.raises(DbtIdentityResolutionError):
            resolve_dbt_identity(model)

    def test_two_tier_3_columns_refuses(self) -> None:
        model = _model(
            _col("user_id", is_unique=True, is_not_null=True),
            _col("legacy_id", is_unique=True, is_not_null=True),
        )
        with pytest.raises(DbtIdentityResolutionError) as exc_info:
            resolve_dbt_identity(model)
        msg = str(exc_info.value)
        assert "user_id" in msg
        assert "legacy_id" in msg


# ----- Refusal: no match across any tier ------------------------------------


class TestRefusalNoMatch:
    def test_no_constraints_no_tests_refuses(self) -> None:
        model = _model(_col("name"), _col("description"))
        with pytest.raises(DbtIdentityResolutionError) as exc_info:
            resolve_dbt_identity(model)
        msg = str(exc_info.value)
        # Names the model that failed + lists the columns so the user
        # knows what to add identity tests/constraints to.
        assert "customer_dim" in msg
        assert "name" in msg
        assert "description" in msg

    def test_empty_columns_refuses(self) -> None:
        # A dbt model with no declared columns can't anchor an entity.
        # Refuse explicitly so the caller sees the structural failure
        # rather than getting a zero-column entity downstream.
        model = _model()  # no columns
        with pytest.raises(DbtIdentityResolutionError) as exc_info:
            resolve_dbt_identity(model)
        assert "customer_dim" in str(exc_info.value)

    def test_unrelated_constraints_dont_count(self) -> None:
        # foreign_key and check constraints don't satisfy identity.
        model = _model(
            _col(
                "user_id",
                constraints=(
                    DbtConstraint(type="foreign_key", name="fk_user"),
                    DbtConstraint(type="check", name="ck_user_id_pos"),
                ),
            ),
        )
        with pytest.raises(DbtIdentityResolutionError):
            resolve_dbt_identity(model)


# ----- DbtIdentityResolution dataclass shape -------------------------------


class TestDbtIdentityResolutionShape:
    def test_is_frozen(self) -> None:
        import dataclasses

        resolution = DbtIdentityResolution(column_name="id", method="primary_key_constraint")
        with pytest.raises(dataclasses.FrozenInstanceError):
            resolution.column_name = "other"  # type: ignore[misc]

    def test_compares_by_value(self) -> None:
        a = DbtIdentityResolution(column_name="id", method="primary_key_constraint")
        b = DbtIdentityResolution(column_name="id", method="primary_key_constraint")
        assert a == b

    def test_rejects_invalid_method_value(self) -> None:
        # `method` is a closed Literal — the audit-log dashboard reads it
        # to bucket imports by resolution tier. An unknown value would
        # silently break the bucketing.
        with pytest.raises(ValueError, match="method"):
            DbtIdentityResolution(column_name="id", method="some_unknown_method")  # type: ignore[arg-type]


# ----- Determinism guarantee ------------------------------------------------


class TestDeterminism:
    def test_resolution_is_deterministic_across_identical_models(self) -> None:
        # Two structurally-identical models must resolve to the same
        # column + method. Guards against any iteration-order or
        # set-based ambiguity sneaking into the resolver.
        model_a = _model(
            _col("id", constraints=(DbtConstraint(type="primary_key"),)),
            _col("email"),
        )
        model_b = _model(
            _col("id", constraints=(DbtConstraint(type="primary_key"),)),
            _col("email"),
        )
        assert resolve_dbt_identity(model_a) == resolve_dbt_identity(model_b)
