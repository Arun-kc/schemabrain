"""Tests for `CanonicalJoin` + `JoinColumnPair` + `JoinOrigin` literal.

Locks the canonical-join-graph design decisions in code:

  - `name`-keyed identification (not `(source, target)`-keyed) so
    multiple canonical joins between the same entity pair coexist
    (billing/shipping address case).
  - Equi-join-only at v1; `on` is a non-empty tuple of
    `JoinColumnPair`.
  - Self-joins (`source_entity == target_entity`) are rejected —
    not supported (workaround: split into two entities).
  - `JoinOrigin = Literal["manual", "suggested", "dbt_import"]` —
    `dbt_import` is RESERVED (no joins producer yet) but
    valid at the dataclass + store layer for schema symmetry with
    `Entity.origin`.

All invariants enforced in `__post_init__` so store reads + YAML loads
+ mining output all converge on the same validity guarantee.
"""

from __future__ import annotations

import dataclasses

import pytest

from schemabrain.core.join import CanonicalJoin, JoinColumnPair

# ----- JoinColumnPair --------------------------------------------------------


class TestJoinColumnPair:
    def test_accepts_valid_identifier_pair(self) -> None:
        pair = JoinColumnPair(source_column="user_id", target_column="id")
        assert pair.source_column == "user_id"
        assert pair.target_column == "id"

    def test_is_frozen(self) -> None:
        pair = JoinColumnPair(source_column="user_id", target_column="id")
        with pytest.raises(dataclasses.FrozenInstanceError):
            pair.source_column = "other"  # type: ignore[misc]

    @pytest.mark.parametrize(
        "bad",
        [
            "",  # empty
            "1leading_digit",  # starts with digit
            "user id",  # space
            "user.id",  # dot (column-shaped — for entity columns, no dot)
            "user-id",  # hyphen
        ],
    )
    def test_rejects_non_identifier_source_column(self, bad: str) -> None:
        with pytest.raises(ValueError, match="source_column"):
            JoinColumnPair(source_column=bad, target_column="id")

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "1leading_digit",
            "id space",
            "id.x",
        ],
    )
    def test_rejects_non_identifier_target_column(self, bad: str) -> None:
        with pytest.raises(ValueError, match="target_column"):
            JoinColumnPair(source_column="user_id", target_column=bad)

    def test_accepts_dollar_sign_in_identifier(self) -> None:
        # Postgres column shape — `row$id` is valid unquoted. Matches
        # the alphabet used by `Entity.identity`.
        pair = JoinColumnPair(source_column="row$id", target_column="id$")
        assert pair.source_column == "row$id"

    def test_two_pairs_with_same_columns_are_equal(self) -> None:
        a = JoinColumnPair(source_column="user_id", target_column="id")
        b = JoinColumnPair(source_column="user_id", target_column="id")
        assert a == b


# ----- CanonicalJoin ---------------------------------------------------------


def _pair(src: str = "user_id", tgt: str = "id") -> JoinColumnPair:
    return JoinColumnPair(source_column=src, target_column=tgt)


def _make_join(**overrides: object) -> CanonicalJoin:
    defaults: dict[str, object] = {
        "name": "customer_orders",
        "description": "",
        "source_entity": "order",
        "target_entity": "customer",
        "on": (_pair(),),
        "origin": "manual",
    }
    defaults.update(overrides)
    return CanonicalJoin(**defaults)  # type: ignore[arg-type]


class TestCanonicalJoinHappyPath:
    def test_constructs_with_defaults(self) -> None:
        join = _make_join()
        assert join.name == "customer_orders"
        assert join.source_entity == "order"
        assert join.target_entity == "customer"
        assert len(join.on) == 1
        assert join.origin == "manual"

    def test_origin_defaults_to_manual(self) -> None:
        # YAML hand-edited files omit origin; the default makes that
        # ergonomic without breaking the closed-Literal invariant.
        join = CanonicalJoin(
            name="customer_orders",
            description="",
            source_entity="order",
            target_entity="customer",
            on=(_pair(),),
        )
        assert join.origin == "manual"

    def test_is_frozen(self) -> None:
        join = _make_join()
        with pytest.raises(dataclasses.FrozenInstanceError):
            join.name = "other"  # type: ignore[misc]

    def test_two_equal_joins_compare_equal(self) -> None:
        # Frozen + tuple-typed `on` means equality is field-wise.
        a = _make_join()
        b = _make_join()
        assert a == b


class TestCanonicalJoinComposite:
    def test_accepts_composite_on_list(self) -> None:
        # Composite-PK joins (junction tables, natural keys) need
        # multiple `on` pairs. Length-N tuple supported from day one.
        join = _make_join(
            on=(
                _pair("org_id", "id"),
                _pair("user_id", "id"),
            ),
        )
        assert len(join.on) == 2
        assert join.on[0].source_column == "org_id"
        assert join.on[1].source_column == "user_id"

    def test_on_is_a_tuple(self) -> None:
        # Tuple, not list — preserves hashability + frozen invariant.
        join = _make_join()
        assert isinstance(join.on, tuple)


