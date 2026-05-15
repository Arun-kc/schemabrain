"""Private helpers shared across MCP tool implementations.

Not part of the public API — the leading underscore is intentional.
"""

from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel

# Char-to-token ratio for the token estimator. ~4 is the standard rough
# estimate for English text + JSON punctuation. Used uniformly across
# all tool responses so agent budget arithmetic stays consistent.
_CHARS_PER_TOKEN = 4

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


def _parse_qualified_name(qualified_name: str) -> tuple[str, str]:
    """Split `"schema.name"` into `(schema, name)`. Raises `ValueError`
    on malformed input — exactly one dot, both parts non-empty.

    The error message includes recovery guidance so an LLM agent that
    passes a bare table name (`"orders"` instead of `"public.orders"`)
    learns the right next move from the message itself, rather than
    having to retry blind. Manual tests showed agents instinctively
    pass unqualified names; this nudge saves a turn per occurrence.
    """
    parts = qualified_name.split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(
            f"qualified_name must be exactly `schema.name`, got {qualified_name!r}. "
            f"If you don't know the schema, call `find_relevant_tables` first to "
            f"discover the qualified name (e.g. `public.orders`)."
        )
    return parts[0], parts[1]


def _parse_column_qualified_name(qualified_name: str) -> tuple[str, str, str]:
    """Split `"schema.table.column"` into `(schema, table, column)`.

    Raises `ValueError` on malformed input — exactly two dots, all
    three parts non-empty. We use plain `split` rather than rsplit
    tricks because Postgres identifiers don't allow embedded dots in
    standard usage; if a future schema needs that, the user will use
    quoting and we'll revisit.

    Same recovery-guidance pattern as `_parse_qualified_name` — the
    error tells the agent how to find the right qualified name.
    """
    parts = qualified_name.split(".")
    if len(parts) != 3 or not all(parts):
        raise ValueError(
            f"qualified_name must be exactly `schema.table.column`, got {qualified_name!r}. "
            f"If you don't know the schema or table, call `find_relevant_tables` "
            f"or `describe_table` first to discover them (e.g. `public.orders.user_id`)."
        )
    return parts[0], parts[1], parts[2]
