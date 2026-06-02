"""Tests for the Entity dataclass + SingleTableBinding + Origin literal.

Locks the semantic-layer-foundation design decisions in code:
  - single_table binding only (no multi-table at v1)
  - identity column required (no optional identity)
  - origin Literal["manual", "suggested", "dbt_import"], default "manual"

Plus dataclass-level invariants: frozen, identifier-shaped names, qualified
binding form, equality semantics. These constraints are enforced in
__post_init__ so the store-side writer can never persist a malformed row.
"""

from __future__ import annotations

import dataclasses

import pytest

from schemabrain.core.entity import Entity, SingleTableBinding

# ----- SingleTableBinding ----------------------------------------------------


class TestSingleTableBinding:
    def test_accepts_valid_qualified_table(self) -> None:
        binding = SingleTableBinding(qualified_table="public.customers")
        assert binding.qualified_table == "public.customers"

    def test_is_frozen(self) -> None:
        binding = SingleTableBinding(qualified_table="public.customers")
        with pytest.raises(dataclasses.FrozenInstanceError):
            binding.qualified_table = "public.orders"  # type: ignore[misc]

    @pytest.mark.parametrize(
        "bad",
        [
            "",  # empty
            "customers",  # zero dots
            "public.customers.id",  # two dots (column-shaped)
            ".customers",  # empty schema part
            "public.",  # empty table part
            "public..customers",  # empty middle part
            "public customers",  # space in name
        ],
    )
    def test_rejects_non_qualified_table(self, bad: str) -> None:
        with pytest.raises(ValueError, match="qualified_table"):
            SingleTableBinding(qualified_table=bad)

    def test_two_bindings_with_same_table_are_equal(self) -> None:
        a = SingleTableBinding(qualified_table="public.customers")
        b = SingleTableBinding(qualified_table="public.customers")
        assert a == b


# ----- Entity ----------------------------------------------------------------


def _make_entity(**overrides: object) -> Entity:
    defaults: dict[str, object] = {
        "name": "customer",
        "description": "A registered shopper",
        "binding": SingleTableBinding(qualified_table="public.customers"),
        "identity": "id",
    }
    defaults.update(overrides)
    return Entity(**defaults)  # type: ignore[arg-type]


class TestEntityHappyPath:
    def test_constructs_with_required_fields(self) -> None:
        entity = _make_entity()
        assert entity.name == "customer"
        assert entity.description == "A registered shopper"
        assert entity.binding.qualified_table == "public.customers"
        assert entity.identity == "id"

    def test_origin_defaults_to_manual(self) -> None:
        entity = _make_entity()
        assert entity.origin == "manual"

    def test_accepts_empty_description(self) -> None:
        entity = _make_entity(description="")
        assert entity.description == ""

    def test_is_frozen(self) -> None:
        entity = _make_entity()
        with pytest.raises(dataclasses.FrozenInstanceError):
            entity.name = "order"  # type: ignore[misc]


class TestEntityNameValidation:
    @pytest.mark.parametrize(
        "good",
        [
            "customer",
            "order_item",
            "Customer",
            "_internal",
            "x",
            "a1",
            "snake_case_99",
        ],
    )
    def test_accepts_identifier_shaped_names(self, good: str) -> None:
        entity = _make_entity(name=good)
        assert entity.name == good

    @pytest.mark.parametrize(
        "bad",
        [
            "",  # empty
            " ",  # whitespace
            "customer ",  # trailing space
            " customer",  # leading space
            "1customer",  # leading digit
            "customer-1",  # hyphen
            "customer.id",  # dot
            "customer name",  # internal space
            "cust\nomer",  # newline
        ],
    )
    def test_rejects_non_identifier_names(self, bad: str) -> None:
        with pytest.raises(ValueError, match="name"):
            _make_entity(name=bad)


class TestEntityIdentityValidation:
    """Identity column is required on single_table binding."""

    @pytest.mark.parametrize("good", ["id", "customer_id", "_id", "ID"])
    def test_accepts_identifier_shaped_identity(self, good: str) -> None:
        entity = _make_entity(identity=good)
        assert entity.identity == good

    @pytest.mark.parametrize(
        "bad",
        ["", " ", "id ", "1id", "id.name", "id-name", "id name"],
    )
    def test_rejects_non_identifier_identity(self, bad: str) -> None:
        with pytest.raises(ValueError, match="identity"):
            _make_entity(identity=bad)


class TestEntityOrigin:
    """Origin Literal closed enum, default 'manual'."""

    @pytest.mark.parametrize("origin", ["manual", "suggested", "dbt_import"])
    def test_accepts_all_origin_literals(self, origin: str) -> None:
        entity = _make_entity(origin=origin)
        assert entity.origin == origin

    def test_rejects_unknown_origin(self) -> None:
        with pytest.raises(ValueError, match="origin"):
            _make_entity(origin="auto_inferred")


class TestEntityGroup:
    """Group Literal closed enum, default 'other' (v15)."""

    @pytest.mark.parametrize("group", ["identity", "billing", "activity", "other"])
    def test_accepts_all_group_literals(self, group: str) -> None:
        entity = _make_entity(group=group)
        assert entity.group == group

    def test_defaults_to_other(self) -> None:
        assert _make_entity().group == "other"

    def test_rejects_unknown_group(self) -> None:
        with pytest.raises(ValueError, match="group"):
            _make_entity(group="financial")


class TestEntityTrustSignalGuards:
    """The 2D trust-signal fields enforce their closed enums in
    `__post_init__` so a direct (untyped) caller can't smuggle a bad
    value past the store-side CHECK (v14).
    """

    def test_rejects_unknown_inference_method(self) -> None:
        with pytest.raises(ValueError, match="inference_method"):
            _make_entity(inference_method="psychic")

    def test_rejects_unknown_validation_state(self) -> None:
        with pytest.raises(ValueError, match="validation_state"):
            _make_entity(validation_state="probably")


class TestEntityEquality:
    def test_identical_entities_are_equal(self) -> None:
        a = _make_entity()
        b = _make_entity()
        assert a == b

    def test_differing_origin_breaks_equality(self) -> None:
        a = _make_entity(origin="manual")
        b = _make_entity(origin="suggested")
        assert a != b

    def test_differing_description_breaks_equality(self) -> None:
        a = _make_entity(description="A")
        b = _make_entity(description="B")
        assert a != b

    def test_entities_are_hashable(self) -> None:
        a = _make_entity()
        b = _make_entity()
        assert hash(a) == hash(b)
        assert {a, b} == {a}
