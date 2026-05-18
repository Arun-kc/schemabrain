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

from schemabrain.core.join import CanonicalJoin
from schemabrain.joins.yaml_grammar import (
    CanonicalJoinParseError,
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
