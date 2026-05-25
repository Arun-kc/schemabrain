"""Tests for ``scripts/seed_refused_audit_row.py``.

The script appends one chain-valid refused row to ``mcp_audit`` so the
Pagila demo tape (``docs/assets/demo-pagila.tape``) Act 7 has a row
to display under ``schemabrain audit tail``. Tests pin:

* The seeded row carries the exact shape Act 7 narrates: ``status=refused``,
  ``refusal_reason=pii_blocked``, ``pii_categories=credential``,
  ``cost_class=refused``, ``tool_name=get_metric``.
* The row is chain-valid: ``walk_chain`` returns an empty iterator
  after seeding against both an empty audit table (genesis) and a
  table that already has prior rows.
* Missing store and other operator mistakes exit with rc=2 and a
  stderr line, not a stack trace.
* The CLI ``main`` entry point round-trips argv → rc cleanly.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path
from types import ModuleType

import pytest

from schemabrain.audit.ddl import ensure_audit_schema
from schemabrain.audit.verify import walk_chain
from schemabrain.audit.writer import AuditWriter, build_audit_row

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "seed_refused_audit_row.py"


@pytest.fixture()
def seed_module() -> ModuleType:
    """Load ``scripts/seed_refused_audit_row.py`` as an importable module.

    Mirrors the pattern used by ``tests/test_charter_lint.py``:
    ``scripts/`` is not a package, so test imports go through
    ``importlib.util.spec_from_file_location``. Cached on the module
    name so multiple fixture uses in one test session share the
    module object.
    """
    cached = sys.modules.get("seed_refused_audit_row")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location("seed_refused_audit_row", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["seed_refused_audit_row"] = module
    spec.loader.exec_module(module)
    return module


def _empty_store_with_audit_schema(path: Path) -> None:
    """Create a SQLite file that has the audit DDL applied but no rows.

    Mirrors the post-``schemabrain init`` state for the seed helper's
    file-exists guard. ``AuditWriter.__init__`` would also apply the
    DDL, but the guard fires before construction so we need the file
    to exist independently.
    """
    conn = sqlite3.connect(path)
    try:
        with conn:
            ensure_audit_schema(conn)
    finally:
        conn.close()


class _FakeResponse:
    def __init__(self, status: str = "success") -> None:
        self.status = status


def _write_prior_rows(path: Path, count: int) -> None:
    """Pre-populate the audit table with ``count`` success rows.

    Used by the "seed continues an existing chain" test to prove the
    seeded row's chain_hash references the prior tail rather than
    restarting from genesis.
    """
    writer = AuditWriter(path)
    try:
        for _ in range(count):
            draft = build_audit_row(
                tool_name="describe_table",
                source_connection_id="pagila",
                response=_FakeResponse(status="success"),
            )
            writer.write(draft)
    finally:
        writer.close()


class TestSeedOneRefusedRowShape:
    """The seeded row's content matches what demo-pagila.tape Act 7 narrates."""

    def test_genesis_seed_carries_expected_refusal_shape(
        self, seed_module: ModuleType, tmp_path: Path
    ) -> None:
        store_path = tmp_path / "pagila.db"
        _empty_store_with_audit_schema(store_path)

        row = seed_module.seed_one_refused_row(store_path=store_path, source_connection_id="pagila")

        assert row.id == 1
        assert row.tool_name == "get_metric"
        assert row.status == "refused"
        assert row.refusal_reason == "pii_blocked"
        assert row.cost_class == "refused"
        assert row.pii_categories == "credential"
        assert row.source_connection_id == "pagila"
        assert row.caller_id is None
        assert row.ast_shape_hash is None
        assert row.rule_id is None
        assert len(row.fingerprint) == 32
        assert len(row.chain_hash) == 32

    def test_persisted_row_matches_returned_row(
        self, seed_module: ModuleType, tmp_path: Path
    ) -> None:
        """The returned ``AuditRow`` is consistent with what SQLite stores."""
        store_path = tmp_path / "pagila.db"
        _empty_store_with_audit_schema(store_path)

        seeded = seed_module.seed_one_refused_row(
            store_path=store_path, source_connection_id="pagila"
        )

        conn = sqlite3.connect(store_path)
        conn.row_factory = sqlite3.Row
        try:
            stored = conn.execute("SELECT * FROM mcp_audit WHERE id = ?", (seeded.id,)).fetchone()
        finally:
            conn.close()

        assert stored is not None
        assert stored["tool_name"] == "get_metric"
        assert stored["status"] == "refused"
        assert stored["refusal_reason"] == "pii_blocked"
        assert stored["pii_categories"] == "credential"
        assert stored["cost_class"] == "refused"
        assert bytes(stored["chain_hash"]) == seeded.chain_hash


