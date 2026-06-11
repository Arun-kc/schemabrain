"""`schemabrain demo` — a zero-setup, zero-prompt showcase of the firewall.

Productionises the offline SaaS demo. Setup builds the bundled
12-entity / 11-join / 5-metric store with real PII tags and a populated
knowledge graph, then seeds a tamper-evident audit chain of mixed
refused / error / success calls — all instant, with **no Docker, no API
key, no Postgres**. The caller then picks a payoff: open the dashboard,
run a terminal showcase, or wire an MCP host. Only the host-wire path
provisions the demo Postgres on demand, because a live agent needs real
rows to query.

The store is keyed by the SAME `source_connection_id` as the live demo
Postgres (`DEMO_DATABASE_URL`), so the offline-built store is reused
verbatim when wiring a host — the dictionary compiles against it and
`serve` executes against the live DB.
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rich.console import Console

from schemabrain.core.store import SQLiteStore

# Stable default location, rebuilt fresh on every run so the dashboard
# can be reopened later. Deliberately NOT `schemabrain.db` (the real
# store) and NOT the stashed demo artifacts under `~/.schemabrain-demos`.
DEMO_STORE_PATH = Path.home() / ".schemabrain" / "demo.db"
DEMO_PORT = 7878


@dataclass(frozen=True)
class _Beat:
    """One showcase/seed call: a natural-language question, the
    `get_metric` args it maps to, canned rows for success beats (the
    stub executor returns them after the plan compiles), the expected
    envelope status, and a one-line takeaway printed beneath it."""

    label: str
    args: dict[str, Any]
    expect: str  # "success" | "error" | "refused"
    takeaway: str
    rows: list[dict[str, Any]] = field(default_factory=list)


# The firewall story in five calls: it computes real metrics, AND it
# refuses to leak. The refusals are decided at the plan/firewall boundary
# BEFORE any SQL runs, so they are 100% real even with a stub executor;
# only the success beat's numbers come from canned rows.
_DEMO_BEATS: tuple[_Beat, ...] = (
    _Beat(
        label="Revenue by plan tier",
        args={"name": "total_revenue", "group_by": ["plan.title"]},
        expect="success",
        takeaway="computed from a curated metric — the agent never wrote SQL",
        rows=[
            {"title": "Enterprise", "total_revenue": 4_812_004},
            {"title": "Pro", "total_revenue": 61_902},
            {"title": "Free", "total_revenue": 0},
        ],
    ),
    _Beat(
        label="Count our users, broken down by email address",
        args={"name": "user_count", "group_by": ["user.email"]},
        expect="refused",
        takeaway="pii_blocked — grouping by a PII column is row-level disclosure, "
        "refused before any SQL runs (phrased as an aggregation so the agent "
        "computes it and the SERVER refuses — 'list the emails' makes a careful "
        "agent self-decline before the firewall can fire)",
    ),
    _Beat(
        label="Usage volume by plan tier",
        args={"name": "usage_volume", "group_by": ["plan.title"]},
        expect="error",
        takeaway="unreachable_entity — no modeled join from usage to plan; the agent "
        "gets a resolve_join recovery, not a hallucinated join",
    ),
    _Beat(
        label="Total revenue over time",
        args={"name": "total_revenue_real", "time_grain": "month"},
        expect="error",
        takeaway="ambiguous_time_dimension — multiple candidate timestamps; the agent "
        "disambiguates instead of guessing",
    ),
    _Beat(
        label="Count accounts by their password-hash column",
        args={"name": "user_count", "group_by": ["user.password_hash"]},
        expect="refused",
        takeaway="pii_blocked (credential) — the always-on catastrophic floor",
    ),
)


class _StubExecutor:
    """Canned-row executor — success beats need rows to land without a
    live Postgres. Refusals/errors never reach it."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.max_rows: int | None = None

    def execute(self, sql_text: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.calls.append((sql_text, params))
        return self.rows


class _FakeEmbedder:
    """Embedder seam stand-in — `get_metric` never calls it."""

    def embed(self, text: str) -> list[float]:
        del text
        return [0.0]


def seed_demo_store(store_path: Path) -> str:
    """Build the offline SaaS store + graph projection + a seeded audit
    chain. Instant: no Docker, no API key, no Postgres. Returns the
    store's `source_connection_id`. Rebuilds from scratch each call."""
    from schemabrain.datadict.demo_store import SOURCE_ID, build_saas_dictionary_store
    from schemabrain.semantic.graph_projection import rebuild_graph_projection

    store_path.parent.mkdir(parents=True, exist_ok=True)
    if store_path.exists():
        store_path.unlink()

    build_saas_dictionary_store(store_path)
    with SQLiteStore(store_path) as store:
        rebuild_graph_projection(store, source_connection_id=SOURCE_ID)

    _replay_beats(store_path, SOURCE_ID, write_audit=True)
    return SOURCE_ID


def _replay_beats(
    store_path: Path,
    source_id: str,
    *,
    console: Console | None = None,
    write_audit: bool = False,
) -> None:
    """Run every demo beat through the REAL MCP server. With
    `write_audit` the outcomes land in the tamper-evident chain; with a
    `console` each envelope is printed as it happens."""
    from schemabrain.audit.ddl import ensure_audit_schema
    from schemabrain.audit.writer import AuditWriter
    from schemabrain.mcp.server import build_server
    from schemabrain.pii import CATASTROPHIC_LEAK_CATEGORIES

    writer: AuditWriter | None = None
    if write_audit:
        conn = sqlite3.connect(str(store_path))
        ensure_audit_schema(conn)
        conn.commit()
        conn.close()
        writer = AuditWriter(store_path)
    try:
        for beat in _DEMO_BEATS:
            with SQLiteStore(store_path) as store:
                app = build_server(
                    store=store,
                    source_connection_id=source_id,
                    embedder=_FakeEmbedder(),  # type: ignore[arg-type]
                    metric_executor=_StubExecutor(beat.rows),  # type: ignore[arg-type]
                    pii_block=CATASTROPHIC_LEAK_CATEGORIES,
                    audit_writer=writer,
                )
                _content, structured = asyncio.run(app.call_tool("get_metric", beat.args))
            if console is not None:
                _print_beat(console, beat, structured)
    finally:
        if writer is not None:
            writer.close()


def _print_beat(console: Console, beat: _Beat, structured: dict[str, Any]) -> None:
    status = structured["status"]
    if status == "success":
        console.print(f"  [green]✓[/] {beat.label}")
        for row in structured["data"]["rows"]:
            console.print(f"      [dim]{_format_row(row)}[/]")
    else:
        kind = structured.get("error", {}).get("kind", "?")
        glyph = "[red]✕[/]" if status == "refused" else "[yellow]⚠[/]"
        console.print(f"  {glyph} {beat.label} → [bold]{status}[/] ([italic]{kind}[/])")
    console.print(f"      [dim]{beat.takeaway}[/]\n")


def _format_row(row: dict[str, Any]) -> str:
    parts: list[str] = []
    for key, value in row.items():
        if key.endswith(("_revenue", "_cents")) and isinstance(value, int):
            parts.append(f"{key}=${value / 100:,.2f}")
        else:
            parts.append(f"{key}={value}")
    return "  ".join(parts)


def _verify_chain(store_path: Path, console: Console) -> bool:
    from schemabrain.audit.verify import walk_chain

    conn = sqlite3.connect(str(store_path))
    conn.row_factory = sqlite3.Row
    try:
        mismatches = list(walk_chain(conn, full=True))
    finally:
        conn.close()
    if not mismatches:
        console.print(
            f"  [green]✓[/] audit chain intact ([bold]{len(_DEMO_BEATS)}[/] calls, tamper-evident)"
        )
        return True
    console.print(f"  [red]✕[/] audit chain has {len(mismatches)} mismatch(es)")
    return False


def run_showcase(*, store_path: Path, source_id: str, console: Console) -> int:
    """Replay the firewall beats live in the terminal, then verify the
    seeded audit chain. Self-contained — no browser, no key, no host."""
    console.print(
        "\n[bold]SchemaBrain firewall showcase[/] — the server computes, "
        "refuses, or recovers every call, and logs all of them.\n"
    )
    _replay_beats(store_path, source_id, console=console, write_audit=False)
    _verify_chain(store_path, console)
    console.print(
        f"\n  [dim]Explore these calls visually: "
        f"schemabrain dashboard --store-path {store_path}[/]\n"
    )
    return 0


def open_dashboard(
    *,
    store_path: Path,
    source_id: str,
    port: int,
    open_browser: bool,
    console: Console,
) -> int:
    """Serve the read-only dashboard against the seeded store. Blocks
    until Ctrl-C."""
    from schemabrain.dashboard.cli import run_dashboard
    from schemabrain.dashboard.sidecar import is_ui_available

    if not is_ui_available():
        console.print(
            "  [red]✕[/] the dashboard needs the [bold][ui][/] extra: "
            "[bold]pip install 'schemabrain[ui]'[/]"
        )
        console.print(f"  [dim]the store is ready at {store_path} — re-run once installed.[/]")
        return 2
    console.print(
        f"\n  [green]→[/] dashboard at [bold]http://127.0.0.1:{port}[/]  [dim](Ctrl-C to stop)[/]\n"
    )
    return run_dashboard(
        store_path=store_path,
        port=port,
        open_browser=open_browser,
        source_connection_id=source_id,
    )


def wire_demo_host(
    *,
    store_path: Path,
    source_id: str,
    host: str | None,
    console: Console,
) -> int:
    """Wire an MCP host to the demo. A live agent queries real rows, so
    this provisions the demo Postgres first (needs Docker)."""
    del source_id  # store already has the dictionary; serve re-reads it
    from urllib.parse import urlparse

    from schemabrain.errors import silent_rewrite_to_psycopg
    from schemabrain.pii.categories import CATASTROPHIC_LEAK_CATEGORIES
    from schemabrain.setup.hosts import detect_host
    from schemabrain.setup.init_flow import init
    from schemabrain.setup.setup_stage import (
        DEMO_DATABASE_URL,
        detect_docker,
        ensure_demo_postgres,
    )

    docker_problem = detect_docker()
    if docker_problem is not None:
        console.print(
            "  [red]✕[/] Wiring a host needs the demo Postgres (a live agent "
            "queries real rows), which needs Docker."
        )
        console.print(f"      [dim]{docker_problem}[/]")
        console.print(
            "      [dim]The dashboard and CLI showcase work without it — "
            "try `schemabrain demo --dashboard` or `--showcase`.[/]"
        )
        return 2

    if not ensure_demo_postgres(console=console):
        console.print("  [red]✕[/] could not start the demo Postgres.")
        return 2

    # `init` builds a SQLAlchemy engine to validate the source; a bare
    # `postgresql://` resolves to psycopg2 (not bundled) and crashes. Apply
    # the same silent `+psycopg` rewrite the CLI `init` path applies before
    # handing the URL to the wizard, so validation connects AND the
    # snippet's serve-computed source_id matches the offline store (which
    # is keyed to the post-rewrite hash — see datadict.demo_store.SOURCE_ID).
    demo_url = DEMO_DATABASE_URL
    rewritten = silent_rewrite_to_psycopg(urlparse(demo_url).scheme, demo_url)
    if rewritten is not None:
        demo_url = rewritten

    target = host or detect_host()
    result = init(
        source_url=demo_url,
        store_path=store_path.resolve(),
        host=target,  # type: ignore[arg-type]
        env_var_name="DATABASE_URL",
        skip_index=True,  # the store is already populated
        assume_yes=True,
        pii_block=tuple(sorted(CATASTROPHIC_LEAK_CATEGORIES)),
    )
    _print_wire_result(console, target, result.state)
    return 0


def _print_wire_result(console: Console, host: str, state: str) -> None:
    if state in ("written", "shell_out_succeeded"):
        console.print(f"\n  [green]✓[/] wired [bold]{host}[/] to the demo.")
        console.print("      Restart it, then ask:")
        console.print(
            '        [bold]"revenue by plan tier, top 5"[/]   [dim]→ a number, no SQL written[/]'
        )
        console.print(
            '        [bold]"revenue by customer account"[/]   [dim]→ a multi-hop join, computed[/]'
        )
        console.print(
            '        [bold]"usage volume by plan tier"[/]     '
            "[dim]→ unreachable: it won't invent a join[/]"
        )
        console.print(
            "      Then run [bold]schemabrain demo --showcase[/] to watch the server refuse "
            "PII directly — a careful agent declines PII on its own, so the firewall is "
            "clearest there."
        )
    elif state in ("printed_only", "unchanged"):
        console.print(
            f"\n  [yellow]⚠[/] [bold]{host}[/]: nothing written "
            "(manual host, or config already current) — see the snippet above."
        )
    else:
        console.print(f"\n  [red]✕[/] wiring [bold]{host}[/] did not complete ({state}).")


def print_next_steps(store_path: Path, console: Console) -> None:
    console.print("\n  Run any of these whenever you like:")
    console.print(f"    [bold]schemabrain demo --dashboard[/]   [dim]· {store_path}[/]")
    console.print("    [bold]schemabrain demo --showcase[/]")
    console.print("    [bold]schemabrain demo --wire[/]")
    console.print(f"    [bold]schemabrain audit verify --store-path {store_path}[/]\n")


def run_demo_menu(
    *,
    store_path: Path,
    source_id: str,
    console: Console,
    port: int,
    open_browser: bool,
) -> int:
    """One guided prompt → dashboard / showcase / wire host / quit."""
    from rich.prompt import Prompt

    console.print("\n[bold]Demo ready.[/] What now?\n")
    console.print("  [bold]1.[/] Open the dashboard     [dim]· 9 visual surfaces, no API key[/]")
    console.print(
        "  [bold]2.[/] Run the CLI showcase   [dim]· refusals + audit verify, in the terminal[/]"
    )
    console.print(
        "  [bold]3.[/] Wire Claude Desktop    [dim]· try it in your own agent (starts demo Postgres)[/]"
    )
    console.print(
        "  [bold]4.[/] Quit                   [dim]· print the commands to do these later[/]"
    )
    choice = Prompt.ask("\n  Choice", choices=["1", "2", "3", "4"], default="1", console=console)

    if choice == "1":
        return open_dashboard(
            store_path=store_path,
            source_id=source_id,
            port=port,
            open_browser=open_browser,
            console=console,
        )
    if choice == "2":
        return run_showcase(store_path=store_path, source_id=source_id, console=console)
    if choice == "3":
        return wire_demo_host(
            store_path=store_path, source_id=source_id, host=None, console=console
        )
    print_next_steps(store_path, console)
    return 0
