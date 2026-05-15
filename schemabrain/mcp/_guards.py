"""Pre-emptive defense-in-depth guards for the future SQL-execution
boundary (v2 `execute` / `validate_query`).

This module is built NOW so that the moment any tool accepts a string
that flows toward a SQL engine, the guard is already in place — not
bolted on later. Cross-references the Datadog Security Labs case
study on the archived Postgres MCP server, which demonstrated that
a "safety wrapper" applied late is structurally weaker than rejecting
dangerous shapes at the type/parser boundary.

CRITICAL DISCIPLINE: every check below is tagged
`# DEFENSE-IN-DEPTH ONLY — never the primary control`. The PRIMARY
controls at the execution boundary will be:

  - typed `CompiledQuery` tokens issued by the compiler, not strings
  - parameterised execution; identifiers from a closed set
  - single-statement driver enforcement
  - read-only role on the source database

This guard is *belt-and-braces*: if any primary slips through review
or regresses, the guard buys one more refusal before raw SQL ever
reaches Postgres. It is NEVER a substitute for parameterisation or a
real SQL parser — those are the actual defences.

Today, no MCP tool calls `forbid_sql`. The module exists so the PR
that introduces `execute(handle: CompiledQuery)` can wire this in on
day one without a parallel "design the guard" sub-task.
"""

from __future__ import annotations

import re


class ForbiddenSQLError(ValueError):
    """Raised when `forbid_sql` rejects a candidate string.

    Subclass of `ValueError` so existing catch sites that handle
    `ValueError` generically pick this up without a separate import.
    The v2 executor will catch this and surface it as a refused
    `ToolResponse` envelope.
    """


# Characters / digraphs that are SQL-meaningful and have no place in
# any agent-supplied input at the v2 boundary. Tuples are
# (substring, name-for-error-message).
_FORBIDDEN_SUBSTRINGS: tuple[tuple[str, str], ...] = (
    (";", "semicolon"),  # DEFENSE-IN-DEPTH ONLY — never the primary control
    ("--", "line comment"),  # DEFENSE-IN-DEPTH ONLY — never the primary control
    ("/*", "block comment opener"),  # DEFENSE-IN-DEPTH ONLY — never the primary control
    ("*/", "block comment closer"),  # DEFENSE-IN-DEPTH ONLY — never the primary control
    ("'", "single quote"),  # DEFENSE-IN-DEPTH ONLY — never the primary control
)

# Keywords that have no business appearing as standalone words in
# anything an agent supplies at the v2 boundary. Word-boundary
# matched so `subset_id` (innocent identifier) doesn't trigger but
# `BEGIN` (transaction escape attempt) does.
_FORBIDDEN_KEYWORDS: frozenset[str] = frozenset(
    {
        # Transaction-control keywords — V3 vector (read-only escape):
        "BEGIN",
        "COMMIT",
        "ROLLBACK",
        "SAVEPOINT",
        "SET",
        # DDL keywords — V8 vector (schema mutation):
        "DROP",
        "ALTER",
        "CREATE",
        "TRUNCATE",
        "GRANT",
        "REVOKE",
    }
)

# Compiled once at import. `\b` is a word boundary so `subset` doesn't
# match `set` but `set =` does. `re.IGNORECASE` for case-insensitive
# Postgres keyword matching. Underscore is included as a word
# character by `\b`, so `rollback_table` keeps "rollback" inside an
# identifier and survives.
_KEYWORD_REGEX = re.compile(
    r"\b(" + "|".join(re.escape(kw) for kw in _FORBIDDEN_KEYWORDS) + r")\b",
    flags=re.IGNORECASE,
)

# Bound on input echoed in error messages — same rationale as
# `_helpers._MAX_ECHO_LEN` (context-budget exhaustion via giant input).
_MAX_ECHO_LEN = 200


def _bounded(s: str) -> str:
    if len(s) <= _MAX_ECHO_LEN:
        return repr(s)
    return repr(s[:_MAX_ECHO_LEN] + "...")


def forbid_sql(s: str) -> None:
    """Reject strings that contain SQL-meaningful constructs.

    Raises `ForbiddenSQLError` on first violation; the message names
    the construct that fired (`"semicolon"`, `"line comment"`,
    transaction-keyword name, etc.) so reviewers can debug rejection
    without re-running with verbose logging.

    DOES NOT validate that the input is "safe SQL" — that's not what
    this guard is. It rejects characters and keywords that have no
    legitimate place in agent-supplied text at the v2 tool boundary.
    The primary controls (typed handles, parameterisation, single-
    statement enforcement, read-only role) remain authoritative.
    """
    for substring, name in _FORBIDDEN_SUBSTRINGS:
        if substring in s:  # DEFENSE-IN-DEPTH ONLY — never the primary control
            raise ForbiddenSQLError(
                f"input contains forbidden construct ({name}): {_bounded(s)}"
            )
    # DEFENSE-IN-DEPTH ONLY — never the primary control
    keyword_match = _KEYWORD_REGEX.search(s)
    if keyword_match:
        raise ForbiddenSQLError(
            f"input contains forbidden keyword "
            f"({keyword_match.group(1).upper()}): {_bounded(s)}"
        )
