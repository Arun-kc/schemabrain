"""Tests for the `schemabrain demo` command (zero-setup showcase).

The demo builds the offline SaaS store + graph projection + a seeded
audit chain, then offers a guided menu / direct actions. These tests pin
the seeding (full pack + verified chain), the firewall beat outcomes (so
a beat can't silently drift), and the CLI dispatch paths that don't need
Docker or a blocking server (showcase, non-TTY next-steps, dashboard
without the `[ui]` extra).
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from rich.console import Console

import schemabrain.cli as cli_mod
import schemabrain.setup.demo as demo_mod
from schemabrain.audit.verify import walk_chain
from schemabrain.cli import _dispatch
from schemabrain.core.store import SQLiteStore
from schemabrain.datadict.demo_store import SOURCE_ID
from schemabrain.mcp.server import build_server
from schemabrain.pii import CATASTROPHIC_LEAK_CATEGORIES
from schemabrain.setup.demo import (
    _DEMO_BEATS,
    _FakeEmbedder,
    _StubExecutor,
    seed_demo_store,
)


class TestSeedDemoStore:
    def test_builds_full_pack_graph_and_verified_chain(self, tmp_path: Path) -> None:
        store_path = tmp_path / "demo.db"
        source_id = seed_demo_store(store_path)

        # The offline store keys to the SAME source_id as the live demo
        # Postgres, so the store is reusable when wiring a host.
        assert source_id == SOURCE_ID

        conn = sqlite3.connect(str(store_path))
        conn.row_factory = sqlite3.Row
        try:
            joins = conn.execute("SELECT count(*) FROM canonical_joins").fetchone()[0]
            nodes = conn.execute("SELECT count(*) FROM graph_nodes").fetchone()[0]
            edges = conn.execute("SELECT count(*) FROM graph_edges").fetchone()[0]
            audit_rows = conn.execute("SELECT count(*) FROM mcp_audit").fetchone()[0]
            mismatches = list(walk_chain(conn, full=True))
        finally:
            conn.close()

        assert joins == 11
        assert nodes == 12
        assert edges == 11
        # One audit row per replayed beat; the chain is intact.
        assert audit_rows == len(_DEMO_BEATS)
        assert mismatches == []

    def test_rebuilds_from_scratch_each_call(self, tmp_path: Path) -> None:
        store_path = tmp_path / "demo.db"
        seed_demo_store(store_path)
        # A second call must not double the audit chain — it rebuilds fresh.
        seed_demo_store(store_path)
        conn = sqlite3.connect(str(store_path))
        try:
            audit_rows = conn.execute("SELECT count(*) FROM mcp_audit").fetchone()[0]
        finally:
            conn.close()
        assert audit_rows == len(_DEMO_BEATS)


class TestDemoBeats:
    def test_every_beat_produces_its_declared_status(self, tmp_path: Path) -> None:
        # The headline guarantee: each beat's `expect` matches what the
        # real firewall returns. If a metric/join/PII rule changes such
        # that a beat no longer refuses (or no longer succeeds), this
        # fails loudly instead of the demo quietly misrepresenting itself.
        store_path = tmp_path / "demo.db"
        source_id = seed_demo_store(store_path)
        for beat in _DEMO_BEATS:
            with SQLiteStore(store_path) as store:
                app = build_server(
                    store=store,
                    source_connection_id=source_id,
                    embedder=_FakeEmbedder(),  # type: ignore[arg-type]
                    metric_executor=_StubExecutor(beat.rows),  # type: ignore[arg-type]
                    pii_block=CATASTROPHIC_LEAK_CATEGORIES,
                )
                _content, structured = asyncio.run(app.call_tool("get_metric", beat.args))
            assert structured["status"] == beat.expect, (
                f"{beat.label!r}: expected {beat.expect}, got {structured['status']} "
                f"({structured.get('error')})"
            )

    def test_email_enumeration_is_the_pii_headline_beat(self) -> None:
        # The exact question that used to leak 72 emails must be a refused
        # beat — it is the demo's headline firewall moment.
        email_beats = [b for b in _DEMO_BEATS if b.args.get("group_by") == ["user.email"]]
        assert len(email_beats) == 1
        assert email_beats[0].expect == "refused"

    def test_beat_mix_tells_the_full_story(self) -> None:
        # At least one success, two refusals, two recoverable errors — so
        # the showcase shows compute AND protection AND recovery.
        by_status: dict[str, int] = {}
        for beat in _DEMO_BEATS:
            by_status[beat.expect] = by_status.get(beat.expect, 0) + 1
        assert by_status.get("success", 0) >= 1
        assert by_status.get("refused", 0) >= 2
        assert by_status.get("error", 0) >= 2


class TestDemoDispatch:
    def test_showcase_action_exits_zero(self, tmp_path: Path) -> None:
        store_path = tmp_path / "demo.db"
        rc = _dispatch(["demo", "--showcase", "--store-path", str(store_path)])
        assert rc == 0
        assert store_path.exists()

    def test_non_tty_no_action_prints_next_steps(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Without an action flag and not at a TTY, the command must NOT
        # block on the interactive menu — it seeds, prints next steps, and
        # exits 0 (the CI / piped-output contract).
        monkeypatch.setattr(cli_mod, "_stderr_is_interactive_tty", lambda: False)
        store_path = tmp_path / "demo.db"
        rc = _dispatch(["demo", "--store-path", str(store_path)])
        assert rc == 0
        combined = capsys.readouterr()
        assert "schemabrain demo --showcase" in (combined.out + combined.err)


class TestWireDemoHost:
    def test_returns_2_and_guides_when_docker_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Wiring a host needs the live demo Postgres (the agent queries
        # real rows). With no Docker, it must fail cleanly and point the
        # user at the Docker-free dashboard / showcase paths — never
        # half-provision.
        store_path = tmp_path / "demo.db"
        source_id = seed_demo_store(store_path)
        monkeypatch.setattr(
            "schemabrain.setup.setup_stage.detect_docker",
            lambda: "Docker is not installed.",
        )
        console = Console(record=True)
        rc = demo_mod.wire_demo_host(
            store_path=store_path, source_id=source_id, host=None, console=console
        )
        assert rc == 2
        text = console.export_text()
        assert "--dashboard" in text or "--showcase" in text

    def test_hands_init_a_psycopg_scheme_url(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # init() builds a SQLAlchemy engine to validate the source. A bare
        # `postgresql://` resolves to psycopg2 (not bundled) and crashes
        # with ModuleNotFoundError. wire must hand init the `+psycopg`
        # form — the same scheme `serve` resolves DEMO_DATABASE_URL to —
        # so validation connects AND the snippet's source_id matches the
        # store. Regression: wire passed the raw bare URL straight through.
        from types import SimpleNamespace

        store_path = tmp_path / "demo.db"
        source_id = seed_demo_store(store_path)
        monkeypatch.setattr("schemabrain.setup.setup_stage.detect_docker", lambda: None)
        monkeypatch.setattr(
            "schemabrain.setup.setup_stage.ensure_demo_postgres", lambda **_kw: True
        )
        captured: dict[str, object] = {}

        def _fake_init(**kwargs: object) -> SimpleNamespace:
            captured.update(kwargs)
            return SimpleNamespace(state="printed_only")

        monkeypatch.setattr("schemabrain.setup.init_flow.init", _fake_init)

        rc = demo_mod.wire_demo_host(
            store_path=store_path,
            source_id=source_id,
            host="manual",
            console=Console(record=True),
        )

        assert rc == 0
        assert isinstance(captured["source_url"], str)
        assert captured["source_url"].startswith("postgresql+psycopg://")


class TestOpenDashboard:
    def test_returns_2_without_ui_extra(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store_path = tmp_path / "demo.db"
        source_id = seed_demo_store(store_path)
        # Pretend the [ui] extra is absent so the dashboard never tries to
        # bind a port / block. open_dashboard imports is_ui_available at
        # call time from the sidecar module.
        monkeypatch.setattr("schemabrain.dashboard.sidecar.is_ui_available", lambda: False)
        rc = demo_mod.open_dashboard(
            store_path=store_path,
            source_id=source_id,
            port=7878,
            open_browser=False,
            console=Console(),
        )
        assert rc == 2
