"""Pre-wizard setup prompt — the demo-vs-own-DB fork.

This module owns the "stage 0" UX the day-one overhaul adds in front
of the existing 7-stage wizard. It runs in ``_cmd_init`` BEFORE the
wizard is constructed, so it can resolve the connection URL
interactively (own-DB) or by walking the user through bringing up a
local Postgres + loading the bundled ecommerce fixture (demo).

Why a separate module rather than inlining in ``cli.py`` or
``wizard.py``:

* ``cli.py`` is already 6800+ LOC. Stage 0 has enough conditional
  logic (TTY gates, Docker probes, fork-by-choice, demo URL pinning)
  that putting it inline makes the calling block unreadable.
* ``wizard.py`` is the orchestrator for the 7 main stages — none of
  which prompt for the URL itself. Stage 0 is a different shape (CLI-
  level prompt that happens before wizard construction), so coupling
  it into the stage-handler contract would force a larger refactor
  than the day-one UX overhaul justifies.

What stage 0 does NOT do (deliberate scope discipline):

* It does NOT run ``docker compose up`` automatically. The PM brief
  defers Docker lifecycle ownership to PR-2 — owning ``docker run``,
  port-conflict probes, container reuse, and clean teardown is a
  significant try/except surface that needs its own PR. PR-1 detects
  Docker, prints the exact recipe, and waits for the user to run it
  in another terminal.
* It does NOT load the ecommerce fixture in-process. Fixture loading
  via SQLAlchemy is straightforward (~30 LOC) but pairs naturally
  with the auto-``docker run`` work in PR-2 — they share the same
  wait-for-Postgres + retry primitives.
* It does NOT mutate ``WizardConfig``. The caller (``_cmd_init``)
  uses the returned URL as ``WizardConfig.source_url`` — stage 0 is
  a pure resolver, not a configurator.
"""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

from schemabrain._ui import (
    GLYPH_ACTIVE,
    GLYPH_BRAND,
    GLYPH_OK,
    GLYPH_PENDING,
    GLYPH_WARN,
    pause_active_spinner,
    prompt_for_url,
)

if TYPE_CHECKING:
    from rich.console import Console

__all__ = [
    "DEMO_DATABASE_URL",
    "DEMO_DOCKER_RUN_COMMAND",
    "DEMO_FIXTURE_LOAD_COMMAND",
    "detect_docker",
    "prompt_for_init_setup",
]


# The demo URL the wizard returns when the user picks the demo path.
# Pinned to match the constants the user copies into `docker run` —
# port 5433 to avoid the developer-local Postgres on 5432, password
# `local` to match the docker-compose.yml convention (matches what
# the existing demo stack at repo root uses).
#
# If this constant changes, ``DEMO_DOCKER_RUN_COMMAND`` and
# ``DEMO_FIXTURE_LOAD_COMMAND`` MUST change in lockstep — they bake
# the same host/port/password into the commands shown to the user.
DEMO_DATABASE_URL = "postgresql://postgres:local@localhost:5433/postgres"

# The exact `docker run` invocation the wizard shows. Pinned as a
# constant so a test can fence the operator-visible recipe. Uses a
# named container (`sb-demo-pg`) so the user can grep/inspect/stop
# it predictably, and `-p 127.0.0.1:5433:5432` so the demo doesn't
# bind to all interfaces (matches the docker-compose.yml posture).
DEMO_DOCKER_RUN_COMMAND = (
    "docker run -d --name sb-demo-pg "
    "-p 127.0.0.1:5433:5432 "
    "-e POSTGRES_PASSWORD=local "
    "postgres:16-alpine"
)

# The fixture-load command the wizard shows after Postgres is up.
# Uses `psql` via a one-off docker container so the user doesn't need
# `psql` installed locally — matches the SQLAlchemy-load deferral
# (PR-2 will do the load in-process, eliminating this command).
DEMO_FIXTURE_LOAD_COMMAND = (
    "docker run --rm --network host "
    "-v $(pwd)/schemabrain/eval/fixtures/ecommerce.sql:/f.sql:ro "
    "-e PGPASSWORD=local postgres:16-alpine "
    "psql -h localhost -p 5433 -U postgres -d postgres -f /f.sql"
)


def detect_docker() -> str | None:
    """Check if Docker is usable; return an explanation string if not.

    Returns ``None`` when the ``docker`` binary is on PATH (the demo
    path is viable). Returns a one-paragraph explanation string when
    not — the caller can print it verbatim and fall back to the
    own-DB path.

    Only checks PATH presence, not daemon liveness. A user with
    Docker installed but daemon not running will see the "docker
    daemon not reachable" error from the actual ``docker run``
    command they execute. Probing daemon liveness in-process would
    cost a subprocess call and a few seconds; the trade-off isn't
    worth it for PR-1 — the user gets the error from Docker itself
    with one extra command attempt. PR-2 (auto-``docker run``) will
    do the daemon probe properly.
    """
    if shutil.which("docker") is None:
        return (
            "Docker is not on your PATH. Install Docker Desktop "
            "(https://docker.com/products/docker-desktop) and re-run, "
            "or pick option 1 to connect your own Postgres."
        )
    return None


