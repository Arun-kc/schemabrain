"""Tests for the pii_policy.yaml parser + emitter."""

from __future__ import annotations

from pathlib import Path

import pytest

from schemabrain.pii.policy import ColumnOverride, Policy
from schemabrain.pii.policy_yaml import (
    PolicyYamlError,
    parse_policy_yaml,
    parse_policy_yaml_file,
    policy_to_yaml,
)


class TestParseHappyPath:
    def test_minimal_policy_with_just_block(self) -> None:
        policy = parse_policy_yaml(
            """
            version: 1
            block:
              - credential
              - payment_card
              - government_id
            """
        )
        assert policy.block == frozenset({"credential", "payment_card", "government_id"})
        assert policy.column_overrides == ()
        assert policy.description == ""

    def test_empty_block_list_is_legal(self) -> None:
        policy = parse_policy_yaml(
            """
            version: 1
            block: []
            """
        )
        assert policy.block == frozenset()

    def test_description_carries_through(self) -> None:
        policy = parse_policy_yaml(
            """
            version: 1
            description: PCI-DSS Q&A 1.1 — last4 alone is not sensitive
            block:
              - credential
            """
        )
        assert "PCI-DSS" in policy.description

    def test_column_overrides_parse_into_dataclass_tuple(self) -> None:
        policy = parse_policy_yaml(
            """
            version: 1
            block:
              - credential
            column_overrides:
              public.users.email:
                sensitivity: internal
                categories: []
              public.payment_methods.card_number_last4:
                sensitivity: internal
                categories: []
            """
        )
        assert len(policy.column_overrides) == 2
        by_col = {o.qualified_column: o for o in policy.column_overrides}
        assert by_col["public.users.email"].sensitivity == "internal"
        assert by_col["public.users.email"].categories == frozenset()
        assert by_col["public.payment_methods.card_number_last4"].sensitivity == "internal"

    def test_categories_field_can_carry_pii_categories(self) -> None:
        # An override that DOWNGRADES from `pii` would set categories
        # to `[]`. An override that UPGRADES (operator asserts "this
        # plain column actually carries credential data") would set
        # categories to the new value — exercise this path.
        policy = parse_policy_yaml(
            """
            version: 1
            block: []
            column_overrides:
              public.app_state.session_token:
                sensitivity: pii
                categories:
                  - credential
            """
        )
        override = policy.column_overrides[0]
        assert override.categories == frozenset({"credential"})

    def test_categories_default_to_empty_when_omitted(self) -> None:
        policy = parse_policy_yaml(
            """
            version: 1
            block: []
            column_overrides:
              public.users.email:
                sensitivity: internal
            """
        )
        assert policy.column_overrides[0].categories == frozenset()


class TestParseRejectsBadStructure:
    def test_empty_document_rejected(self) -> None:
        with pytest.raises(PolicyYamlError, match="empty"):
            parse_policy_yaml("")

    def test_top_level_list_rejected(self) -> None:
        with pytest.raises(PolicyYamlError, match="mapping"):
            parse_policy_yaml("- credential\n- payment_card\n")

    def test_missing_version_rejected(self) -> None:
        with pytest.raises(PolicyYamlError, match="version"):
            parse_policy_yaml("block:\n  - credential\n")

    def test_missing_block_rejected(self) -> None:
        with pytest.raises(PolicyYamlError, match="block"):
            parse_policy_yaml("version: 1\n")

    def test_unknown_top_level_key_rejected(self) -> None:
        # Required keys present so the unknown-key check is the one
        # that fires (required-key check runs first).
        with pytest.raises(PolicyYamlError, match="unknown.*deny"):
            parse_policy_yaml(
                """
                version: 1
                block:
                  - credential
                deny:
                  - email
                """
            )

    def test_version_must_be_integer_one(self) -> None:
        with pytest.raises(PolicyYamlError, match="version"):
            parse_policy_yaml(
                """
                version: 2
                block:
                  - credential
                """
            )

    def test_string_version_rejected(self) -> None:
        with pytest.raises(PolicyYamlError, match="version"):
            parse_policy_yaml(
                """
                version: "1"
                block:
                  - credential
                """
            )

    def test_block_must_be_list(self) -> None:
        with pytest.raises(PolicyYamlError, match="block"):
            parse_policy_yaml(
                """
                version: 1
                block: credential
                """
            )

    def test_unknown_category_in_block_rejected(self) -> None:
        with pytest.raises(PolicyYamlError, match="block"):
            parse_policy_yaml(
                """
                version: 1
                block:
                  - sometimes_secret
                """
            )

    def test_duplicate_category_in_block_rejected(self) -> None:
        with pytest.raises(PolicyYamlError, match="duplicate"):
            parse_policy_yaml(
                """
                version: 1
                block:
                  - credential
                  - credential
                """
            )

    def test_column_overrides_must_be_mapping(self) -> None:
        with pytest.raises(PolicyYamlError, match="column_overrides"):
            parse_policy_yaml(
                """
                version: 1
                block: []
                column_overrides:
                  - public.users.email
                """
            )

    def test_column_overrides_missing_sensitivity_rejected(self) -> None:
        with pytest.raises(PolicyYamlError, match="sensitivity"):
            parse_policy_yaml(
                """
                version: 1
                block: []
                column_overrides:
                  public.users.email:
                    categories: []
                """
            )

    def test_column_overrides_unknown_field_rejected(self) -> None:
        with pytest.raises(PolicyYamlError, match="unknown"):
            parse_policy_yaml(
                """
                version: 1
                block: []
                column_overrides:
                  public.users.email:
                    sensitivity: internal
                    note: this is a note
                """
            )

    def test_malformed_qualified_column_rejected_with_position(self) -> None:
        with pytest.raises(PolicyYamlError, match="qualified_column"):
            parse_policy_yaml(
                """
                version: 1
                block: []
                column_overrides:
                  users.email:
                    sensitivity: internal
                """
            )

    def test_malformed_yaml_rejected(self) -> None:
        with pytest.raises(PolicyYamlError, match="parse"):
            parse_policy_yaml("version: 1\nblock: [: unbalanced")


