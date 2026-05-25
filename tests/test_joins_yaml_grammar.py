"""Tests for `joins/yaml_grammar.py`.

Pins the canonical-join YAML grammar:

  - `version: 1` required; reject string / float / mismatched values
  - Top-level required keys: `version`, `name`, `source_entity`,
    `target_entity`, `on`
  - Top-level optional keys: `description`, `origin`
  - Strict-keys rejection of typos
  - `on` is a non-empty list of `{source, target}` mappings
  - Self-joins refused (not supported)
  - All three origin Literal values valid (`dbt_import` reserved)
  - `CanonicalJoinParseError` is the uniform CLI-facing error
"""

from __future__ import annotations

from pathlib import Path

import pytest

from schemabrain.core.join import CanonicalJoin, JoinColumnPair
from schemabrain.joins.yaml_grammar import (
    CanonicalJoinParseError,
    canonical_join_to_yaml,
    parse_canonical_join_yaml,
    parse_canonical_join_yaml_file,
)

# ----- happy paths -----------------------------------------------------------


def _minimal_yaml() -> str:
    return """
version: 1
name: customer_orders
source_entity: order
target_entity: customer
on:
  - source: user_id
    target: id
""".strip()


class TestHappyPath:
    def test_minimal_yaml_round_trips(self) -> None:
        join = parse_canonical_join_yaml(_minimal_yaml())
        assert isinstance(join, CanonicalJoin)
        assert join.name == "customer_orders"
        assert join.source_entity == "order"
        assert join.target_entity == "customer"
        assert join.origin == "manual"
        assert join.description == ""

    def test_optional_description_carries_through(self) -> None:
        text = """
version: 1
name: customer_orders
description: Links each order to its customer.
source_entity: order
target_entity: customer
on:
  - source: user_id
    target: id
""".strip()
        join = parse_canonical_join_yaml(text)
        assert join.description == "Links each order to its customer."

    def test_explicit_origin_carries_through(self) -> None:
        text = """
version: 1
name: customer_orders
source_entity: order
target_entity: customer
on:
  - source: user_id
    target: id
origin: suggested
""".strip()
        join = parse_canonical_join_yaml(text)
        assert join.origin == "suggested"

    @pytest.mark.parametrize("origin", ["manual", "suggested"])
    def test_manual_and_suggested_origins_accepted(self, origin: str) -> None:
        # `dbt_import` is RESERVED at this release and refused at YAML
        # parse-time (see `TestDbtImportReservation` below); only the
        # two values with active producers parse cleanly.
        text = f"""
version: 1
name: customer_orders
source_entity: order
target_entity: customer
on:
  - source: user_id
    target: id
origin: {origin}
""".strip()
        join = parse_canonical_join_yaml(text)
        assert join.origin == origin


class TestDbtImportReservation:
    def test_dbt_import_origin_refused_at_parse_time(self) -> None:
        # The dbt-relationships joins importer hasn't shipped. Until then,
        # `origin: dbt_import` in a hand-authored YAML is a user error
        # — silently re-labelling to `manual` (the prior behaviour) hid
        # the deferral. Refuse at parse with a guided message.
        text = """
version: 1
name: customer_orders
source_entity: order
target_entity: customer
on:
  - source: user_id
    target: id
origin: dbt_import
""".strip()
        with pytest.raises(CanonicalJoinParseError, match="dbt-relationships"):
            parse_canonical_join_yaml(text)

    def test_composite_on_list_supported(self) -> None:
        # Composite-PK joins land as multi-pair `on` lists from day one.
        text = """
version: 1
name: composite_join
source_entity: org_member
target_entity: org
on:
  - source: org_id
    target: id
  - source: tenant_id
    target: tenant_id
""".strip()
        join = parse_canonical_join_yaml(text)
        assert len(join.on) == 2
        assert join.on[0].source_column == "org_id"
        assert join.on[1].source_column == "tenant_id"


# ----- version validation ----------------------------------------------------


