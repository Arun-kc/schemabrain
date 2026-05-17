"""Tests for the canonical audit-row serialisation.

The canonical bytes are the input to both the chain hash and any
downstream tamper-evidence check. Tests pin the determinism, key-
ordering, binary-field handling, and strict-shape invariants the
chain depends on.
"""

from __future__ import annotations

import json

import pytest

from schemabrain.audit.canonical import (
    AUDIT_ROW_FIELDS,
    canonical_audit_row,
)


def _baseline_row() -> dict[str, object]:
    return {
        "id": 42,
        "occurred_at": "2026-05-17T18:00:00.123456Z",
        "source_connection_id": "src1",
        "caller_id": None,
        "tool_name": "find_relevant_tables",
        "status": "success",
        "refusal_reason": None,
        "cost_class": "small",
        "pii_categories": "",
        "ast_shape_hash": None,
        "rule_id": None,
        "fingerprint": bytes.fromhex("aa" * 32),
        "fingerprint_version": "fp-v1",
    }


class TestFieldSet:
    def test_field_set_is_thirteen(self) -> None:
        """The chain depends on the row being exactly the 13 documented
        fields (chain_hash itself is field 14 and is the OUTPUT, not the
        input). Adding a field silently would change every downstream
        hash without test failure — pin the count."""
        assert len(AUDIT_ROW_FIELDS) == 13

    def test_field_set_matches_adr(self) -> None:
        """The field NAMES are pinned to ADR 0001's DDL. Any rename or
        addition is a coordinated schema bump, not a silent change."""
        expected = {
            "id",
            "occurred_at",
            "source_connection_id",
            "caller_id",
            "tool_name",
            "status",
            "refusal_reason",
            "cost_class",
            "pii_categories",
            "ast_shape_hash",
            "rule_id",
            "fingerprint",
            "fingerprint_version",
        }
        assert set(AUDIT_ROW_FIELDS) == expected


class TestDeterminism:
    def test_same_dict_same_bytes(self) -> None:
        row = _baseline_row()
        assert canonical_audit_row(row) == canonical_audit_row(row)

    def test_key_order_does_not_matter(self) -> None:
        row1 = _baseline_row()
        row2 = dict(reversed(list(row1.items())))
        assert canonical_audit_row(row1) == canonical_audit_row(row2)

    def test_output_is_bytes(self) -> None:
        assert isinstance(canonical_audit_row(_baseline_row()), bytes)


class TestBinaryFieldHandling:
    def test_bytes_render_as_lowercase_hex(self) -> None:
        row = _baseline_row()
        row["fingerprint"] = bytes.fromhex("DEADBEEF" + "00" * 28)
        parsed = json.loads(canonical_audit_row(row).decode("utf-8"))
        # Lowercase hex; binary never appears in the JSON payload.
        assert parsed["fingerprint"] == "deadbeef" + "00" * 28

    def test_ast_shape_hash_none_serialises_as_null(self) -> None:
        row = _baseline_row()
        row["ast_shape_hash"] = None
        parsed = json.loads(canonical_audit_row(row).decode("utf-8"))
        assert parsed["ast_shape_hash"] is None

    def test_ast_shape_hash_bytes_serialises_as_hex(self) -> None:
        row = _baseline_row()
        row["ast_shape_hash"] = bytes.fromhex("bb" * 32)
        parsed = json.loads(canonical_audit_row(row).decode("utf-8"))
        assert parsed["ast_shape_hash"] == "bb" * 32

    def test_bytes_must_be_hashable_length_when_set(self) -> None:
        """A bytes value with the wrong byte width is almost certainly a
        caller bug (e.g. passing a hex string by accident). Refuse so
        the chain hash isn't computed over an unintended shape."""
        row = _baseline_row()
        row["fingerprint"] = b"\x00" * 16  # half-width
        with pytest.raises(ValueError, match="32 bytes"):
            canonical_audit_row(row)

    def test_non_bytes_for_binary_field_rejected(self) -> None:
        """Hex string accidentally passed where bytes are required is
        the easiest mistake at this seam — refuse loudly."""
        row = _baseline_row()
        row["fingerprint"] = "aa" * 32  # str, not bytes
        with pytest.raises(ValueError, match="must be bytes"):
            canonical_audit_row(row)


class TestStrictShape:
    def test_missing_field_rejected(self) -> None:
        row = _baseline_row()
        del row["tool_name"]
        with pytest.raises(ValueError, match="missing"):
            canonical_audit_row(row)

    def test_extra_field_rejected(self) -> None:
        row = _baseline_row()
        row["caller_email"] = "x@y.com"
        with pytest.raises(ValueError, match="unexpected"):
            canonical_audit_row(row)

    def test_none_for_required_string_rejected(self) -> None:
        row = _baseline_row()
        row["tool_name"] = None
        with pytest.raises(ValueError, match="tool_name"):
            canonical_audit_row(row)

    def test_fingerprint_none_rejected(self) -> None:
        """Fingerprint is NOT NULL in the DDL — refusing None here keeps
        the canonicaliser in sync with the SQL constraint."""
        row = _baseline_row()
        row["fingerprint"] = None
        with pytest.raises(ValueError, match="fingerprint"):
            canonical_audit_row(row)


class TestUnicodeStability:
    def test_non_ascii_strings_serialise_as_utf8_codepoints(self) -> None:
        """`ensure_ascii=False` is intentional — non-ASCII column names
        and identifiers hash as their actual UTF-8 codepoints rather than
        `\\uXXXX` escapes. Flipping this flag silently changes every
        chain hash for any non-ASCII content."""
        row = _baseline_row()
        row["tool_name"] = "find_relevant_tables_ä"
        out = canonical_audit_row(row)
        # The literal UTF-8 byte sequence for `ä` is 0xC3 0xA4.
        assert b"\xc3\xa4" in out
        # NOT the \\u-escape form.
        assert b"\\u00e4" not in out


class TestJsonShape:
    def test_serialised_form_is_one_json_object(self) -> None:
        out = canonical_audit_row(_baseline_row())
        parsed = json.loads(out.decode("utf-8"))
        assert isinstance(parsed, dict)

    def test_no_whitespace_in_serialised_form(self) -> None:
        """Drift in formatter whitespace would invalidate every previously
        chained row. Pin the no-whitespace separator contract."""
        out = canonical_audit_row(_baseline_row())
        # `json.dumps(..., separators=(",", ":"))` produces no ASCII
        # whitespace. UTF-8 multibyte sequences may contain bytes that
        # happen to look like whitespace; check via JSON re-parse rather
        # than substring search.
        assert b" " not in out
        assert b"\n" not in out
        assert b"\t" not in out
