"""Metric execution boundary.

`MetricExecutor` is the Protocol the `get_metric` tool talks to when
it wants to run parameterised SQL against the source database. The
Protocol exists so:

  - the unit-test suite can inject a deterministic stub executor that
    returns canned rows, without spinning up a real database;

  - the production path (`schemabrain serve`) wires a SQLAlchemy-backed
    `EngineMetricExecutor` whose engine is created with
    `default_transaction_read_only=on` (same posture as the profiler
    engine introduced in PR #9 — the read-only role is the canonical
    enforcement layer, the connect-arg is defense-in-depth).

The Protocol returns `list[dict[str, Any]]` — one row per dict, keyed
by column alias. SQLAlchemy `Result.mappings()` produces exactly this
shape; the executor adapter normalises any returned type into plain
dicts so downstream code never depends on SQLAlchemy types.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.engine import Engine

_logger = logging.getLogger(__name__)


class MetricExecutor(Protocol):
    """Run a parameterised SELECT and return the rows as dicts.

    Implementations MUST treat the SQL as opaque — the compiler is the
    only source of `sql_text` + `params` passed in; identifier
    interpolation has already been done safely there. The executor
    just binds parameters and ships the result.

    Implementations MUST raise `RuntimeError` (or a subclass) on any
    database error so the `get_metric` tool can catch a stable
    exception type and surface a Charter-v1.1 `internal_error`
    envelope.

    Implementations SHOULD enforce read-only access at the connection
    layer (e.g., `default_transaction_read_only=on`); the Protocol
    doesn't require this but every production implementation does.
    """

    def execute(  # pragma: no cover — Protocol method body; concrete impls are tested
        self,
        sql_text: str,
        params: dict[str, Any],
    ) -> list[dict[str, Any]]: ...


class EngineMetricExecutor:
    """SQLAlchemy-backed `MetricExecutor`.

    Holds a long-lived `Engine` (one connection pool for the lifetime
    of the MCP server). Each `execute` call opens a fresh transaction,
    runs the SELECT, and returns the rows as dicts.

    `max_rows` (optional) caps the returned list size — the SELECT
    still executes in full at the source DB (no `LIMIT` is appended
    server-side; the cap is a payload-size guard, not a query-cost
    guard). When None, no cap is applied. The wizard's MCP `serve`
    surface exposes this via `--max-rows-per-result`. Pair with
    `statement_timeout` (injected at engine-construct time via
    `connect_args`) for the full payload + runtime envelope.

    Defensive: errors from the DB layer wrap as `RuntimeError` so
    callers can branch on one exception type. Bare SQLAlchemy errors
    would leak ORM concepts into the tool layer.
    """

    def __init__(self, engine: Engine, *, max_rows: int | None = None) -> None:
        self._engine = engine
        self._max_rows = max_rows

    def execute(
        self,
        sql_text: str,
        params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        try:
            with self._engine.connect() as conn:
                result = conn.execute(text(sql_text), params)
                # `Result.mappings()` yields read-only RowMapping views;
                # `dict()` materialises to a plain dict so downstream
                # JSON-encoding doesn't depend on SQLAlchemy types.
                rows = [dict(row) for row in result.mappings()]
        except Exception as exc:
            # Log the original type before wrapping so triage can
            # distinguish a compiler bug (`ProgrammingError`) from a
            # transient connection failure (`OperationalError`) — the
            # wrapped `RuntimeError` upstream loses that distinction.
            _logger.error(
                "metric executor failed: %s: %s",
                type(exc).__name__,
                exc,
                exc_info=exc,
            )
            raise RuntimeError(f"metric query failed ({type(exc).__name__}): {exc}") from exc

        if self._max_rows is not None and len(rows) > self._max_rows:
            # Clip the payload but log the truncation so the operator
            # can see in the events stream that the cap fired (vs.
            # the agent silently getting fewer rows than the SQL
            # would have produced). The agent itself sees only the
            # truncated list — no envelope-level "truncated" flag
            # today; that's a v0.5 surface decision.
            _logger.warning(
                "metric executor truncated result: %d rows -> %d (--max-rows-per-result)",
                len(rows),
                self._max_rows,
            )
            return rows[: self._max_rows]
        return rows