class TestEmitAndRoundTrip:
    def test_minimal_policy_round_trips(self) -> None:
        original = Policy(block=frozenset({"credential", "government_id"}))
        text = policy_to_yaml(original)
        parsed = parse_policy_yaml(text)
        assert parsed == original

    def test_full_policy_with_overrides_round_trips(self) -> None:
        original = Policy(
            block=frozenset({"credential", "payment_card"}),
            description="last4 is fine",
            column_overrides=(
                ColumnOverride(
                    qualified_column="public.payment_methods.card_number_last4",
                    sensitivity="internal",
                    categories=frozenset(),
                ),
                ColumnOverride(
                    qualified_column="public.users.email",
                    sensitivity="internal",
                    categories=frozenset(),
                ),
            ),
        )
        text = policy_to_yaml(original)
        parsed = parse_policy_yaml(text)
        assert parsed == original

    def test_emitted_block_categories_are_in_canonical_order(self) -> None:
        # `PII_CATEGORIES_ORDERED` ordering — `contact` before
        # `payment_card` before `credential`. Two writes of the same
        # frozenset must produce byte-identical YAML so no-op edits
        # don't make noisy git diffs.
        policy = Policy(
            block=frozenset({"payment_card", "contact", "credential"})
        )
        text = policy_to_yaml(policy)
        contact_pos = text.index("contact")
        payment_card_pos = text.index("payment_card")
        credential_pos = text.index("credential")
        assert contact_pos < payment_card_pos < credential_pos

    def test_emitted_overrides_sorted_by_qualified_column(self) -> None:
        policy = Policy(
            block=frozenset(),
            column_overrides=(
                ColumnOverride(
                    qualified_column="public.users.email",
                    sensitivity="internal",
                ),
                ColumnOverride(
                    qualified_column="public.payment_methods.card_number_last4",
                    sensitivity="internal",
                ),
            ),
        )
        text = policy_to_yaml(policy)
        pm_pos = text.index("public.payment_methods.card_number_last4")
        users_pos = text.index("public.users.email")
        assert pm_pos < users_pos

    def test_emitted_yaml_omits_empty_description(self) -> None:
        policy = Policy(block=frozenset({"credential"}))
        text = policy_to_yaml(policy)
        assert "description" not in text

    def test_emitted_yaml_omits_overrides_when_empty(self) -> None:
        policy = Policy(block=frozenset({"credential"}))
        text = policy_to_yaml(policy)
        assert "column_overrides" not in text


class TestParseFile:
    def test_round_trip_through_disk(self, tmp_path: Path) -> None:
        original = Policy(
            block=frozenset({"credential"}),
            column_overrides=(
                ColumnOverride(
                    qualified_column="public.users.email",
                    sensitivity="internal",
                ),
            ),
        )
        path = tmp_path / "pii_policy.yaml"
        path.write_text(policy_to_yaml(original), encoding="utf-8")
        loaded = parse_policy_yaml_file(path)
        assert loaded == original

    def test_missing_file_propagates_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            parse_policy_yaml_file(tmp_path / "nonexistent.yaml")

    def test_directory_propagates_is_a_directory(self, tmp_path: Path) -> None:
        with pytest.raises(IsADirectoryError):
            parse_policy_yaml_file(tmp_path)
