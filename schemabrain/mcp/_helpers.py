"""Private helpers shared across MCP tool implementations.

Not part of the public API — the leading underscore is intentional.
"""

from __future__ import annotations

import re
from typing import Literal, TypeVar

from pydantic import BaseModel

# Component role names referenced by `_validate_ident`. Pinned as a
# Literal so a maintainer who adds a new call site gets a type error
# rather than a runtime parse error message with an unrecognised role.
# `"entity"` is used by the `describe_entity` name argument;
# `"entity_a"`, `"entity_b"`, and `"name"` are used by the
# `resolve_join` arg surface (three positional/keyword args that
# each need identifier-shape validation).
IdentRole = Literal["schema", "table", "column", "entity", "entity_a", "entity_b", "name"]

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
# for unquoted identifiers. SQL-standard double-quoted identifiers
# (`"my name"`, `"Percent (%) Eligible"`) are also supported via the
# parser helpers below — embedded `"` escapes as `""`. The unquoted
# value still has to satisfy `_MAX_IDENT_LEN` (NAMEDATALEN-1).
_IDENT_REGEX = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")

# Control-character denylist for the quoted-identifier path. Postgres
# permits these bytes at the catalog layer (a `CREATE TABLE` with a
# control char in a quoted name succeeds), but the MCP firewall
# refuses to echo them into agent-rendered surfaces — terminal UIs
# that pipe MCP responses to stdout would interpret embedded ANSI
# escape sequences (`\x1b[`) live, hiding payload after a colour or
# screen-clear directive. The denylist covers C0 controls (`\x00`
# through `\x1f`, includes NUL/BEL/TAB/LF/CR/ESC) plus DEL (`\x7f`).
# Unicode line/paragraph separators (U+2028, U+2029) and C1
# controls (`\x80`-`\x9f`) are deferred to a v0.5+ broadening of the
# denylist; the current set closes every audit-corpus payload.
_CONTROL_CHAR_REGEX = re.compile(r"[\x00-\x1f\x7f]")

# Upper bound on the raw qualified_name input length before parsing.
# Worst legitimate 3-part input with all parts at NAMEDATALEN-1 and
# every char doubled by `""` escaping: 3 * (2 + 63*2) + 2 dots = 386.
# Round up for headroom. Anything bigger is rejected immediately so
# the parser doesn't walk a 100KB attacker payload.
_MAX_QUALIFIED_NAME_LEN = 512

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


def _split_qualified_segments(qualified_name: str) -> list[tuple[str, bool]]:
    """Tokenize a dotted qualified name into `(value, was_quoted)` pairs.

    Each segment is either:
      - Unquoted: matches `_IDENT_REGEX` (existing strict shape).
      - Quoted:   wrapped in `"`, embedded `"` escapes as `""` (SQL std).

    Returns the unquoted/unescaped value plus a flag so the caller
    can branch validation. Caller is responsible for length and
    identifier-shape checks per segment role.

    Raises `ValueError` on:
      - empty input
      - input exceeding `_MAX_QUALIFIED_NAME_LEN`
      - unterminated quoted segment
      - trailing `.` with no following segment
      - content after a closing quote that isn't a `.` separator
    """
    if not qualified_name:
        raise ValueError("qualified_name must not be empty")
    if len(qualified_name) > _MAX_QUALIFIED_NAME_LEN:
        raise ValueError(
            f"qualified_name too long: {len(qualified_name)} chars "
            f"(max {_MAX_QUALIFIED_NAME_LEN}); got {_bounded_repr(qualified_name)}"
        )

    segments: list[tuple[str, bool]] = []
    n = len(qualified_name)
    i = 0
    while True:
        if i >= n:
            # Reached end immediately after a `.` consumed below.
            raise ValueError(
                f"trailing `.` with no following segment in {_bounded_repr(qualified_name)}"
            )
        if qualified_name[i] == '"':
            # Quoted segment. Walk to matching closing quote, treating
            # `""` as an escape for a literal `"`.
            i += 1
            buf: list[str] = []
            closed = False
            while i < n:
                if qualified_name[i] == '"':
                    if i + 1 < n and qualified_name[i + 1] == '"':
                        buf.append('"')
                        i += 2
                    else:
                        i += 1
                        closed = True
                        break
                else:
                    buf.append(qualified_name[i])
                    i += 1
            if not closed:
                raise ValueError(
                    f"unterminated quoted identifier in {_bounded_repr(qualified_name)}"
                )
            segments.append(("".join(buf), True))
        else:
            # Unquoted segment. Read up to next `.` or end-of-input.
            start = i
            while i < n and qualified_name[i] != ".":
                i += 1
            segments.append((qualified_name[start:i], False))

        # Now i is at a separator dot or end-of-input.
        if i >= n:
            return segments
        if qualified_name[i] != ".":
            # Content after a closing quote that isn't a dot:
            # `"foo"bar.col` — malformed.
            raise ValueError(
                f"expected `.` between segments at offset {i} in {_bounded_repr(qualified_name)}"
            )
        i += 1  # consume the dot; loop expects another segment


