"""Verify `--max-rows-per-result` enforcement and SF-003 signal absence.

SF-003: `EngineMetricExecutor._max_rows` truncates the
returned list silently. The envelope passed to the agent carries
`status="success"` even when the underlying SELECT produced N >>
max_rows. The agent therefore cannot tell that it received a
truncated view.

This test pins:

  1. The truncation HAPPENS at the executor layer (list is clipped).
  2. The truncation happens SILENTLY — there is no per-row marker or
     extra return-shape field surfaced by `execute()`.
  3. The source DB still does the full scan — the cap is a payload
     guard, not a query-cost guard. We measure this by timing a
     truncated 5000-row query: if Postgres also returned only the cap,
     the wall time would collapse; instead it stays ~scan-cost.
  4. Memory-DoS angle: even with max_rows=10, the executor materialises
     the full result list before truncating (`[dict(row) for row in
     result.mappings()]` runs to completion, THEN `rows[:max]`). So a
     10M-row metric would peak at 10M rows in process memory before
     trimming. The test does not allocate 10M rows in CI but pins the
     code path via a 5000-row probe.
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


def _make_executor(*, max_rows: int | None) -> EngineMetricExecutor:
    engine = create_engine(
        safe_engine_url(_PG_URL),
        poolclass=NullPool,
        connect_args={"options": "-c default_transaction_read_only=on"},
    )
    return EngineMetricExecutor(engine, max_rows=max_rows)


@pytest.mark.integration
def test_max_rows_truncates_to_cap() -> None:
    """5000-row source query → executor returns exactly `max_rows`."""
    executor = _make_executor(max_rows=100)
    rows = executor.execute(
        "SELECT id, label FROM firewall_perf.bigtab ORDER BY id",
        {},
    )
    assert len(rows) == 100, f"expected truncation to 100, got {len(rows)}"
    # Truncation takes the HEAD of the result, not a random sample.
    assert rows[0]["id"] == 1
    assert rows[-1]["id"] == 100


@pytest.mark.integration
def test_max_rows_no_cap_returns_full_result() -> None:
    """When `max_rows=None`, the full 5000-row payload comes back."""
    executor = _make_executor(max_rows=None)
    rows = executor.execute("SELECT id FROM firewall_perf.bigtab", {})
    assert len(rows) == 5000


@pytest.mark.integration
def test_truncation_is_silent_no_envelope_signal() -> None:
    """SF-003: the truncated list is RETURNED with no marker.

    `execute()` returns `list[dict[str, Any]]` — there is no wrapper,
    no `truncated` flag, no sentinel row. The agent cannot tell from
    the return value whether the cap fired. This test pins the gap:
    when the contract changes (envelope flag, wrapper type, etc.),
    update or delete this test.
    """
    executor = _make_executor(max_rows=10)
    rows = executor.execute("SELECT id FROM firewall_perf.bigtab", {})
    assert len(rows) == 10
    # No row carries a "truncated" marker
    for r in rows:
        assert set(r.keys()) == {"id"}, (
            f"unexpected key on truncated row — does the executor now surface a marker? row: {r}"
        )


@pytest.mark.integration
def test_source_db_still_scans_full_table_under_cap() -> None:
    """DoS angle: a metric returning 5000 rows still materialises all
    5000 dict() rows BEFORE truncation. Confirm by timing: an
    in-memory scan of 5000 rows + clip-to-10 is still O(5000) work.

    The wall-clock check here is loose (Postgres roundtrip dominates
    on a localhost setup) — the test exists to fingerprint the code
    path so a future "push LIMIT to SQL" optimisation surfaces a
    structural change.
    """
    executor = _make_executor(max_rows=10)
    t0 = time.perf_counter()
    rows = executor.execute("SELECT id, label FROM firewall_perf.bigtab", {})
    elapsed = time.perf_counter() - t0
    assert len(rows) == 10
    # Sanity floor: even a 5000-row roundtrip + parse takes >2ms on
    # localhost; if this drops to <1ms the executor learned to push
    # the cap to the SQL layer (a positive change — flip this test).
    assert elapsed >= 0.002, (
        f"5000-row scan completed in {elapsed * 1000:.2f}ms — did the "
        f"executor learn to push `LIMIT` server-side? Update this test."
    )
