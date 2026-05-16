"""Tests for the entity YAML grammar parser.

Locks the design decisions at the YAML boundary:
  - single_table binding only; multi_table is "deferred to v2"
  - `version: 1` required; absence + wrong version both fail
  - identity required
  - origin Literal closed set, defaults to "manual"

Error contract: every rejection raises `EntityParseError` (a `ValueError`
subclass) with a message that NAMES the offending field and shows the
value when relevant. The CLI relies on this — `schemabrain entities apply
<path>` prints the error message verbatim and exits 1.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.entities.yaml_grammar import (
    EntityParseError,
    parse_entity_yaml,
    parse_entity_yaml_file,
)

# ----- helpers ---------------------------------------------------------------

_MINIMAL_YAML = """\
version: 1
name: customer
binding:
  single_table: public.customers
identity: id
"""

_FULL_YAML = """\
version: 1
name: customer
description: A registered shopper
binding:
  single_table: public.customers
identity: id
origin: manual
"""


# ----- happy path ------------------------------------------------------------


class TestParseHappyPath:
    def test_minimal_yaml_parses(self) -> None:
        entity = parse_entity_yaml(_MINIMAL_YAML)
        assert entity == Entity(
            name="customer",
            description="",
            binding=SingleTableBinding(qualified_table="public.customers"),
            identity="id",
            origin="manual",
        )

    def test_full_yaml_parses(self) -> None:
        entity = parse_entity_yaml(_FULL_YAML)
        assert entity.name == "customer"
        assert entity.description == "A registered shopper"
        assert entity.binding.qualified_table == "public.customers"
        assert entity.identity == "id"
        assert entity.origin == "manual"

    def test_description_defaults_to_empty(self) -> None:
        entity = parse_entity_yaml(_MINIMAL_YAML)
        assert entity.description == ""

    def test_origin_defaults_to_manual(self) -> None:
        entity = parse_entity_yaml(_MINIMAL_YAML)
        assert entity.origin == "manual"

    @pytest.mark.parametrize("origin", ["manual", "suggested", "dbt_import"])
    def test_all_origin_literals_accepted(self, origin: str) -> None:
        yaml_text = _MINIMAL_YAML + f"origin: {origin}\n"
        entity = parse_entity_yaml(yaml_text)
        assert entity.origin == origin


# ----- version field ---------------------------------------------------------


class TestVersionField:
    def test_missing_version_rejected(self) -> None:
        yaml_text = """\
name: customer
binding:
  single_table: public.customers
identity: id
"""
        with pytest.raises(EntityParseError, match="version"):
            parse_entity_yaml(yaml_text)

    def test_wrong_version_rejected(self) -> None:
        yaml_text = _MINIMAL_YAML.replace("version: 1", "version: 2")
        with pytest.raises(EntityParseError, match=r"version.*2"):
            parse_entity_yaml(yaml_text)

    def test_string_version_rejected(self) -> None:
        """`version: "1"` is a string, not the int 1 — reject strictly."""
        yaml_text = _MINIMAL_YAML.replace("version: 1", 'version: "1"')
        with pytest.raises(EntityParseError, match="version"):
            parse_entity_yaml(yaml_text)


# ----- single_table binding only ---------------------------------------------


class TestBindingShape:
    def test_multi_table_binding_rejected_with_v2_hint(self) -> None:
        yaml_text = """\
version: 1
name: user
binding:
  multi_table: public.users + public.profiles
identity: id
"""
        with pytest.raises(EntityParseError, match=r"multi_table.*v2"):
            parse_entity_yaml(yaml_text)

    def test_unknown_binding_shape_rejected(self) -> None:
        yaml_text = """\
version: 1
name: customer
binding:
  view: public.customer_view
identity: id
"""
        with pytest.raises(EntityParseError, match="binding"):
            parse_entity_yaml(yaml_text)

    def test_ambiguous_binding_rejected(self) -> None:
        yaml_text = """\
version: 1
name: customer
binding:
  single_table: public.customers
  multi_table: public.users + public.profiles
identity: id
"""
        with pytest.raises(EntityParseError, match="binding"):
            parse_entity_yaml(yaml_text)

    def test_missing_binding_rejected(self) -> None:
        yaml_text = """\
version: 1
name: customer
identity: id
"""
        with pytest.raises(EntityParseError, match="binding"):
            parse_entity_yaml(yaml_text)

    def test_empty_binding_rejected(self) -> None:
        yaml_text = """\
version: 1
name: customer
binding: {}
identity: id
"""
        with pytest.raises(EntityParseError, match="binding"):
            parse_entity_yaml(yaml_text)

    def test_non_string_single_table_rejected(self) -> None:
        yaml_text = """\
version: 1
name: customer
binding:
  single_table: 42
identity: id
"""
        with pytest.raises(EntityParseError, match="single_table"):
            parse_entity_yaml(yaml_text)

    def test_binding_not_a_mapping_rejected(self) -> None:
        yaml_text = """\
version: 1
name: customer
binding: "public.customers"
identity: id
"""
        with pytest.raises(EntityParseError, match="binding must be a mapping"):
            parse_entity_yaml(yaml_text)

    def test_malformed_qualified_table_rejected(self) -> None:
        """`single_table` is a string but not `schema.table` form."""
        yaml_text = """\
version: 1
name: customer
binding:
  single_table: customers
identity: id
"""
        with pytest.raises(EntityParseError, match="single_table"):
            parse_entity_yaml(yaml_text)


# ----- identity required -----------------------------------------------------


class TestIdentityField:
    def test_missing_identity_rejected(self) -> None:
        yaml_text = """\
version: 1
name: customer
binding:
  single_table: public.customers
