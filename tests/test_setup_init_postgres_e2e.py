"""Postgres 16 integration smoke for `schemabrain init`.

End-to-end exercise of the activation gate against a real Postgres
container: the source-reachable check, the Postgres-specific read-only
session check, atomic config write, namespace isolation, idempotency,
and the round-trip JSON the host will actually read.

Requires Docker (testcontainers). Skip with `-m "not integration"`.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from schemabrain.cli import main
from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore


@pytest.fixture
def seeded_init_store(tmp_path: Path) -> Iterator[Path]:
    """A store with one indexed table + one entity so init's
    entity-count gate passes without --skip-index."""
    path = tmp_path / "store.db"
    store = SQLiteStore(path=path)
    try:
        store.write_table(
            Table(
                name="orgs",
                schema_name="public",
                columns=(
                    Column(
                        name="id",
                        table_name="orgs",
                        schema_name="public",
                        data_type="bigint",
                        nullable=False,
                        ordinal_position=1,
                        is_primary_key=True,
                    ),
                ),
            ),
            source_connection_id="init_smoke",
        )
        store.write_entity(
            Entity(
                name="org",
                description="",
                binding=SingleTableBinding(qualified_table="public.orgs"),
                identity="id",
            ),
            source_connection_id="init_smoke",
        )
        yield path
    finally:
        store.close()


@pytest.fixture
def stub_uvx(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(
        "shutil.which",
        lambda n: "/usr/local/bin/uvx" if n == "uvx" else None,
    )
    yield


@pytest.mark.integration
class TestInitAgainstPostgres16:
    def test_print_only_with_real_postgres_url(
        self,
        seeded_pg_url: str,
        seeded_init_store: Path,
        stub_uvx: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Exercises: source-reachable + Postgres-only read-only session
        # check against a real Postgres 16 instance. --print-only means
        # init never writes; the snippet lands on stdout.
        exit_code = main(
            [
                "init",
                "--source",
                seeded_pg_url,
                "--store-path",
                str(seeded_init_store),
                "--print-only",
            ]
        )
        assert exit_code == 0
        snippet = json.loads(capsys.readouterr().out)
        entry = snippet["mcpServers"]["schemabrain"]
        # Snippet shape invariants hold against a real Postgres URL:
        assert entry["command"] == "uvx"
        assert "--url-env" in entry["args"]
        # Credentials never leak into args.
        assert seeded_pg_url not in entry["args"]
        assert entry["env"] == {"SCHEMABRAIN_DATABASE_URL": seeded_pg_url}
        # Version pin matches installed.
        from schemabrain import __version__

        assert f"schemabrain=={__version__}" in entry["args"]

    def test_writes_to_claude_desktop_config_against_postgres(
        self,
        seeded_pg_url: str,
        seeded_init_store: Path,
        stub_uvx: None,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Full claude-desktop write path: atomic JSON write, backup
        # (none on first run), round-trip parseable.
        claude_dir = tmp_path / "Claude"
        claude_dir.mkdir()
        cfg = claude_dir / "claude_desktop_config.json"
        monkeypatch.setattr(
            "schemabrain.setup.init_flow.claude_desktop_config_path",
            lambda: cfg,
        )
        exit_code = main(
            [
                "init",
                "--source",
                seeded_pg_url,
                "--store-path",
                str(seeded_init_store),
                "--host",
                "claude-desktop",
            ]
        )
        assert exit_code == 0
        # The file is JSON-parseable round-trip — the host will read
        # this on next launch.
        parsed = json.loads(cfg.read_text())
        assert "schemabrain" in parsed["mcpServers"]
        # No .bak (first write, no prior file).
        assert not (cfg.parent / (cfg.name + ".bak")).exists()

    def test_idempotent_second_run_against_postgres(
        self,
        seeded_pg_url: str,
        seeded_init_store: Path,
        stub_uvx: None,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Real Postgres on both runs; second run is a no-op.
        claude_dir = tmp_path / "Claude"
        claude_dir.mkdir()
        cfg = claude_dir / "claude_desktop_config.json"
        monkeypatch.setattr(
            "schemabrain.setup.init_flow.claude_desktop_config_path",
            lambda: cfg,
        )
        main(
            [
                "init",
                "--source",
                seeded_pg_url,
                "--store-path",
                str(seeded_init_store),
                "--host",
                "claude-desktop",
            ]
        )
        capsys.readouterr()  # discard first-run output
        first_content = cfg.read_text()
        exit_code = main(
            [
                "init",
                "--source",
                seeded_pg_url,
                "--store-path",
                str(seeded_init_store),
                "--host",
                "claude-desktop",
            ]
        )
        assert exit_code == 0
        # File untouched.
        assert cfg.read_text() == first_content
        captured = capsys.readouterr()
        assert "already configured" in captured.err

    def test_preserves_other_mcp_servers_against_real_postgres(
        self,
        seeded_pg_url: str,
        seeded_init_store: Path,
        stub_uvx: None,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The namespace-isolation contract must hold end-to-end —
        # other entries in the user's claude_desktop_config.json
        # survive byte-stable.
        claude_dir = tmp_path / "Claude"
        claude_dir.mkdir()
        cfg = claude_dir / "claude_desktop_config.json"
        cfg.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "linear": {
                            "command": "npx",
                            "args": ["@linear/mcp-server"],
                            "env": {"LINEAR_API_KEY": "lin_xxx"},
                        },
                        "stripe": {
                            "command": "stripe-mcp",
                            "args": ["serve"],
                        },
                    },
                    "globalShortcut": "Cmd+Shift+L",
                },
                indent=2,
            )
        )
        monkeypatch.setattr(
            "schemabrain.setup.init_flow.claude_desktop_config_path",
            lambda: cfg,
        )
        exit_code = main(
            [
                "init",
                "--source",
                seeded_pg_url,
                "--store-path",
                str(seeded_init_store),
                "--host",
                "claude-desktop",
            ]
        )
        assert exit_code == 0
        merged = json.loads(cfg.read_text())
        # Schemabrain was added.
        assert "schemabrain" in merged["mcpServers"]
        # Linear, Stripe, and the global shortcut are byte-stable.
        assert merged["mcpServers"]["linear"] == {
            "command": "npx",
            "args": ["@linear/mcp-server"],
            "env": {"LINEAR_API_KEY": "lin_xxx"},
        }
        assert merged["mcpServers"]["stripe"] == {
            "command": "stripe-mcp",
            "args": ["serve"],
        }
        assert merged["globalShortcut"] == "Cmd+Shift+L"
        # Backup of the original was created.
        assert (cfg.parent / (cfg.name + ".bak")).exists()


@pytest.mark.integration
class TestDoctorAgainstPostgres16:
    def test_doctor_against_real_postgres_runs_read_only_check(
        self,
        seeded_pg_url: str,
        seeded_init_store: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Doctor's --json mode is the contract surface; exercise it
        # against real Postgres so the source_session_read_only check
        # runs for real (not via mock).
        exit_code = main(
            [
                "doctor",
                "--source",
                seeded_pg_url,
                "--store-path",
                str(seeded_init_store),
                "--host",
                "manual",
                "--json",
            ]
        )
        assert exit_code == 0
        parsed = json.loads(capsys.readouterr().out)
        names = {c["name"] for c in parsed["checks"]}
        assert "source_reachable" in names
        assert "source_session_read_only" in names
        # The real session enforces RO via the connect_args options
        # init/doctor pass — so the check passes.
        ro_check = next(c for c in parsed["checks"] if c["name"] == "source_session_read_only")
        assert ro_check["outcome"] == "pass"

    def test_read_only_session_actually_blocks_writes_not_just_self_reports(
        self,
        seeded_pg_url: str,
    ) -> None:
        # The session_read_only check verifies via SHOW — but SHOW
        # returning "on" doesn't prove Postgres ACTUALLY blocks the
        # write. This test attempts a real INSERT under the same
        # connect_args init/doctor use, and asserts Postgres rejects
        # it. Catches a regression where the connect_args wiring
        # silently stops applying.
        from sqlalchemy import create_engine, text
        from sqlalchemy.exc import InternalError, OperationalError

        engine = create_engine(
            seeded_pg_url,
            connect_args={"options": "-c default_transaction_read_only=on"},
        )
        try:
            with engine.connect() as conn:
                # Confirm session reports read-only.
                assert conn.execute(text("SHOW default_transaction_read_only")).scalar() == "on"
                # Now actually try a write. Postgres should reject.
                with pytest.raises((InternalError, OperationalError)) as exc_info:
                    conn.execute(text("INSERT INTO orgs (name) VALUES ('writeattempt')"))
                assert "read-only" in str(exc_info.value).lower()
        finally:
            engine.dispose()


@pytest.mark.integration
class TestWizardAgainstPostgres16:
    """End-to-end exercise of the activation wizard against a real
    Postgres 16 instance. Verifies the full 5-stage pipeline:
    source check passes, the index stage populates the local store,
    the entities stage skips cleanly without ANTHROPIC_API_KEY in
    CI, and the host stage emits the snippet.
    """

    def test_wizard_indexes_real_postgres_into_empty_store(
        self,
        seeded_pg_url: str,
        tmp_path: Path,
        stub_uvx: None,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Drop ANTHROPIC_API_KEY so stage 3 takes the soft-skip path
        # (the explicit `--no-entities` works too; the soft-skip is
        # what most CI environments hit and is the path we most want
        # to exercise against a real database).
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        store_path = tmp_path / "wizard.db"

        exit_code = main(
            [
                "init",
                "--source",
                seeded_pg_url,
                "--store-path",
                str(store_path),
                "--print-only",
            ]
        )
        assert exit_code == 0
        captured = capsys.readouterr()
        # All seven stages render. PR #3 swapped the per-stage Panel
        # title `[N/7]` for the design's compact ordinal column —
        # each stage row leads with a zero-padded `01`..`07` cell.
        for ordinal in ("01", "02", "03", "04", "05", "06", "07"):
            assert ordinal in captured.err
        # And the progress rule above the stage list carries the
        # total stage count for the pipeline.
        assert "7 stages" in captured.err
        # Stage 2 indexed real tables.
        assert "tables" in captured.err.lower()
        # Stage 3 soft-skipped on missing API key.
        assert "ANTHROPIC_API_KEY" in captured.err
        # Stage 6 (wire_host) emitted the manual-mode snippet to stdout.
        snippet = json.loads(captured.out)
        assert "schemabrain" in snippet["mcpServers"]
        # Confirm the indexer actually wrote tables to the store —
        # this is what closes the UX gap the wizard was built for.
        with SQLiteStore(path=store_path) as store:
            tables = store.list_tables(
                source_connection_id="anything"
            )  # any source_id is fine for the assertion
            # We don't know the exact source_id without re-deriving;
            # use the cli helper to get the real one.
        from schemabrain.cli import _make_source_id

        real_source_id = _make_source_id(seeded_pg_url)
        with SQLiteStore(path=store_path) as store:
            tables = store.list_tables(source_connection_id=real_source_id)
            assert len(tables) > 0  # the seeded schema was indexed

    def test_wizard_idempotent_against_pre_indexed_store(
        self,
        seeded_pg_url: str,
        tmp_path: Path,
        stub_uvx: None,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Run the wizard twice. Second run must auto-skip stage 2 +
        # stage 3 (idempotent) and still emit the snippet.
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        store_path = tmp_path / "wizard.db"

        # First run: indexes.
        first_exit = main(
            [
                "init",
                "--source",
                seeded_pg_url,
                "--store-path",
                str(store_path),
                "--print-only",
            ]
        )
        assert first_exit == 0
        capsys.readouterr()  # discard

        # Second run: stages 2 + 3 auto-skip via the
        # "reusing N table(s) from a prior indexing run" /
        # "ANTHROPIC_API_KEY missing" paths. The F4 framing fix
        # replaced the ambiguous "already indexed" with explicit
        # prior-run language so the operator can temporally locate
        # the stage's skip.
        second_exit = main(
            [
                "init",
                "--source",
                seeded_pg_url,
                "--store-path",
                str(store_path),
                "--print-only",
            ]
        )
        assert second_exit == 0
        captured = capsys.readouterr()
        # Stage 2 reports prior-run reuse skip.
        assert "from a prior indexing run" in captured.err

    def test_wizard_skip_index_no_entities_against_postgres(
        self,
        seeded_pg_url: str,
        tmp_path: Path,
        stub_uvx: None,
    ) -> None:
        # Explicit power-user flags: --skip-index --no-entities both
        # surface as `skipped` outcomes and the wizard still wires
        # the host. Mirrors the prior PR-33 init contract.
        store_path = tmp_path / "wizard.db"
        # Pre-create the store so wire_host's existing-store check
        # doesn't refuse.
        SQLiteStore(path=store_path).close()

        exit_code = main(
            [
                "init",
                "--source",
                seeded_pg_url,
                "--store-path",
                str(store_path),
                "--print-only",
                "--skip-index",
                "--no-entities",
            ]
        )
        assert exit_code == 0
