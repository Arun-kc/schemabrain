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
        with pytest.raises(PolicyYamlError, match=r"unknown.*deny"):
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

    def test_description_non_string_rejected(self) -> None:
        with pytest.raises(PolicyYamlError, match="description"):
            parse_policy_yaml("version: 1\ndescription: 42\nblock: []\n")

    def test_block_item_not_string_rejected(self) -> None:
        with pytest.raises(PolicyYamlError, match="block items"):
            parse_policy_yaml("version: 1\nblock:\n  - 42\n")

    def test_column_overrides_non_string_key_rejected(self) -> None:
        # YAML maps allow int keys. Surface the type mismatch at the
        # grammar layer so the qualified-column regex doesn't blow up
        # later with a less informative message.
        with pytest.raises(PolicyYamlError, match="column_overrides keys"):
            parse_policy_yaml(
                """
                version: 1
                block: []
                column_overrides:
                  42:
                    sensitivity: internal
                """
            )

    def test_column_overrides_scalar_body_rejected(self) -> None:
        with pytest.raises(PolicyYamlError, match="must be a mapping"):
            parse_policy_yaml(
                """
                version: 1
                block: []
                column_overrides:
                  public.users.email: internal
                """
            )

    def test_column_overrides_non_string_sensitivity_rejected(self) -> None:
        with pytest.raises(PolicyYamlError, match="sensitivity must be"):
            parse_policy_yaml(
                """
                version: 1
                block: []
                column_overrides:
                  public.users.email:
                    sensitivity: 42
                """
            )

    def test_column_overrides_unknown_sensitivity_rejected(self) -> None:
        with pytest.raises(PolicyYamlError, match="sensitivity must be"):
            parse_policy_yaml(
                """
                version: 1
                block: []
                column_overrides:
                  public.users.email:
                    sensitivity: top_secret
                """
            )

    def test_column_overrides_categories_not_list_rejected(self) -> None:
        with pytest.raises(PolicyYamlError, match="categories must be a"):
            parse_policy_yaml(
                """
                version: 1
                block: []
                column_overrides:
                  public.users.email:
                    sensitivity: internal
                    categories: credential
                """
            )

    def test_column_overrides_category_item_not_string_rejected(self) -> None:
        with pytest.raises(PolicyYamlError, match="categories items"):
            parse_policy_yaml(
                """
                version: 1
                block: []
                column_overrides:
                  public.users.email:
                    sensitivity: internal
                    categories:
                      - 42
                """
            )

    def test_column_overrides_duplicate_category_rejected(self) -> None:
        with pytest.raises(PolicyYamlError, match="duplicate"):
            parse_policy_yaml(
                """
                version: 1
                block: []
                column_overrides:
                  public.users.email:
                    sensitivity: pii
                    categories:
                      - credential
                      - credential
                """
            )


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
        policy = Policy(block=frozenset({"payment_card", "contact", "credential"}))
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

    def test_permission_error_wraps_as_policy_yaml_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Real chmod-based denial is non-portable (no-op for root in
        # CI containers); patching `read_text` makes the contract test
        # deterministic across runners.
        target = tmp_path / "pii_policy.yaml"
        target.write_text("version: 1\nblock: []\n", encoding="utf-8")

        def _raise_permission_error(*_args: object, **_kwargs: object) -> str:
            raise PermissionError(13, "Permission denied", str(target))

        monkeypatch.setattr(Path, "read_text", _raise_permission_error)
        with pytest.raises(PolicyYamlError, match="permission denied"):
            parse_policy_yaml_file(target)

    def test_unicode_decode_error_wraps_as_policy_yaml_error(self, tmp_path: Path) -> None:
        # Real-world failure mode: an operator hands a file saved in
        # latin-1 / utf-16 / a binary blob. The parser must surface
        # "not UTF-8" rather than letting the decoder bubble up.
        target = tmp_path / "pii_policy.yaml"
        target.write_bytes(b"\xff\xfe\x00v\x00e\x00r\x00s\x00i\x00o\x00n\x00")
        with pytest.raises(PolicyYamlError, match="not a valid UTF-8"):
            parse_policy_yaml_file(target)