class TestCanonicalJoinValidation:
    @pytest.mark.parametrize(
        "bad_name",
        [
            "",
            "1starts_with_digit",
            "has space",
            "has-hyphen",
            "has.dot",
        ],
    )
    def test_rejects_non_identifier_name(self, bad_name: str) -> None:
        with pytest.raises(ValueError, match="name"):
            _make_join(name=bad_name)

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "1starts_with_digit",
            "has space",
        ],
    )
    def test_rejects_non_identifier_source_entity(self, bad: str) -> None:
        with pytest.raises(ValueError, match="source_entity"):
            _make_join(source_entity=bad)

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "1starts_with_digit",
            "has-hyphen",
        ],
    )
    def test_rejects_non_identifier_target_entity(self, bad: str) -> None:
        with pytest.raises(ValueError, match="target_entity"):
            _make_join(target_entity=bad)

    def test_rejects_self_join(self) -> None:
        # Self-joins (manager_of_user, etc.) are not supported by the
        # CanonicalJoin dataclass invariant. Workaround for users:
        # split into two entities (e.g., `manager` and `direct_report`)
        # and join on the FK column.
        with pytest.raises(ValueError, match="self-joins"):
            _make_join(source_entity="user", target_entity="user")

    def test_rejects_empty_on(self) -> None:
        # Cross joins are not canonical. The dataclass refuses construction;
        # the YAML parser surfaces a guided error instead.
        with pytest.raises(ValueError, match="at least one"):
            _make_join(on=())

    @pytest.mark.parametrize(
        "bad_origin",
        [
            "",
            "approved",  # not in Literal
            "imported",  # close but wrong
            "DBT_IMPORT",  # case-sensitive
            None,
        ],
    )
    def test_rejects_unknown_origin(self, bad_origin: object) -> None:
        with pytest.raises(ValueError, match="origin"):
            _make_join(origin=bad_origin)

    def test_accepts_all_three_valid_origins(self) -> None:
        # `dbt_import` is RESERVED — the dataclass accepts it, but the
        # suggest/apply pipelines refuse to produce it because the
        # dbt-relationships joins importer hasn't shipped yet. Lock
        # the symmetry with `Entity.origin` here so the schema stays
        # consistent across the semantic-layer surface.
        for origin in ("manual", "suggested", "dbt_import"):
            _make_join(origin=origin)

    def test_rejects_unknown_inference_method(self) -> None:
        # Charter v1.2 2D trust signal: the closed-set check in
        # `__post_init__` rejects any inference_method outside the
        # five-value Literal. Mirrors the equivalent on `Entity` and
        # `Metric`.
        with pytest.raises(ValueError, match="inference_method"):
            _make_join(inference_method="bogus")

    def test_rejects_unknown_validation_state(self) -> None:
        # Charter v1.2 2D trust signal: `validation_state` must be
        # one of {draft, applied, confirmed}.
        with pytest.raises(ValueError, match="validation_state"):
            _make_join(validation_state="archived")


# ----- Cardinality -----------------------------------------------------------


class TestCanonicalJoinCardinality:
    def test_cardinality_defaults_to_none(self) -> None:
        # Joins authored before the cardinality column was added do
        # not carry one. Default `None` keeps those rows valid on
        # round-trip; the compiler treats `None` as `many_to_many`
        # (worst-case → fan-out warning).
        join = _make_join()
        assert join.cardinality is None

    @pytest.mark.parametrize(
        "cardinality",
        ["one_to_one", "one_to_many", "many_to_one", "many_to_many"],
    )
    def test_accepts_all_four_cardinality_literals(self, cardinality: str) -> None:
        join = _make_join(cardinality=cardinality)
        assert join.cardinality == cardinality

    @pytest.mark.parametrize(
        "bad_cardinality",
        [
            "",
            "1_to_1",
            "ONE_TO_MANY",  # case-sensitive
            "to_one",
            "many",
            "1:1",
        ],
    )
    def test_rejects_unknown_cardinality(self, bad_cardinality: str) -> None:
        with pytest.raises(ValueError, match="cardinality"):
            _make_join(cardinality=bad_cardinality)

    def test_cardinality_participates_in_equality(self) -> None:
        # Frozen + new field — two joins with same fields but different
        # cardinality are unequal. Catches accidental field-omission
        # bugs in `_row_to_canonical_join` that would silently drop the
        # column on round-trip.
        a = _make_join(cardinality="one_to_many")
        b = _make_join(cardinality="many_to_one")
        assert a != b

    def test_explicit_none_equal_to_default(self) -> None:
        a = _make_join()
        b = _make_join(cardinality=None)
        assert a == b


# ----- flip_cardinality ------------------------------------------------------


class TestFlipCardinality:
    """`flip_cardinality(c)` mirrors a stored cardinality across the
    reverse-traversal axis. Load-bearing for the BFS-traversal-aware
    fan-out detector — a stored `many_to_one` traversed in reverse
    actually multiplies anchor rows in the chain's direction.
    """

    def test_one_to_many_flips_to_many_to_one(self) -> None:
        from schemabrain.core.join import flip_cardinality

        assert flip_cardinality("one_to_many") == "many_to_one"

    def test_many_to_one_flips_to_one_to_many(self) -> None:
        from schemabrain.core.join import flip_cardinality

        assert flip_cardinality("many_to_one") == "one_to_many"

    def test_one_to_one_is_symmetric(self) -> None:
        from schemabrain.core.join import flip_cardinality

        assert flip_cardinality("one_to_one") == "one_to_one"

    def test_many_to_many_is_symmetric(self) -> None:
        from schemabrain.core.join import flip_cardinality

        assert flip_cardinality("many_to_many") == "many_to_many"

    def test_none_passes_through(self) -> None:
        # `None` propagates so the worst-case "treat as many_to_many"
        # downstream behaves identically regardless of traversal
        # direction.
        from schemabrain.core.join import flip_cardinality

        assert flip_cardinality(None) is None