def _validate_parsed_segment(value: str, *, was_quoted: bool, role: IdentRole) -> None:
    """Validate one parsed segment with its quoting metadata.

    Unquoted segments must still satisfy `_IDENT_REGEX`. Quoted
    segments may carry any PRINTABLE content but face three further
    constraints: the NAMEDATALEN-1 length cap, the non-empty
    requirement (Postgres rejects `""` as an identifier; so do we),
    and a control-character denylist that closes the ANSI-escape
    smuggling vector — even a Postgres column whose catalog name
    legitimately carries control bytes is refused at this boundary
    because echoing the name into a `ColumnDetail` response would
    reflow the operator's terminal.
    """
    if not value:
        raise ValueError(f"{role} must not be empty")
    if len(value) > _MAX_IDENT_LEN:
        raise ValueError(
            f"{role} too long: {len(value)} chars (max {_MAX_IDENT_LEN}); "
            f"got {_bounded_repr(value)}"
        )
    # Control-char check fires on BOTH quoted and unquoted paths. The
    # unquoted path also enforces `_IDENT_REGEX` (which would reject
    # control chars anyway), so this is redundant defense-in-depth
    # there but the canonical gate for the quoted path. Run BEFORE
    # the regex check so the more specific error message fires for
    # the more specific failure mode.
    if _CONTROL_CHAR_REGEX.search(value):
        raise ValueError(
            f"{role} contains invalid control character; "
            f"got {_bounded_repr(value)}. "
            f"Identifiers cannot carry C0 control bytes (\\x00-\\x1f) or "
            f"DEL (\\x7f) even inside a double-quoted segment."
        )
    if not was_quoted and not _IDENT_REGEX.match(value):
        raise ValueError(
            f"unquoted {role} must match [A-Za-z_][A-Za-z0-9_$]*, "
            f"got {_bounded_repr(value)}. "
            f'Wrap the name in double quotes (e.g. "my name") to use a '
            f"non-identifier-shaped name."
        )


def _parse_qualified_name(qualified_name: str) -> tuple[str, str]:
    """Split `"schema.name"` into `(schema, name)` and validate each
    part. Raises `ValueError` on malformed input.

    Supports both unquoted (`public.orders`) and SQL-standard
    double-quoted (`public."Order Items"`) segments. Embedded `"`
    inside a quoted segment escapes as `""`.

    The error message includes recovery guidance so an LLM agent that
    passes a bare table name (`"orders"` instead of `"public.orders"`)
    learns the right next move from the message itself.

    All echoed input is bounded via `_bounded_repr` so an adversarial
    100KB qualified_name can't blow up the agent's context window
    through an unbounded ValueError echo.
    """
    try:
        segments = _split_qualified_segments(qualified_name)
    except ValueError as e:
        raise ValueError(
            f"qualified_name must be `schema.name` (with optional double-"
            f'quoting for non-identifier-shaped parts, e.g. public."weird name"), '
            f"got {_bounded_repr(qualified_name)}. {e}. "
            f"If you don't know the schema, call `find_relevant_tables` first to "
            f"discover the qualified name (e.g. `public.orders`)."
        ) from e
    if len(segments) != 2:
        raise ValueError(
            f"qualified_name must be exactly `schema.name`, got "
            f"{len(segments)} segment(s) in {_bounded_repr(qualified_name)}. "
            f"If you don't know the schema, call `find_relevant_tables` first to "
            f"discover the qualified name (e.g. `public.orders`)."
        )
    schema_val, schema_quoted = segments[0]
    name_val, name_quoted = segments[1]
    try:
        _validate_parsed_segment(schema_val, was_quoted=schema_quoted, role="schema")
        _validate_parsed_segment(name_val, was_quoted=name_quoted, role="table")
    except ValueError as e:
        # Re-wrap so every error from this parser carries the
        # `qualified_name` discriminator — callers (and tests) match
        # on that word to distinguish parser errors from store errors.
        raise ValueError(
            f"qualified_name segment validation failed for {_bounded_repr(qualified_name)}: {e}"
        ) from e
    return schema_val, name_val


def _parse_column_qualified_name(qualified_name: str) -> tuple[str, str, str]:
    """Split `"schema.table.column"` into `(schema, table, column)`
    and validate each part.

    Supports SQL-standard double-quoting per segment. Real-world
    driver: any column whose name contains spaces, `%`, parens,
    hyphens, or other non-identifier chars — e.g. BIRD California
    Schools' `Percent (%) Eligible Free (K-12)`.

    Same recovery-guidance pattern as `_parse_qualified_name`.

    All echoed input is bounded via `_bounded_repr` so an adversarial
    100KB qualified_name can't blow up the agent's context window.
    """
    try:
        segments = _split_qualified_segments(qualified_name)
    except ValueError as e:
        raise ValueError(
            f"qualified_name must be `schema.table.column` (with optional "
            f"double-quoting for non-identifier-shaped parts, e.g. "
            f'public.frpm."Percent (%) Eligible Free (K-12)"), got '
            f"{_bounded_repr(qualified_name)}. {e}. "
            f"If you don't know the schema or table, call `find_relevant_tables` "
            f"or `describe_table` first to discover them (e.g. `public.orders.user_id`)."
        ) from e
    if len(segments) != 3:
        raise ValueError(
            f"qualified_name must be exactly `schema.table.column`, got "
            f"{len(segments)} segment(s) in {_bounded_repr(qualified_name)}. "
            f"If you don't know the schema or table, call `find_relevant_tables` "
            f"or `describe_table` first to discover them (e.g. `public.orders.user_id`)."
        )
    schema_val, schema_quoted = segments[0]
    table_val, table_quoted = segments[1]
    column_val, column_quoted = segments[2]
    try:
        _validate_parsed_segment(schema_val, was_quoted=schema_quoted, role="schema")
        _validate_parsed_segment(table_val, was_quoted=table_quoted, role="table")
        _validate_parsed_segment(column_val, was_quoted=column_quoted, role="column")
    except ValueError as e:
        # Re-wrap so every error from this parser carries the
        # `qualified_name` discriminator (same contract as the 2-part
        # parser; callers and tests match on that word).
        raise ValueError(
            f"qualified_name segment validation failed for {_bounded_repr(qualified_name)}: {e}"
        ) from e
    return schema_val, table_val, column_val
