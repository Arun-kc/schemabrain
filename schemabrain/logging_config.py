"""Logging configuration for SchemaBrain.

Two execution surfaces, one conflicting constraint:

  - **CLI mode** (`schemabrain index|eval|fixture-path`) — stderr carries
    the Rich progress bar and the guided error blocks; logs need to
    coexist without garbling them.
  - **MCP-stdio mode** (`schemabrain serve`) — stdout is the JSON-RPC
    wire. ANY byte written to stdout corrupts the protocol. Logs MUST
    go to stderr.

Both reduce to one rule: **always log to stderr, never to stdout**. This
module enforces that with a single `StreamHandler(sys.stderr)` on the
`schemabrain` logger. `propagate=False` keeps the root logger's
`lastResort` handler from double-emitting.

Verbosity ladder:
  - default → WARNING
  - `-v`    → INFO
  - `-vv`+  → DEBUG

For MCP-stdio there's no `-v` flag, so set `SCHEMABRAIN_LOG_LEVEL`
(any of DEBUG / INFO / WARNING / ERROR / CRITICAL, case-insensitive)
in the Claude Desktop config or your shell. The env var is also a
fallback in CLI mode; the explicit flag always wins.

Third-party loggers (`mcp`, `anyio`, `httpx`, `httpcore`, `fastembed`)
are pinned at WARNING regardless of our level so `-vv` doesn't drown
the user in SDK chatter.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import TextIO

_PACKAGE_LOGGER = "schemabrain"
_HANDLER_NAME = "schemabrain.stderr"
_LOG_FORMAT = "[%(levelname)s] %(name)s: %(message)s"
_ENV_VAR = "SCHEMABRAIN_LOG_LEVEL"


class _LiveStderrHandler(logging.StreamHandler):
    """`StreamHandler` that re-resolves `sys.stderr` per emit.

    Stdlib's `StreamHandler.__init__` captures the stream once at
    construction time. That breaks under pytest's `capfd` (which
    redirects `sys.stderr` after our config runs) and under any
    test/runtime that swaps `sys.stderr` post-bootstrap. Mirrors the
    private `logging._StderrHandler` pattern from cpython.
    """

    def __init__(self) -> None:  # type: ignore[override]
        super().__init__(stream=None)  # base initializes to sys.stderr

    @property
    def stream(self) -> TextIO:  # type: ignore[override]
        return sys.stderr

    @stream.setter
    def stream(self, value: TextIO | None) -> None:  # type: ignore[override]
        # No-op. The base class assigns in __init__; we always defer
        # to whatever `sys.stderr` currently is.
        pass


# Standard logging level names we accept from the env var. Sourced from
# the stdlib's authoritative mapping (Python 3.11+) so a future stdlib
# addition automatically flows through. NOTSET is excluded — it means
# "inherit from parent", not "be quiet", and would silently no-op.
_VALID_ENV_LEVELS: frozenset[str] = frozenset(logging.getLevelNamesMapping().keys()) - {"NOTSET"}

# Third-party loggers we explicitly hold at WARNING. The list covers
# the dependencies that today produce verbose INFO/DEBUG output in
# practice. Add to this list if a new dep starts logging noisily.
_THIRD_PARTY_QUIETED = ("mcp", "anyio", "httpx", "httpcore", "fastembed")


def configure_logging(*, verbosity: int = 0) -> None:
    """Configure SchemaBrain's stderr-only logging.

    `verbosity`:
        0 → WARNING (default), 1 → INFO, 2+ → DEBUG. Higher numbers
        clamp to DEBUG — `-vvvvv` should not raise, it should just stay
        as loud as possible.

    Resolution order when `verbosity == 0`:
        1. `SCHEMABRAIN_LOG_LEVEL` env var (DEBUG/INFO/WARNING/ERROR/
           CRITICAL, case-insensitive) — used for MCP-stdio mode where
           the operator can't pass CLI flags.
        2. WARNING.

    When `verbosity >= 1`, the flag always wins over the env var.

    Idempotent: a second call replaces our handler in place rather
    than stacking. Other handlers added by the operator are preserved.
    """
    level = _resolve_level(verbosity)

    pkg_logger = logging.getLogger(_PACKAGE_LOGGER)
    # Remove only the handler we previously attached. Operators may have
    # added their own with a different `name` — leave those alone.
    for h in list(pkg_logger.handlers):
        if getattr(h, "name", None) == _HANDLER_NAME:
            pkg_logger.removeHandler(h)

    handler = _LiveStderrHandler()
    handler.name = _HANDLER_NAME
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    handler.setLevel(level)

    pkg_logger.addHandler(handler)
    pkg_logger.setLevel(level)
    # Don't propagate to root — root has its own lastResort handler
    # that would double-emit on stderr.
    pkg_logger.propagate = False

    for name in _THIRD_PARTY_QUIETED:
        logging.getLogger(name).setLevel(logging.WARNING)


def _resolve_level(verbosity: int) -> int:
    if verbosity >= 2:
        return logging.DEBUG
    if verbosity == 1:
        return logging.INFO
    env = os.environ.get(_ENV_VAR, "").strip().upper()
    if env in _VALID_ENV_LEVELS:
        # `getLevelNamesMapping` returns the int directly — preferred over
        # `getattr(logging, env)` because it accepts only canonical level
        # names (no accidental `logging.NOTSET` style typos).
        return logging.getLevelNamesMapping()[env]
    if env:
        # A user with no other feedback path (Claude Desktop env config) is
        # silently typo-stuck without this warning. One line, before the
        # handler is attached — straight to stderr.
        print(
            f"[schemabrain] WARNING: unrecognised {_ENV_VAR}={env!r}; "
            f"falling back to WARNING. Valid: "
            f"{', '.join(sorted(_VALID_ENV_LEVELS))}",
            file=sys.stderr,
        )
    return logging.WARNING


__all__ = ["configure_logging"]
