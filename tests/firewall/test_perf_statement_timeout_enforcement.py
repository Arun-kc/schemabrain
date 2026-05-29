"""Verify `--statement-timeout-ms` actually bounds slow Postgres queries.

Background — IF-3: earlier versions of `schemabrain serve` shipped
with the timeout default OMITTED, making the firewall operationally
weak out of the box. The default is now 30000ms (30s) with `0` as the
explicit opt-out; these tests pin enforcement behaviour AT the
executor layer (independent of argparse defaults) so the cap
mechanics stay regression-tested even if defaults shift again in the
future.

We don't drive `get_metric` end-to-end (that requires a curated
semantic layer); instead we exercise the same `EngineMetricExecutor`
wiring that `cli.py::_cmd_serve` constructs, so any drift between the
executor's engine and the serve path's engine surfaces here.

Three scenarios:

  1. Timeout EXPLICITLY disabled (None at the executor) → a 3-second
     `pg_sleep` query completes normally. Pins the executor's
     "unbounded when None" contract — the CLI default is now 30000ms,
     and this scenario documents what passing `--statement-timeout-ms 0`
     ultimately resolves to inside the executor.
  2. Timeout = 1000ms → the same query aborts in ~1-2s with a
     `OperationalError` wrapped in `RuntimeError`.
  3. URL-param bypass attempt (`?options=-c statement_timeout=0`) is
     stripped by `safe_engine_url` so the operator-set timeout
     remains authoritative.
"""

from __future__ import annotations

import os
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from schemabrain.connectors._url import safe_engine_url
from schemabrain.mcp.metric_executor import EngineMetricExecutor

_PG_URL = os.environ.get(
    "DBURL",
    "postgresql+psycopg://postgres:local@127.0.0.1:5433/postgres",
)


def _make_executor(*, statement_timeout_ms: int | None) -> EngineMetricExecutor:
    options_parts = ["-c default_transaction_read_only=on"]
    if statement_timeout_ms is not None:
        options_parts.append(f"-c statement_timeout={statement_timeout_ms}")
    engine = create_engine(
        safe_engine_url(_PG_URL),
        poolclass=NullPool,
        connect_args={"options": " ".join(options_parts)},
    )
    return EngineMetricExecutor(engine)


@pytest.mark.integration
def test_executor_without_timeout_lets_slow_query_run() -> None:
    """Executor contract: when statement_timeout is None, a 3s pg_sleep
    COMPLETES normally.

    Pins the executor-internal "None means unbounded" invariant. The
    CLI surface defaults to 30000ms and exposes `0` as the explicit
    opt-out (which the cli.py wiring passes through to Postgres as
    `statement_timeout=0`, semantically equivalent to None here).
    """
    executor = _make_executor(statement_timeout_ms=None)
    t0 = time.perf_counter()
    rows = executor.execute("SELECT pg_sleep(3) AS s", {})
    elapsed = time.perf_counter() - t0
    # Query ran to completion → firewall did NOT bound the query.
    assert rows  # single row
    assert elapsed >= 2.8, (
        f"expected ~3s sleep without timeout; got {elapsed:.2f}s — "
        f"did Postgres apply an implicit timeout?"
    )


@pytest.mark.integration
def test_timeout_configured_aborts_slow_query() -> None:
    """Timeout = 1000ms aborts a 10s pg_sleep well inside budget."""
    executor = _make_executor(statement_timeout_ms=1000)
    t0 = time.perf_counter()
    with pytest.raises(RuntimeError) as exc_info:
        executor.execute("SELECT pg_sleep(10) AS s", {})
    elapsed = time.perf_counter() - t0
    # Postgres raises `canceling statement due to statement timeout`
    # which wraps as OperationalError → RuntimeError.
    msg = str(exc_info.value).lower()
    assert "statement timeout" in msg or "canceling" in msg or "operationalerror" in msg, (
        f"expected statement-timeout error; got: {exc_info.value!r}"
    )
    # Allow generous wall margin for connection setup; the bound itself
    # is well under 2 seconds in steady state.
    assert elapsed < 3.0, (
        f"timeout fired but took {elapsed:.2f}s wall — timeout may not be applied via connect_args"
    )


@pytest.mark.integration
def test_url_param_options_bypass_is_stripped() -> None:
    """Bypass attempt: `?options=-c statement_timeout=0` in the URL.

    `safe_engine_url` must strip the smuggled `options` query param so
    an agent that can influence the connection URL (e.g., env var
    injection upstream) cannot disable the operator's configured
    timeout.
    """
    hostile_url = _PG_URL + "?options=-c%20statement_timeout%3D0"
    safe = safe_engine_url(hostile_url)
    assert "statement_timeout" not in safe, (
        f"safe_engine_url did NOT strip statement_timeout from URL: {safe}"
    )
    assert "options=" not in safe, f"safe_engine_url left an `options` URL param in place: {safe}"


@pytest.mark.integration
def test_timeout_via_slow_view_bypass() -> None:
    """FW-010: a view whose definition embeds pg_sleep should STILL be
    bounded by statement_timeout because the timeout applies to the
    outer SELECT regardless of inner view expressions.

    Setup created `firewall_perf.slow_view AS SELECT pg_sleep(5)`. With a
    1s timeout, the call must abort in <3s wall.
    """
    executor = _make_executor(statement_timeout_ms=1000)
    t0 = time.perf_counter()
    with pytest.raises(RuntimeError) as exc_info:
        executor.execute("SELECT * FROM firewall_perf.slow_view", {})
    elapsed = time.perf_counter() - t0
    msg = str(exc_info.value).lower()
    assert "timeout" in msg or "canceling" in msg, (
        f"view-embedded pg_sleep did NOT honor statement_timeout. Error: {exc_info.value!r}"
    )
    assert elapsed < 3.0, (
        f"timeout fired but view query took {elapsed:.2f}s — FW-010 bypass "
        f"may exist on the view path."
    )
