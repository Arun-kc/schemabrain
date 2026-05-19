"""Strict env-var resolution for `SCHEMABRAIN_*` configuration knobs.

Shared seam used by every callsite that exposes a configuration knob
via an environment variable. Centralizes:

  - **Strict ASCII parsing** — rejects Python `int()` / `float()`
    footguns: underscore separators (`"1_000"`), fullwidth unicode
    digits (U+FF10..FF19, which Python silently folds to ASCII),
    leading zeros on the integer part (`"01"`), hex/octal prefixes,
    scientific notation, signed negatives. Any of these would
    otherwise silently produce a smaller/wrong value than the operator
    intended.

  - **One-shot stderr warning** when an env var is SET but empty.
    Silent fall-through would leave operators chasing why their
    override didn't take effect. Dedup'd per `(env_var)` so a
    repeated-resolution pattern (wizard calls per stage) doesn't
    flood logs.

  - **Two invalid-handling modes** via `on_invalid`:
      `"raise"`              — raise ValueError. Use when a wrong
                               operator-supplied value MUST not be
                               silently swallowed (e.g. `max_tokens`
                               — a wrong cap silently truncates LLM
                               responses; ANTHROPIC API rate-limits
                               — wrong concurrency triggers cascading
                               429s).
      `"warn_and_default"`   — print stderr warning + return default.
                               Use when a leftover env var shouldn't
                               abort an interactive run (e.g. wizard
                               cost-cap defaults where the wizard
                               itself has a sane per-stage fallback).

    The CLI's "render `GuidedError` + exit 2" pattern layers on top:
    callers use `"raise"` mode and translate the `ValueError` into
    the CLI-specific guided render.

History: factored out of `enrichment/anthropic_client.py` after the
2026-05-19 config-flexibility audit found 4+ near-duplicate
parser+warn-and-fallback paths across `cli.py` and `wizard.py`,
each using lax `float(env_var)` that silently accepted the same
footguns PR #67 hardened the int parser against.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Literal, TypeVar

__all__ = [
    "InvalidMode",
    "resolve_positive_float_env",
    "resolve_positive_int_env",
]

InvalidMode = Literal["raise", "warn_and_default"]

# ASCII decimal positive integer with optional leading `+`. Rejects:
# leading zeros (`"00100"`), underscore separators (`"1_000"`),
# fullwidth unicode digits (U+FF10..FF19 fold to ASCII via int()),
# hex/octal prefixes, decimals, signed negatives. Python's `int()`
# silently accepts most of those; this pattern makes the operator-facing
# contract explicit so a typo never silently becomes a smaller-than-
# default cap. Pinned via the test suite to match PR #67's regex.
_POSITIVE_INT_RE = re.compile(r"^\+?[1-9][0-9]*$")

# ASCII decimal positive float with optional leading `+`. Accepts:
#   "1", "1.0", "0.5", "+1.5", "10.0", ".5"
# Rejects:
#   "1_000.5" (underscore separator — Python's float() silently
#              accepts in some contexts and rejects in others; never
#              ambiguous here)
#   "1e3" / "1E3" / "1.5e-2" (scientific notation — overkill for
#              cost caps and easy to mistype)
#   "-1.0" / "-0.5" (negatives — operator probably meant positive)
#   "01" / "001.5" (leading zero on integer part != 0 — typo class)
#   fullwidth digits (U+FF10..FF19, which Python's float() folds
#              silently to ASCII)
#   "Infinity" / "NaN" / "inf" / "nan" (Python's float() accepts
#              these; we don't want them in a cost cap)
# The regex has two alternatives because a leading `.` (e.g. ".5") is
# a valid float shape that wouldn't fit the `(?:0|[1-9]\d*)` integer
# anchor of the main branch.
_POSITIVE_FLOAT_RE = re.compile(r"^\+?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$|^\+?\.[0-9]+$")

# Module-scoped set of env-var names we've already warned about for
# "set but empty". Keeps the warn to once per process per var rather
# than once per resolution call. Tests reset via
# `_reset_warned_empty_cache_for_tests()`.
_WARNED_EMPTY_ENV_VARS: set[str] = set()


def _reset_warned_empty_cache_for_tests() -> None:
    """Test-only seam — wipes the once-per-process warned-empty set so
    tests can re-trigger the warning in isolation without bleed-over.
    """
    _WARNED_EMPTY_ENV_VARS.clear()


T = TypeVar("T", int, float)


def _warn_empty_once(env_var: str, default: T, default_display: str | None) -> None:
    """Emit a one-shot stderr warning when `env_var` is set but empty."""
    if env_var in _WARNED_EMPTY_ENV_VARS:
        return
    _WARNED_EMPTY_ENV_VARS.add(env_var)
    display = default_display if default_display is not None else str(default)
    print(
        f"[schemabrain] warning: {env_var} is set but empty; using default {display}",
        file=sys.stderr,
    )


def _handle_invalid(
    msg: str,
    default: T,
    *,
    on_invalid: InvalidMode,
    default_display: str | None,
) -> T:
    """Either raise or warn-and-default per `on_invalid`. Centralized so
    both int and float resolvers route through the same branch and
    callers can switch the mode via one parameter."""
    if on_invalid == "raise":
        raise ValueError(msg)
    display = default_display if default_display is not None else str(default)
    print(
        f"[schemabrain] warning: {msg}; using default {display}",
        file=sys.stderr,
    )
    return default


def resolve_positive_int_env(
    env_var: str,
    default: int,
    *,
    on_invalid: InvalidMode = "raise",
    default_display: str | None = None,
) -> int:
    """Resolve a positive-integer config knob from an env var.

    Returns `default` when `env_var` is unset entirely. When set to an
    empty or whitespace-only string, emits a one-shot stderr warning
    and returns `default`. When set to a value, parses via strict
    ASCII regex (see `_POSITIVE_INT_RE`); invalid values either raise
    `ValueError` (`on_invalid="raise"`, default) or emit a stderr
    warning + return `default` (`on_invalid="warn_and_default"`).

    `default_display` overrides `str(default)` in warning messages.
    Useful when the default is more naturally rendered with a unit
    (e.g. `"$1.00"`, `"8 workers"`).
    """
    raw_value = os.environ.get(env_var)
    if raw_value is None:
        return default
    raw = raw_value.strip()
    if not raw:
        _warn_empty_once(env_var, default, default_display)
        return default
    if not _POSITIVE_INT_RE.match(raw):
        msg = (
            f"{env_var}={raw!r} is not a positive decimal integer "
            f"(no underscores, no leading zeros, no decimal point)"
        )
        return _handle_invalid(msg, default, on_invalid=on_invalid, default_display=default_display)
    return int(raw)


def resolve_positive_float_env(
    env_var: str,
    default: float,
    *,
    on_invalid: InvalidMode = "raise",
    default_display: str | None = None,
) -> float:
    """Resolve a positive-float config knob from an env var.

    Same shape as `resolve_positive_int_env`, parsing via
    `_POSITIVE_FLOAT_RE`. Also enforces `parsed > 0` after the regex
    pass because the regex accepts `"0"` / `"0.0"` syntactically
    (the integer-anchor `(?:0|[1-9]\\d*)` allows `0`); we reject
    those at the value layer.
    """
    raw_value = os.environ.get(env_var)
    if raw_value is None:
        return default
    raw = raw_value.strip()
    if not raw:
        _warn_empty_once(env_var, default, default_display)
        return default
    if not _POSITIVE_FLOAT_RE.match(raw):
        msg = (
            f"{env_var}={raw!r} is not a positive decimal number "
            f"(no underscores, no scientific notation, no leading zeros, no negatives)"
        )
        return _handle_invalid(msg, default, on_invalid=on_invalid, default_display=default_display)
    parsed = float(raw)
    if parsed <= 0:
        msg = f"{env_var}={raw!r} must be a positive number"
        return _handle_invalid(msg, default, on_invalid=on_invalid, default_display=default_display)
    return parsed
