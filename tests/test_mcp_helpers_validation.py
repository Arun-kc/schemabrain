"""Tests for identifier validation + bounded echo on MCP qualified-name
parsers.

Closes a context-budget-exhaustion attack vector: an agent passes a
giant attacker-controlled string as the qualified name, and the
parser's ValueError echoes the full string back into the agent's
context window. The fix is two-part:

1. `_validate_ident(part, role)` enforces Postgres identifier shape:
   length ≤ NAMEDATALEN-1 (63) and `^[A-Za-z_][A-Za-z0-9_$]*$`.
2. `_parse_qualified_name` / `_parse_column_qualified_name` truncate
   any echoed name to a bounded length using `repr()` so embedded
   control characters can't break out of the error string.

Trade-off documented: quoted Postgres identifiers like `"Order Items"`
and non-ASCII identifiers like `café` are rejected. Revisit when a
real user schema needs them.
"""

from __future__ import annotations

import pytest

from schemabrain.mcp._helpers import (
    _MAX_IDENT_LEN,
    _parse_column_qualified_name,
    _parse_qualified_name,
    _validate_ident,
)


class TestValidateIdent:
    """`_validate_ident` enforces Postgres identifier shape."""

    @pytest.mark.parametrize(
        "good",
        [
            "orders",
            "public",
            "users_v2",
            "_leading_underscore",
            "A",  # single char OK
            "a" * _MAX_IDENT_LEN,  # boundary: exactly 63 chars
            "col$dollar",  # $ allowed past first position
            "col123",  # digits OK past first position
        ],
    )
    def test_accepts_valid_postgres_identifiers(self, good: str) -> None:
        _validate_ident(good, role="schema")  # must not raise

    def test_rejects_empty_string(self) -> None:
        with pytest.raises(ValueError, match="schema"):
            _validate_ident("", role="schema")

    def test_rejects_over_length(self) -> None:
        too_long = "a" * (_MAX_IDENT_LEN + 1)
        with pytest.raises(ValueError, match="too long"):
            _validate_ident(too_long, role="table")

    def test_rejects_leading_digit(self) -> None:
        with pytest.raises(ValueError):
            _validate_ident("9orders", role="table")

    @pytest.mark.parametrize(
        "bad",
        [
            "has space",
            "semi;colon",
            "dash-name",
            "quote'name",
            "dot.in.name",
            "back\\slash",
            "tab\tinside",
            "newline\ninside",
            "unicodeé",  # é
        ],
    )
    def test_rejects_invalid_characters(self, bad: str) -> None:
        with pytest.raises(ValueError):
            _validate_ident(bad, role="column")

    def test_error_message_includes_role(self) -> None:
        """The `role` argument tells the agent WHICH part is bad
        (schema / table / column) when a multi-part name fails."""
        with pytest.raises(ValueError, match="column"):
            _validate_ident("9bad", role="column")


class TestParseQualifiedNameValidation:
    """`_parse_qualified_name` now runs each part through `_validate_ident`."""

    def test_happy_path_still_works(self) -> None:
        assert _parse_qualified_name("public.orders") == ("public", "orders")

    def test_rejects_oversize_schema(self) -> None:
        oversize = "a" * 200
        with pytest.raises(ValueError):
            _parse_qualified_name(f"{oversize}.orders")

    def test_rejects_oversize_table(self) -> None:
        oversize = "x" * 200
        with pytest.raises(ValueError):
            _parse_qualified_name(f"public.{oversize}")

    def test_rejects_invalid_chars_in_schema(self) -> None:
        with pytest.raises(ValueError):
            _parse_qualified_name("bad;schema.orders")

    def test_rejects_invalid_chars_in_table(self) -> None:
        with pytest.raises(ValueError):
            _parse_qualified_name("public.bad table")

    def test_missing_dot_error_is_bounded(self) -> None:
        # Adversarial: 100 KB qualified name with no dot. The error
        # echoes the INPUT — must be truncated so the agent's context
        # window doesn't explode.
        attack = "x" * 100_000
        with pytest.raises(ValueError) as exc_info:
            _parse_qualified_name(attack)
        # The message must NOT contain the full 100 KB payload.
        assert len(str(exc_info.value)) < 2_000

    def test_oversize_part_error_is_bounded(self) -> None:
        # Same attack via a "looks like schema.name" prefix but with a
        # giant schema part.
        attack = "a" * 100_000
        with pytest.raises(ValueError) as exc_info:
            _parse_qualified_name(f"{attack}.orders")
        assert len(str(exc_info.value)) < 2_000


class TestParseColumnQualifiedNameValidation:
    """`_parse_column_qualified_name` analogous validation."""

    def test_happy_path_still_works(self) -> None:
        assert _parse_column_qualified_name("public.orders.user_id") == (
            "public",
            "orders",
            "user_id",
        )

    def test_rejects_oversize_part(self) -> None:
        oversize = "a" * 200
        with pytest.raises(ValueError):
            _parse_column_qualified_name(f"public.orders.{oversize}")

    def test_rejects_invalid_char_in_column(self) -> None:
        with pytest.raises(ValueError):
            _parse_column_qualified_name("public.orders.bad-col")

    def test_missing_dots_error_is_bounded(self) -> None:
        attack = "x" * 100_000
        with pytest.raises(ValueError) as exc_info:
            _parse_column_qualified_name(attack)
        assert len(str(exc_info.value)) < 2_000

    def test_oversize_part_error_is_bounded(self) -> None:
        attack = "a" * 100_000
        with pytest.raises(ValueError) as exc_info:
            _parse_column_qualified_name(f"public.orders.{attack}")
        assert len(str(exc_info.value)) < 2_000