class TestSeedOneRefusedRowChainIntegrity:
    """``audit verify`` over the chain stays clean after seeding."""

    def test_seed_against_empty_table_produces_intact_chain(
        self, seed_module: ModuleType, tmp_path: Path
    ) -> None:
        store_path = tmp_path / "pagila.db"
        _empty_store_with_audit_schema(store_path)

        seed_module.seed_one_refused_row(store_path=store_path, source_connection_id="pagila")

        conn = sqlite3.connect(store_path)
        conn.row_factory = sqlite3.Row
        try:
            mismatches = list(walk_chain(conn, full=True))
        finally:
            conn.close()
        assert mismatches == []

    def test_seed_extends_existing_chain_not_genesis(
        self, seed_module: ModuleType, tmp_path: Path
    ) -> None:
        """When prior rows exist the seeded row references the latest
        tail, not ``GENESIS_CHAIN_HASH``. ``walk_chain`` confirms the
        whole sequence verifies."""
        store_path = tmp_path / "pagila.db"
        _empty_store_with_audit_schema(store_path)
        _write_prior_rows(store_path, count=3)

        seeded = seed_module.seed_one_refused_row(
            store_path=store_path, source_connection_id="pagila"
        )
        assert seeded.id == 4

        conn = sqlite3.connect(store_path)
        conn.row_factory = sqlite3.Row
        try:
            mismatches = list(walk_chain(conn, full=True))
            tail_count = conn.execute("SELECT count(*) AS n FROM mcp_audit").fetchone()["n"]
        finally:
            conn.close()
        assert mismatches == []
        assert tail_count == 4


class TestSeedOneRefusedRowOperatorGuards:
    """Operator-fixable mistakes surface as ``FileNotFoundError`` /
    rc=2, not as stack traces."""

    def test_missing_store_raises_file_not_found(
        self, seed_module: ModuleType, tmp_path: Path
    ) -> None:
        store_path = tmp_path / "does-not-exist.db"
        with pytest.raises(FileNotFoundError) as excinfo:
            seed_module.seed_one_refused_row(store_path=store_path, source_connection_id="pagila")
        # Operator-facing copy points at `schemabrain init` so the
        # next step is obvious.
        assert "schemabrain init" in str(excinfo.value)