class TestVersionValidation:
    def test_rejects_missing_version(self) -> None:
        text = """
name: x
source_entity: a
target_entity: b
on:
  - source: c
    target: d
""".strip()
        with pytest.raises(CanonicalJoinParseError, match="version"):
            parse_canonical_join_yaml(text)

    def test_rejects_string_version(self) -> None:
        text = """
version: "1"
name: x
source_entity: a
target_entity: b
on:
  - source: c
    target: d
""".strip()
        with pytest.raises(CanonicalJoinParseError, match="version must be the integer"):
            parse_canonical_join_yaml(text)

    def test_rejects_float_version(self) -> None:
        text = """
version: 1.0
name: x
source_entity: a
target_entity: b
on:
  - source: c
    target: d
""".strip()
        with pytest.raises(CanonicalJoinParseError, match="version must be the integer"):
            parse_canonical_join_yaml(text)

    def test_rejects_unsupported_version(self) -> None:
        text = """
version: 2
name: x
source_entity: a
target_entity: b
on:
  - source: c
    target: d
""".strip()
        with pytest.raises(CanonicalJoinParseError, match="version must be 1"):
            parse_canonical_join_yaml(text)

    def test_rejects_bool_version(self) -> None:
        # `version: true` parses as bool in YAML, isinstance(True, int)
        # is True so we must guard against bool explicitly.
        text = """
version: true
name: x
source_entity: a
target_entity: b
on:
  - source: c
    target: d
""".strip()
        with pytest.raises(CanonicalJoinParseError, match="version must be the integer"):
            parse_canonical_join_yaml(text)


# ----- strict-keys + required fields -----------------------------------------


class TestStrictKeys:
    def test_rejects_unknown_top_level_key(self) -> None:
        text = """
version: 1
name: x
source_entity: a
target_entity: b
on:
  - source: c
    target: d
sourse_entity: typo
""".strip()
        with pytest.raises(CanonicalJoinParseError, match="unknown top-level keys"):
            parse_canonical_join_yaml(text)

    def test_rejects_missing_required_keys(self) -> None:
        text = """
version: 1
name: x
""".strip()
        with pytest.raises(CanonicalJoinParseError, match="missing required field"):
            parse_canonical_join_yaml(text)

    def test_rejects_unknown_on_item_key(self) -> None:
        text = """
version: 1
name: x
source_entity: a
target_entity: b
on:
  - source: c
    target: d
    extra: oops
""".strip()
        with pytest.raises(CanonicalJoinParseError, match=r"unknown on\[0\] keys"):
            parse_canonical_join_yaml(text)


# ----- structural validation -------------------------------------------------


class TestStructuralValidation:
    def test_rejects_non_mapping_top_level(self) -> None:
        with pytest.raises(CanonicalJoinParseError, match="mapping at the top level"):
            parse_canonical_join_yaml("- a\n- b\n")

    def test_rejects_empty_yaml(self) -> None:
        with pytest.raises(CanonicalJoinParseError, match="empty"):
            parse_canonical_join_yaml("")

    def test_rejects_malformed_yaml(self) -> None:
        with pytest.raises(CanonicalJoinParseError, match="failed to parse YAML"):
            parse_canonical_join_yaml("name: [unclosed")

    def test_rejects_non_list_on(self) -> None:
        text = """
version: 1
name: x
source_entity: a
target_entity: b
on: "just a string"
""".strip()
        with pytest.raises(CanonicalJoinParseError, match="on must be a list"):
            parse_canonical_join_yaml(text)

    def test_rejects_empty_on_list(self) -> None:
        text = """
version: 1
name: x
source_entity: a
target_entity: b
on: []
""".strip()
        with pytest.raises(CanonicalJoinParseError, match="at least one column pair"):
            parse_canonical_join_yaml(text)

    def test_rejects_non_mapping_on_item(self) -> None:
        text = """
version: 1
name: x
source_entity: a
target_entity: b
on:
  - just_a_string
""".strip()
        with pytest.raises(CanonicalJoinParseError, match=r"on\[0\] must be a mapping"):
            parse_canonical_join_yaml(text)


# ----- self-join refusal -----------------------------------------------------


class TestSelfJoinRefusal:
    def test_self_join_refused_at_parse_time(self) -> None:
        # Refusal lands via the dataclass `__post_init__` path; the
        # error gets wrapped in `CanonicalJoinParseError` with the
        # "canonical-join validation failed" prefix.
        text = """
version: 1
name: self_join
source_entity: user
target_entity: user
on:
  - source: manager_id
    target: id
""".strip()
        with pytest.raises(CanonicalJoinParseError, match="self-joins"):
            parse_canonical_join_yaml(text)


# ----- bad-value validation --------------------------------------------------