"""
        with pytest.raises(EntityParseError, match="identity"):
            parse_entity_yaml(yaml_text)

    def test_empty_identity_rejected(self) -> None:
        yaml_text = """\
version: 1
name: customer
binding:
  single_table: public.customers
identity: ""
"""
        with pytest.raises(EntityParseError, match="identity"):
            parse_entity_yaml(yaml_text)

    def test_non_string_identity_rejected(self) -> None:
        yaml_text = """\
version: 1
name: customer
binding:
  single_table: public.customers
identity: 42
"""
        with pytest.raises(EntityParseError, match="identity"):
            parse_entity_yaml(yaml_text)


# ----- origin -----------------------------------------------------------------


class TestOriginField:
    def test_unknown_origin_rejected(self) -> None:
        yaml_text = _MINIMAL_YAML + "origin: auto_inferred\n"
        with pytest.raises(EntityParseError, match="origin"):
            parse_entity_yaml(yaml_text)

    def test_non_string_origin_rejected(self) -> None:
        yaml_text = _MINIMAL_YAML + "origin: 42\n"
        with pytest.raises(EntityParseError, match="origin must be a string"):
            parse_entity_yaml(yaml_text)


# ----- name + structural -----------------------------------------------------


class TestNameField:
    def test_missing_name_rejected(self) -> None:
        yaml_text = """\
version: 1
binding:
  single_table: public.customers
identity: id
"""
        with pytest.raises(EntityParseError, match="name"):
            parse_entity_yaml(yaml_text)

    def test_non_identifier_name_rejected(self) -> None:
        yaml_text = _MINIMAL_YAML.replace("name: customer", "name: customer-1")
        with pytest.raises(EntityParseError, match="name"):
            parse_entity_yaml(yaml_text)

    def test_non_string_name_rejected(self) -> None:
        yaml_text = _MINIMAL_YAML.replace("name: customer", "name: 42")
        with pytest.raises(EntityParseError, match="name"):
            parse_entity_yaml(yaml_text)


class TestDescriptionField:
    def test_non_string_description_rejected(self) -> None:
        yaml_text = _MINIMAL_YAML + "description: 42\n"
        with pytest.raises(EntityParseError, match="description"):
            parse_entity_yaml(yaml_text)


class TestStructuralRejections:
    def test_empty_yaml_rejected(self) -> None:
        with pytest.raises(EntityParseError, match="empty"):
            parse_entity_yaml("")

    def test_top_level_list_rejected(self) -> None:
        yaml_text = "- version: 1\n- name: customer\n"
        with pytest.raises(EntityParseError, match="mapping"):
            parse_entity_yaml(yaml_text)

    def test_top_level_scalar_rejected(self) -> None:
        with pytest.raises(EntityParseError, match="mapping"):
            parse_entity_yaml("customer")

    def test_unknown_top_level_key_rejected(self) -> None:
        yaml_text = _MINIMAL_YAML + "unknown_field: oops\n"
        with pytest.raises(EntityParseError, match="unknown_field"):
            parse_entity_yaml(yaml_text)

    def test_malformed_yaml_rejected(self) -> None:
        with pytest.raises(EntityParseError, match="parse"):
            parse_entity_yaml("version: 1\n  bad: indent: here")


# ----- file loader -----------------------------------------------------------


class TestParseEntityYamlFile:
    def test_loads_valid_file(self, tmp_path: Path) -> None:
        yaml_path = tmp_path / "customer.yaml"
        yaml_path.write_text(_MINIMAL_YAML)
        entity = parse_entity_yaml_file(yaml_path)
        assert entity.name == "customer"

    def test_nonexistent_file_raises_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            parse_entity_yaml_file(tmp_path / "missing.yaml")

    def test_directory_path_raises(self, tmp_path: Path) -> None:
        # IsADirectoryError on POSIX; the test just asserts SOME OSError.
        with pytest.raises(OSError):
            parse_entity_yaml_file(tmp_path)

    def test_parse_error_in_file_includes_path(self, tmp_path: Path) -> None:
        yaml_path = tmp_path / "broken.yaml"
        yaml_path.write_text("name: customer\n")  # missing version, binding, identity
        with pytest.raises(EntityParseError):
            parse_entity_yaml_file(yaml_path)

    def test_permission_error_wrapped_as_parse_error(self, tmp_path: Path) -> None:
        """Unreadable file → `EntityParseError`, not raw `PermissionError`."""
        import os
        import sys

        if sys.platform == "win32" or os.geteuid() == 0:
            pytest.skip("chmod-based unreadability does not apply (Windows or root)")
        yaml_path = tmp_path / "locked.yaml"
        yaml_path.write_text(_MINIMAL_YAML)
        yaml_path.chmod(0o000)
        try:
            with pytest.raises(EntityParseError, match="permission denied"):
                parse_entity_yaml_file(yaml_path)
        finally:
            # Restore so pytest can clean up tmp_path.
            yaml_path.chmod(0o600)

    def test_non_utf8_file_wrapped_as_parse_error(self, tmp_path: Path) -> None:
        """A binary file masquerading as YAML surfaces with a clear
        UTF-8 message, not a raw `UnicodeDecodeError` traceback."""
        yaml_path = tmp_path / "binary.yaml"
        # UTF-16 BOM + a few non-ASCII bytes — not valid UTF-8.
        yaml_path.write_bytes(b"\xff\xfe\x00\x80\x81")
        with pytest.raises(EntityParseError, match="UTF-8"):
            parse_entity_yaml_file(yaml_path)


# ----- error type ------------------------------------------------------------


class TestErrorType:
    def test_entity_parse_error_is_value_error(self) -> None:
        """Callers should be able to catch parse errors as ValueError."""
        with pytest.raises(ValueError):
            parse_entity_yaml("")