class TestSeedRefusedRowsBatch:
    """``seed_refused_rows`` bulk-seeds N chained refused rows in one
    writer-open. Used by the Pagila demo tape to populate the audit
    table after Act 2's init clears it."""

    def test_count_four_produces_four_chained_rows(
        self, seed_module: ModuleType, tmp_path: Path
    ) -> None:
        store_path = tmp_path / "pagila.db"
        _empty_store_with_audit_schema(store_path)

        rows = seed_module.seed_refused_rows(
            store_path=store_path, source_connection_id="pagila", count=4
        )

        assert [r.id for r in rows] == [1, 2, 3, 4]
        assert all(r.status == "refused" for r in rows)
        # Each row's chain_hash is unique (the chain advances).
        assert len({r.chain_hash for r in rows}) == 4

        conn = sqlite3.connect(store_path)
        conn.row_factory = sqlite3.Row
        try:
            mismatches = list(walk_chain(conn, full=True))
        finally:
            conn.close()
        assert mismatches == []

    def test_count_one_matches_seed_one_refused_row(
        self, seed_module: ModuleType, tmp_path: Path
    ) -> None:
        """The original ``seed_one_refused_row`` API is preserved as a
        thin wrapper around ``seed_refused_rows(count=1)``."""
        store_path = tmp_path / "pagila.db"
        _empty_store_with_audit_schema(store_path)

        row = seed_module.seed_one_refused_row(store_path=store_path, source_connection_id="pagila")
        assert row.id == 1
        assert row.status == "refused"

    def test_count_zero_rejects(self, seed_module: ModuleType, tmp_path: Path) -> None:
        store_path = tmp_path / "pagila.db"
        _empty_store_with_audit_schema(store_path)
        with pytest.raises(ValueError, match="count must be >= 1"):
            seed_module.seed_refused_rows(
                store_path=store_path, source_connection_id="pagila", count=0
            )

    def test_cli_count_flag_renders_id_range(
        self,
        seed_module: ModuleType,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        store_path = tmp_path / "pagila.db"
        _empty_store_with_audit_schema(store_path)

        rc = seed_module.main(
            [
                "--store-path",
                str(store_path),
                "--source-id",
                "pagila",
                "--count",
                "3",
            ]
        )
        captured = capsys.readouterr()
        assert rc == 0
        assert "seeded 3 row(s)" in captured.out
        assert "ids=1..3" in captured.out


class TestCliEntryPoint:
    def test_cli_happy_path_prints_summary_and_exits_zero(
        self,
        seed_module: ModuleType,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        store_path = tmp_path / "pagila.db"
        _empty_store_with_audit_schema(store_path)

        rc = seed_module.main(["--store-path", str(store_path), "--source-id", "pagila"])
        captured = capsys.readouterr()
        assert rc == 0
        assert "seeded 1 row(s)" in captured.out
        assert "ids=1..1" in captured.out
        assert "status=refused" in captured.out
        assert "refusal_reason=pii_blocked" in captured.out
        assert "pii_categories=credential" in captured.out
        assert "chain_hash=" in captured.out

    def test_cli_missing_store_exits_two_with_stderr(
        self,
        seed_module: ModuleType,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        store_path = tmp_path / "does-not-exist.db"
        rc = seed_module.main(["--store-path", str(store_path), "--source-id", "pagila"])
        captured = capsys.readouterr()
        assert rc == 2
        assert captured.out == ""
        assert "seed_refused_audit_row:" in captured.err
        assert "schemabrain init" in captured.err

    def test_cli_respects_explicit_tool_name_and_pii_category(
        self,
        seed_module: ModuleType,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """``--tool-name`` and ``--pii-category`` override the defaults.

        The audit schema does not gate ``pii_categories`` values at
        the SQL layer (the column is ``TEXT NOT NULL DEFAULT ''``)
        and Python's ``Literal`` is not runtime-checked. The seed
        helper trusts the operator on these knobs since it is a
        single-purpose demo fixture, not an end-user surface.
        """
        store_path = tmp_path / "pagila.db"
        _empty_store_with_audit_schema(store_path)

        rc = seed_module.main(
            [
                "--store-path",
                str(store_path),
                "--source-id",
                "pagila",
                "--tool-name",
                "describe_entity",
                "--pii-category",
                "contact",
            ]
        )
        captured = capsys.readouterr()
        assert rc == 0
        assert "pii_categories=contact" in captured.out

        conn = sqlite3.connect(store_path)
        conn.row_factory = sqlite3.Row
        try:
            stored = conn.execute(
                "SELECT tool_name, pii_categories FROM mcp_audit WHERE id = 1"
            ).fetchone()
        finally:
            conn.close()
        assert stored["tool_name"] == "describe_entity"
        assert stored["pii_categories"] == "contact"