class TestBadValues:
    def test_rejects_non_string_name(self) -> None:
        text = """
version: 1
name: 123
source_entity: a
target_entity: b
on:
  - source: c
    target: d
""".strip()
        with pytest.raises(CanonicalJoinParseError, match="name must be a string"):
            parse_canonical_join_yaml(text)

    def test_rejects_empty_name(self) -> None:
        text = """
version: 1
name: ""
source_entity: a
target_entity: b
on:
  - source: c
    target: d
""".strip()
        with pytest.raises(CanonicalJoinParseError, match="name must be non-empty"):
            parse_canonical_join_yaml(text)

    def test_rejects_unknown_origin(self) -> None:
        text = """
version: 1
name: x
source_entity: a
target_entity: b
on:
  - source: c
    target: d
origin: approved
""".strip()
        with pytest.raises(CanonicalJoinParseError, match="origin must be one of"):
            parse_canonical_join_yaml(text)

    def test_rejects_non_identifier_column(self) -> None:
        text = """
version: 1
name: x
source_entity: a
target_entity: b
on:
  - source: "with space"
    target: id
""".strip()
        with pytest.raises(CanonicalJoinParseError):
            parse_canonical_join_yaml(text)


# ----- file-based parsing ----------------------------------------------------


class TestParseFile:
    def test_parses_valid_file(self, tmp_path: Path) -> None:
        path = tmp_path / "join.yaml"
        path.write_text(_minimal_yaml(), encoding="utf-8")
        join = parse_canonical_join_yaml_file(path)
        assert join.name == "customer_orders"

    def test_missing_file_raises_file_not_found(self, tmp_path: Path) -> None:
        # FileNotFoundError propagates verbatim — the CLI distinguishes
        # "missing file" from "malformed YAML."
        with pytest.raises(FileNotFoundError):
            parse_canonical_join_yaml_file(tmp_path / "nope.yaml")

    def test_directory_raises_is_a_directory(self, tmp_path: Path) -> None:
        with pytest.raises(IsADirectoryError):
            parse_canonical_join_yaml_file(tmp_path)

    def test_non_utf8_file_raises_parse_error(self, tmp_path: Path) -> None:
        path = tmp_path / "junk.yaml"
        path.write_bytes(b"\xff\xfe\xfd\xfc\xfb")  # invalid UTF-8
        from schemabrain.joins.yaml_grammar import CanonicalJoinParseError

        with pytest.raises(CanonicalJoinParseError, match="not a valid UTF-8"):
            parse_canonical_join_yaml_file(path)


class TestNonStringFieldValues:
    def test_non_string_description_rejected(self) -> None:
        text = """
version: 1
name: x
description: 123
source_entity: a
target_entity: b
"on":
  - source: c
    target: d
""".strip()
        with pytest.raises(CanonicalJoinParseError, match="description must be a string"):
            parse_canonical_join_yaml(text)

    def test_non_string_origin_rejected(self) -> None:
        text = """
version: 1
name: x
source_entity: a
target_entity: b
"on":
  - source: c
    target: d
origin: 42
""".strip()
        with pytest.raises(CanonicalJoinParseError, match="origin must be a string"):
            parse_canonical_join_yaml(text)

    def test_non_string_source_in_on_item_rejected(self) -> None:
        text = """
version: 1
name: x
source_entity: a
target_entity: b
"on":
  - source: 123
    target: d
""".strip()
        with pytest.raises(CanonicalJoinParseError, match=r"on\[0\]\.source must be"):
            parse_canonical_join_yaml(text)

    def test_non_string_target_in_on_item_rejected(self) -> None:
        text = """
version: 1
name: x
source_entity: a
target_entity: b
"on":
  - source: c
    target: 123
""".strip()
        with pytest.raises(CanonicalJoinParseError, match=r"on\[0\]\.target must be"):
            parse_canonical_join_yaml(text)