def prompt_for_init_setup(*, console: Console) -> str | None:
    """Show the demo-vs-own-DB fork prompt and return a URL or None.

    Top-level entry point used by ``_cmd_init`` when no source URL
    was provided via env or CLI flag AND stderr is a TTY. The caller
    is responsible for gating on both conditions — this helper
    assumes interactive mode is appropriate and blocks on stdin.

    Returns:
        A connection URL string (either the pinned demo URL or one
        the user typed in the own-DB path), or ``None`` when the
        user declined to provide anything (pressed Enter at the
        own-DB URL prompt). The caller routes ``None`` to either
        the standard ``_resolve_url_source`` guided-error fallback
        or to an explicit "no URL — exiting" abort.

    The fork prompt defaults to ``[2]`` (demo) because the UX
    research synthesis showed new users without their own DB are
    the larger cohort AND the more failure-prone group. An
    experienced user with a real DB will read the option labels and
    pick ``1`` deliberately.

    ``KeyboardInterrupt`` propagates verbatim so Ctrl-C aborts the
    setup. The caller translates that into an exit-130 (the standard
    Ctrl-C convention) — this helper stays out of policy.
    """
    from rich.prompt import Prompt

    with pause_active_spinner():
        console.print()
        console.print(f"  [bold]{GLYPH_BRAND} Schema Brain — wire Claude to a Postgres database[/]")
        console.print()
        console.print("  [bright_black]How would you like to start?[/]")
        console.print()
        console.print(f"    [bright_black]{GLYPH_ACTIVE} 1. Connect my own Postgres[/]")
        console.print(f"    [bright_black]{GLYPH_ACTIVE} 2. Try with sample data (uses Docker)[/]")
        console.print()
        choice = Prompt.ask(
            "  [cyan]Choice[/]",
            console=console,
            choices=["1", "2"],
            default="2",
            show_default=True,
            show_choices=False,
        )
    if choice == "1":
        return _handle_own_db_path(console=console)
    return _handle_demo_path(console=console)


def _handle_own_db_path(*, console: Console) -> str | None:
    """Own-DB path — prompt for a URL using the standard primitive.

    Delegates to ``prompt_for_url`` so the URL prompt copy stays
    consistent with the 5 post-init commands. Returns whatever the
    user typed (or ``None`` for empty input — caller decides what to
    do).
    """
    console.print()
    return prompt_for_url(
        console,
        purpose="(we'll wire Claude to this database)",
    )


def _handle_demo_path(*, console: Console) -> str | None:
    """Demo path — Docker preflight, recipe printout, wait, return URL.

    Returns the pinned ``DEMO_DATABASE_URL`` when the user completes
    the recipe successfully. Returns ``None`` when Docker isn't
    available (so the caller can fall back to the own-DB prompt
    instead of dying).
    """
    from rich.prompt import Prompt

    console.print()
    docker_missing = detect_docker()
    if docker_missing is not None:
        # Print the explanation, then fall back to own-DB so the
        # user can still complete setup with their own Postgres.
        console.print(f"  [yellow]{GLYPH_WARN} {docker_missing}[/]")
        console.print()
        return _handle_own_db_path(console=console)

    console.print(f"  [bright_black]{GLYPH_OK} Docker detected on PATH[/]")
    console.print()
    console.print(f"  [bold]{GLYPH_PENDING} Run this in another terminal to start Postgres:[/]")
    console.print(f"    [cyan]{DEMO_DOCKER_RUN_COMMAND}[/]")
    console.print()
    console.print(
        f"  [bold]{GLYPH_PENDING} Then load the ecommerce sample (7 tables, ~1.2k rows):[/]"
    )
    console.print(f"    [cyan]{DEMO_FIXTURE_LOAD_COMMAND}[/]")
    console.print()
    console.print(
        "  [bright_black]Both commands take ~30s on first run "
        "(image pull). Press Enter when both have finished.[/]"
    )

    with pause_active_spinner():
        Prompt.ask(
            "  [cyan]Press Enter to continue[/]",
            console=console,
            default="",
            show_default=False,
        )

    console.print()
    console.print(f"  [bright_black]{GLYPH_OK} Using demo URL: {DEMO_DATABASE_URL}[/]")
    return DEMO_DATABASE_URL
