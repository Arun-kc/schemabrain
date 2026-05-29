"""Shared fixtures for the firewall bypass corpus.

These tests target SchemaBrain's MCP firewall — the boundary that
sits between an LLM agent and a Postgres source database. Each test
proves (or refutes) one bypass surfaced during the firewall audit
by exercising the actual MCP tool implementations against a live
Postgres + a real `SQLiteStore`.

Test marks:
  - ``integration``     — requires a live Postgres on 127.0.0.1:5433
                          (the `sb-demo-pg` Docker container).
  - ``firewall_bypass`` — selects only this corpus
                          (``-m firewall_bypass``).

Run all bypass tests with:

    uv run pytest tests/firewall/ -m firewall_bypass

Run a single attack:

    uv run pytest tests/firewall/test_fw_002_semantic_search_leak.py -v
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from schemabrain.core.store import SQLiteStore

# Hard-coded URL keeps tests self-describing — user/password/port are
# visible in the file rather than buried behind a `.env`. Local-only
# credentials, not a leak.
PG_URL = "postgresql+psycopg://postgres:local@127.0.0.1:5433/postgres"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Auto-tag every test in this directory with ``firewall_bypass``.

    The marker is excluded from the project's default pytest selection
    (see ``pyproject.toml`` ``addopts``), so the whole corpus is opt-in
    via ``pytest -m firewall_bypass``. Without this hook, perf tests
    (which only carry ``integration``) and unit-only bypass repros
    (which carry no DB marker at all) would land in the default suite.
    """
    bypass_mark = pytest.mark.firewall_bypass
    for item in items:
        if "tests/firewall/" in str(item.fspath):
            item.add_marker(bypass_mark)


# Source connection id used across all firewall tests. Anything stable
# works; we pick a clearly attack-flavoured string so audit rows are
# searchable later.
SOURCE_ID = "firewall_bypass"


@pytest.fixture(scope="session", autouse=True)
def _setup_firewall_perf_fixtures() -> Iterator[None]:
    """Ensure the ``firewall_perf`` schema + perf-test objects exist.

    Required by ``test_perf_max_rows_truncation.py`` and
    ``test_perf_statement_timeout_enforcement.py``. Created once per
    session; objects persist across runs. Silently no-ops if Postgres
    isn't reachable — perf tests carry ``@pytest.mark.integration`` and
    will skip individually in that environment.
    """
    try:
        engine = create_engine(PG_URL)
        with engine.begin() as conn:
            conn.execute(text("CREATE SCHEMA IF NOT EXISTS firewall_perf"))
            conn.execute(
                text(
                    "CREATE OR REPLACE VIEW firewall_perf.slow_view AS "
                    "SELECT pg_sleep(5)::text AS s, 1 AS id"
                )
            )
            exists = conn.execute(
                text("SELECT to_regclass('firewall_perf.bigtab') IS NOT NULL")
            ).scalar()
            if not exists:
                conn.execute(text("CREATE TABLE firewall_perf.bigtab (id INT, label TEXT)"))
                conn.execute(
                    text(
                        "INSERT INTO firewall_perf.bigtab "
                        "SELECT i, 'label-' || i::text FROM generate_series(1, 5000) AS i"
                    )
                )
        engine.dispose()
    except Exception:
        # Postgres unreachable — perf tests will skip via their own
        # connectivity checks; bypass repros that don't need PG still
        # run normally.
        pass
    yield


@pytest.fixture(scope="session")
def pg_url() -> str:
    """Live Postgres connection URL for the docker `sb-demo-pg` container."""
    # Sanity probe — if Postgres isn't reachable, skip the whole module
    # rather than letting each test individually time out.
    engine = create_engine(PG_URL)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover — env-specific
        pytest.skip(f"Postgres not reachable at {PG_URL}: {exc}")
    finally:
        engine.dispose()
    return PG_URL


@pytest.fixture
def pg_engine(pg_url: str):
    """SQLAlchemy engine bound to the live Postgres."""
    engine = create_engine(pg_url)
    yield engine
    engine.dispose()


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SQLiteStore]:
    """Fresh SQLite store per test — torn down with the tmp dir.

    Each test gets a clean store. The tests do NOT need to share
    state with each other — they're stand-alone bypass repros.
    """
    s = SQLiteStore(tmp_path / "firewall_store.db")
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def make_schema(pg_engine):
    """Create and drop a Postgres schema per test.

    Returns a callable ``make_schema(name)`` that creates the schema
    and registers it for cleanup. Schemas are dropped CASCADE so any
    test tables go with them — no need for per-table teardown.
    """
    created: list[str] = []

    def _make(name: str) -> str:
        with pg_engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))
            conn.execute(text(f'CREATE SCHEMA "{name}"'))
        created.append(name)
        return name

    yield _make

    for name in created:
        with pg_engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))
