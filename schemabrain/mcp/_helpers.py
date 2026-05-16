"""Private helpers shared across MCP tool implementations.

Not part of the public API — the leading underscore is intentional.
"""

from __future__ import annotations

import re
from typing import Literal, TypeVar

from pydantic import BaseModel

# Component role names referenced by `_validate_ident`. Pinned as a
# Literal so a maintainer who adds a fifth call site (e.g. "index"
# or "partition") gets a type error rather than a runtime parse
# error message with an unrecognised role. `"entity"` is used by
# the `describe_entity` name argument.
IdentRole = Literal["schema", "table", "column", "entity"]

# Char-to-token ratio for the token estimator. ~4 is the standard rough
# estimate for English text + JSON punctuation. Used uniformly across
# all tool responses so agent budget arithmetic stays consistent.
_CHARS_PER_TOKEN = 4

# Postgres NAMEDATALEN is 64 by default; usable identifier length is
# NAMEDATALEN-1. Enforcing this here bounds the size of anything an
# agent (or an attacker via a prompt-injected agent) can stuff into a
# qualified_name string and have echoed back through a ValueError.
_MAX_IDENT_LEN = 63

# Bound on echoed input in error messages. Three times _MAX_IDENT_LEN
# is enough headroom that a legitimate "schema.table.column" with all
# three parts at max length (3*63 + 2 dots = 191) fits, while still
# capping the malicious-input case at a few hundred bytes.
_MAX_ECHO_LEN = _MAX_IDENT_LEN * 3 + 2

# Postgres unquoted identifiers: start with letter or underscore,
# followed by letters/digits/underscores/$. Matches `psql`'s lexer
# for unquoted identifiers. Quoted identifiers (with embedded spaces,
# dots, etc.) are intentionally rejected; revisit when a real user
# schema needs them.
_IDENT_REGEX = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")

_M = TypeVar("_M", bound=BaseModel)


def _token_estimate_of(model: BaseModel) -> int:
    """Rough token count for a Pydantic payload, via JSON-length / 4."""
    serialized = model.model_dump_json()
    return max(1, len(serialized) // _CHARS_PER_TOKEN)


def _with_token_estimate(model: _M) -> _M:
    """Return a copy of `model` with `token_estimate` set to a fresh
    estimate of itself. Encapsulates the two-pass build (Pydantic
    frozen models leave no other clean path).

    Off-by-one is acceptable: the estimate is computed against a JSON
    blob where `token_estimate` was the placeholder `0`, then the final
    blob has the real value (1-3 more chars). At char/4 granularity
    this is at most a 1-token error on a rough estimate.
    """
    return model.model_copy(update={"token_estimate": _token_estimate_of(model)})


def _bounded_repr(value: str) -> str:
    """`repr()` of `value`, truncated to `_MAX_ECHO_LEN` characters of
    the underlying string before `repr` is applied.

    Used in error messages whenever a parser echoes an attacker-
    controllable input back. Truncation happens BEFORE `repr` so the
    final escaped form (with quotes + escape sequences) stays bounded
    too — `repr()` of a 100KB string with control characters can
    balloon well past the input size. The appended `"..."` literal is
    included in the `repr` call.
    """
    if len(value) <= _MAX_ECHO_LEN:
        return repr(value)
    return repr(value[:_MAX_ECHO_LEN] + "...")


def _validate_ident(part: str, *, role: IdentRole) -> None:
    """Validate one component of a qualified name.

    `role` is one of `"schema"`, `"table"`, `"column"` and appears in
    the raised ValueError so a multi-part-name failure points the agent
    at the specific bad component.

    Rejects:
    - empty strings
    - identifiers longer than `_MAX_IDENT_LEN` (Postgres NAMEDATALEN-1)
    - strings that don't match `^[A-Za-z_][A-Za-z0-9_$]*$`

    Defense purpose: bounds the size of any input echoed back
    through a ValueError, closing the context-budget-exhaustion
    attack vector (a malicious agent passing a 100KB qualified_name
    string and reading it back from the error message).
    """
    if not part:
        raise ValueError(f"{role} must not be empty")
    if len(part) > _MAX_IDENT_LEN:
        raise ValueError(
            f"{role} too long: {len(part)} chars (max {_MAX_IDENT_LEN}); got {_bounded_repr(part)}"
        )
    if not _IDENT_REGEX.match(part):
        raise ValueError(f"{role} must match [A-Za-z_][A-Za-z0-9_$]*, got {_bounded_repr(part)}")


def _parse_qualified_name(qualified_name: str) -> tuple[str, str]:
    """Split `"schema.name"` into `(schema, name)` and validate each
    part. Raises `ValueError` on malformed input.

    The error message includes recovery guidance so an LLM agent that
    passes a bare table name (`"orders"` instead of `"public.orders"`)
    learns the right next move from the message itself, rather than
    having to retry blind. Manual tests showed agents instinctively
    pass unqualified names; this nudge saves a turn per occurrence.

    All echoed input is bounded via `_bounded_repr` so an adversarial
    100KB qualified_name can't blow up the agent's context window
    through an unbounded ValueError echo.
    """
    parts = qualified_name.split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(
            f"qualified_name must be exactly `schema.name`, got "
            f"{_bounded_repr(qualified_name)}. "
            f"If you don't know the schema, call `find_relevant_tables` first to "
            f"discover the qualified name (e.g. `public.orders`)."
        )
    _validate_ident(parts[0], role="schema")
    _validate_ident(parts[1], role="table")
    return parts[0], parts[1]


def _parse_column_qualified_name(qualified_name: str) -> tuple[str, str, str]:
    """Split `"schema.table.column"` into `(schema, table, column)`
    and validate each part.

    Raises `ValueError` on malformed input — exactly two dots, all
    three parts non-empty, each part matching the identifier regex.
    We use plain `split` rather than rsplit tricks because Postgres
    identifiers don't allow embedded dots in standard usage; if a
    future schema needs that, the user will use quoting and we'll
    revisit.

    Same recovery-guidance pattern as `_parse_qualified_name` — the
    error tells the agent how to find the right qualified name.

    All echoed input is bounded via `_bounded_repr` so an adversarial
    100KB qualified_name can't blow up the agent's context window
    through an unbounded ValueError echo.
    """
    parts = qualified_name.split(".")
    if len(parts) != 3 or not all(parts):
        raise ValueError(
            f"qualified_name must be exactly `schema.table.column`, got "
            f"{_bounded_repr(qualified_name)}. "
            f"If you don't know the schema or table, call `find_relevant_tables` "
            f"or `describe_table` first to discover them (e.g. `public.orders.user_id`)."
        )
    _validate_ident(parts[0], role="schema")
    _validate_ident(parts[1], role="table")
    _validate_ident(parts[2], role="column")
    return parts[0], parts[1], parts[2]