class TestCardinalityField:
    @pytest.mark.parametrize(
        "cardinality",
        ["one_to_one", "one_to_many", "many_to_one", "many_to_many"],
    )
    def test_cardinality_carries_through(self, cardinality: str) -> None:
        # `cardinality` is optional at the grammar layer (older rows
        # have `None`); when populated the parser validates the closed
        # Literal before delegating to the dataclass.
        text = f"""
version: 1
name: customer_orders
source_entity: order
target_entity: customer
on:
  - source: user_id
    target: id
cardinality: {cardinality}
""".strip()
        join = parse_canonical_join_yaml(text)
        assert join.cardinality == cardinality

    def test_cardinality_absent_yields_none(self) -> None:
        # Hand-authored YAML pre-dating cardinality lacks the field; we keep
        # the round-trip valid by treating absence as `None`.
        text = """
version: 1
name: customer_orders
source_entity: order
target_entity: customer
on:
  - source: user_id
    target: id
""".strip()
        join = parse_canonical_join_yaml(text)
        assert join.cardinality is None

    def test_unknown_cardinality_rejected(self) -> None:
        text = """
version: 1
name: customer_orders
source_entity: order
target_entity: customer
on:
  - source: user_id
    target: id
cardinality: many_per_few
""".strip()
        with pytest.raises(CanonicalJoinParseError, match="cardinality"):
            parse_canonical_join_yaml(text)

    def test_non_string_cardinality_rejected(self) -> None:
        text = """
version: 1
name: customer_orders
source_entity: order
target_entity: customer
on:
  - source: user_id
    target: id
cardinality: 42
""".strip()
        with pytest.raises(CanonicalJoinParseError, match="cardinality"):
            parse_canonical_join_yaml(text)


class TestCanonicalJoinToYamlRoundTrip:
    """`canonical_join_to_yaml` is the inverse of
    `parse_canonical_join_yaml`. Round-trip invariant is load-bearing
    for the CLI export → edit → apply workflow.

    Critically: the `"on":` key must be force-quoted in the output.
    The native YAML 1.1 boolean coercion would parse a bare `on:`
    key as `True:`, silently turning a valid join body into "missing
    required field" on re-parse. The serialiser dodges this by
    splitting the body into a yaml.safe_dump'd head + tail and
    hand-rolling the `"on":` block between them.
    """

    def _basic_join(self, **overrides: object) -> CanonicalJoin:
        base = {
            "name": "order_to_customer",
            "description": "Orders belong to customers",
            "source_entity": "order",
            "target_entity": "customer",
            "on": (JoinColumnPair(source_column="customer_id", target_column="id"),),
            "origin": "manual",
            "cardinality": "many_to_one",
        }
        base.update(overrides)
        return CanonicalJoin(**base)  # type: ignore[arg-type]

    def test_basic_join_round_trips(self) -> None:
        original = self._basic_join()
        body = canonical_join_to_yaml(original)
        assert parse_canonical_join_yaml(body) == original

    def test_on_key_is_force_quoted(self) -> None:
        """The serialiser MUST emit `"on":` with quotes so a re-parse
        under stricter (YAML 1.1) loaders does not coerce the key to
        `True`. This is the load-bearing reason the join serialiser
        splits the body around `yaml.safe_dump` rather than dumping
        the whole dict at once.
        """
        original = self._basic_join()
        body = canonical_join_to_yaml(original)
        assert '"on":' in body
        # No bare `^on:` line — guard against an inadvertent dedupe
        # of the quoting later.
        for line in body.splitlines():
            if line.startswith("on:"):
                raise AssertionError(
                    f"bare `on:` line emitted: {line!r} — would parse as True under YAML 1.1"
                )

    def test_composite_key_join_round_trips(self) -> None:
        """Junction tables use composite-key joins — the `on` list has
        multiple pairs. Both pairs must survive the round-trip.
        """
        original = self._basic_join(
            on=(
                JoinColumnPair(source_column="customer_id", target_column="id"),
                JoinColumnPair(source_column="region_id", target_column="region_id"),
            ),
        )
        body = canonical_join_to_yaml(original)
        assert parse_canonical_join_yaml(body) == original

    def test_missing_cardinality_omitted(self) -> None:
        """A join with `cardinality=None` (back-compat shape for joins
        authored before the column existed) must emit a body without
        a `cardinality:` line — the parser fills None when the field
        is absent.
        """
        original = self._basic_join(cardinality=None)
        body = canonical_join_to_yaml(original)
        assert "cardinality:" not in body
        assert parse_canonical_join_yaml(body) == original

    def test_trust_signal_fields_not_emitted(self) -> None:
        original = self._basic_join(
            inference_method="fk_constraint",
            validation_state="applied",
        )
        body = canonical_join_to_yaml(original)
        assert "inference_method" not in body
        assert "validation_state" not in body
        round_tripped = parse_canonical_join_yaml(body)
        # Trust signal resets to defaults; the operator edited the
        # file, so they ARE manually-authored now.
        assert round_tripped.inference_method == "manually_authored"
