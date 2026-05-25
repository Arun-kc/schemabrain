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

What stage 0 does (post-PR-#79 D2):

* Auto-runs ``docker run`` for the demo Postgres container when
  Docker is detected. Reuses an existing ``sb-demo-pg`` container
  (start if stopped; pass through if running). On any failure
  (daemon not reachable, image pull failure, port conflict, perm
  denied), falls through to the pre-D2 copy-paste recipe — the
  manual path is never replaced, only augmented.
* Polls Postgres readiness via ``_wait_for_postgres_ready`` (30s
  timeout, 1s interval).
* Auto-loads the ecommerce fixture via ``docker run --rm psql``
  (matches the PR-1 recipe shape). PR-3 will replace the second
  ``docker run`` with an in-process SQLAlchemy load to eliminate
  the Docker dependency for fixture-loading.

What stage 0 does NOT do (deliberate scope discipline):

* It does NOT load the ecommerce fixture in-process via SQLAlchemy
  (PR-3 — pairs with making fixture-loading runnable on hosts
  without Docker).
* It does NOT mutate ``WizardConfig``. The caller (``_cmd_init``)
  uses the returned URL as ``WizardConfig.source_url`` — stage 0 is
  a pure resolver, not a configurator.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from typing import TYPE_CHECKING

from schemabrain._ui import (
    GLYPH_ACTIVE,
    GLYPH_BRAND,
    GLYPH_ERR,
    GLYPH_OK,
    GLYPH_PENDING,
    GLYPH_WARN,
    pause_active_spinner,
    prompt_for_url,
)
from schemabrain.eval.bundled import resolve_bundled_path

if TYPE_CHECKING:
    from rich.console import Console

__all__ = [
    "DEMO_CONTAINER_NAME",
    "DEMO_DATABASE_URL",
    "DEMO_DOCKER_RUN_COMMAND",
    "DEMO_FIXTURE_BUNDLED_NAME",
    "DEMO_FIXTURE_LOAD_COMMAND",
    "detect_docker",
    "prompt_for_init_setup",
]

# D2: container name lifted to a module constant. The auto-docker
# helpers reuse it across `docker run` / `docker inspect` /
# `docker start`, and tests can assert the exact name without
# duplicating the literal.
DEMO_CONTAINER_NAME = "sb-demo-pg"

# Bundled basename for the ecommerce fixture. Resolved to an absolute
# path at call time via `schemabrain.eval.bundled.resolve_bundled_path`
# — the same helper that backs `schemabrain fixture-path`. A repo-
# relative path joined against `Path.cwd()` breaks for every
# `pip install schemabrain` user because the fixture lives inside
# the wheel, not at `$(pwd)/schemabrain/...`.
DEMO_FIXTURE_BUNDLED_NAME = "ecommerce.sql"


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
# `psql` installed locally. The fixture path is resolved at run time
# via the `schemabrain fixture-path` CLI — `$(pwd)/schemabrain/...`
# only worked when the user was inside a cloned repo, but the
# documented install path is `pip install schemabrain` where the
# fixture lives inside the wheel. Constructed with an f-string so the
# bundled basename has a single source of truth.
DEMO_FIXTURE_LOAD_COMMAND = (
    "docker run --rm --network host "
    f"-v $(schemabrain fixture-path {DEMO_FIXTURE_BUNDLED_NAME}):/f.sql:ro "
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
        console.print(f"  [bold]{GLYPH_BRAND} SchemaBrain — wire Claude to a Postgres database[/]")
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


def prompt_for_pii_block(*, console: Console) -> tuple[str, ...]:
    """Show the PII-block-category choice prompt and return the selected
    categories.

    Runs from ``_cmd_init`` when stderr is a TTY AND ``--yes`` was NOT
    passed. The caller is responsible for gating on both conditions.
    Returns the categories that will be written as ``--pii-block`` into
    the host snippet, or ``()`` for "no PII enforcement".

    Default is ``contact`` because it covers the most universally
    sensitive PII (email, phone, address) without surprising the
    operator with refusals on benign business-domain columns. The
    "all" option enumerates the v1 closed set so the operator sees
    exactly what's in scope; "none" is the development-database
    escape hatch.

    ``KeyboardInterrupt`` propagates verbatim so Ctrl-C aborts the
    setup; non-interactive callers (or ``--yes`` paths) should not
    reach this helper and instead apply the ``("contact",)`` default
    directly.
    """
    from rich.prompt import Prompt

    with pause_active_spinner():
        console.print()
        console.print("  [bold]Block PII categories from agent queries?[/]")
        console.print(
            "  [bright_black]Refuses get_metric calls that touch the selected categories — "
            "agents can still describe the schema but cannot aggregate PII columns.[/]"
        )
        console.print()
        console.print(
            f"    [bright_black]{GLYPH_ACTIVE} 1. Recommended: contact (email, phone, address)[/]"
        )
        console.print(
            f"    [bright_black]{GLYPH_ACTIVE} 2. All categories "
            "(contact, financial, payment_card, health, government_id, ...)[/]"
        )
        console.print(
            f"    [bright_black]{GLYPH_ACTIVE} 3. None "
            "(dev/synthetic data only — agent sees all PII columns)[/]"
        )
        console.print()
        choice = Prompt.ask(
            "  [cyan]Choice[/]",
            console=console,
            choices=["1", "2", "3"],
            default="1",
            show_default=True,
            show_choices=False,
        )
    if choice == "1":
        return ("contact",)
    if choice == "2":
        # The closed v1 set imported locally — keeps the prompt
        # default-driven and avoids forcing the operator to type the
        # full enum. Sorted for stable host-snippet output.
        from schemabrain.pii.categories import PII_CATEGORIES

        return tuple(sorted(PII_CATEGORIES))
    return ()


# D2: subprocess timeouts. Tight enough to surface a stuck Docker
# daemon quickly; loose enough that a slow first-run image pull
# (~30s for postgres:16-alpine on a fresh machine) completes inside
# the window. Module-level so tests can monkeypatch them down to
# something tiny for fast assertion runs.
_DOCKER_INSPECT_TIMEOUT_S = 5
_DOCKER_START_TIMEOUT_S = 15
_DOCKER_RUN_TIMEOUT_S = 90  # postgres image pull can take this long on first run
_DOCKER_FIXTURE_TIMEOUT_S = 60
_POSTGRES_READY_TIMEOUT_S = 30
_POSTGRES_READY_INTERVAL_S = 1.0


def _docker_run_demo_postgres(*, console: Console) -> bool:
    """Bring up the demo Postgres container; return True on success.

    Idempotent: if ``sb-demo-pg`` already exists, start it (or pass
    through if already running) rather than failing on the name
    conflict. On any subprocess failure (daemon down, image pull,
    port conflict, perm denied), prints a one-line explanation and
    returns False so the caller can fall through to the manual
    copy-paste recipe.

    Never raises — every subprocess failure mode is translated into
    ``False`` so the caller's fallback path is predictable. The wizard
    contract is "stage 0 always returns a URL or None"; raising from
    a Docker helper would violate that.
    """
    inspect = _safe_subprocess(
        ["docker", "inspect", "-f", "{{.State.Running}}", DEMO_CONTAINER_NAME],
        timeout_s=_DOCKER_INSPECT_TIMEOUT_S,
    )
    if inspect is not None and inspect.returncode == 0:
        # Container exists — reuse rather than recreate.
        if inspect.stdout.strip() == "true":
            console.print(
                f"  [bright_black]{GLYPH_OK} Container "
                f"[cyan]{DEMO_CONTAINER_NAME}[/] already running[/]"
            )
            return True
        # Stopped — start it.
        started = _safe_subprocess(
            ["docker", "start", DEMO_CONTAINER_NAME],
            timeout_s=_DOCKER_START_TIMEOUT_S,
        )
        if started is not None and started.returncode == 0:
            console.print(
                f"  [bright_black]{GLYPH_OK} Started existing container "
                f"[cyan]{DEMO_CONTAINER_NAME}[/][/]"
            )
            return True
        _print_docker_failure(console, "couldn't start the existing container", started)
        return False

    # No existing container — fresh run.
    console.print(
        f"  [bright_black]{GLYPH_PENDING} Starting Postgres container "
        f"([cyan]{DEMO_CONTAINER_NAME}[/]) — first run pulls "
        "[cyan]postgres:16-alpine[/] (~30s)...[/]"
    )
    result = _safe_subprocess(
        [
            "docker",
            "run",
            "-d",
            "--name",
            DEMO_CONTAINER_NAME,
            "-p",
            "127.0.0.1:5433:5432",
            "-e",
            "POSTGRES_PASSWORD=local",
            "postgres:16-alpine",
        ],
        timeout_s=_DOCKER_RUN_TIMEOUT_S,
    )
    if result is not None and result.returncode == 0:
        console.print(f"  [bright_black]{GLYPH_OK} Container [cyan]{DEMO_CONTAINER_NAME}[/] up[/]")
        return True
    _print_docker_failure(console, "couldn't start Postgres container", result)
    return False


def _wait_for_postgres_ready(
    *,
    url: str,
    console: Console,
    timeout_s: float = _POSTGRES_READY_TIMEOUT_S,
    interval_s: float = _POSTGRES_READY_INTERVAL_S,
) -> bool:
    """Poll ``url`` until Postgres accepts connections; True if ready in time.

    Uses SQLAlchemy with a short per-connect timeout so a hung
    Postgres doesn't extend the wall-clock past the loop's deadline.
    Lazy import of sqlalchemy at function scope keeps the module's
    import cost low for callers that never reach the demo path.

    Never raises on `OperationalError` — that's the expected
    "Postgres still starting" signal during the loop. Other
    exceptions (unrelated config bugs) also fall through to a
    `False` return so the demo path can degrade gracefully to the
    manual recipe; the operator sees the connection failure
    surfaced by the actual wizard's stage 1 (`source_check`) if
    they continue.
    """
    from urllib.parse import urlparse

    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import ArgumentError, OperationalError

    from schemabrain.errors import silent_rewrite_to_psycopg

    # `silent_rewrite_to_psycopg` normalises bare `postgresql://` to
    # `postgresql+psycopg://` so SQLAlchemy doesn't try to import the
    # missing psycopg2 driver. SchemaBrain ships psycopg v3 only —
    # same boundary rewrite as `_resolve_url_source`. The helper
    # returns `None` for already-correctly-suffixed URLs; the
    # `or url` fallback keeps those verbatim.
    rewritten_url = silent_rewrite_to_psycopg(urlparse(url).scheme, url) or url
    console.print(
        f"  [bright_black]{GLYPH_PENDING} Waiting for Postgres to accept connections "
        f"(timeout {int(timeout_s)}s)...[/]"
    )
    started = time.monotonic()
    last_error: str | None = None
    while time.monotonic() - started < timeout_s:
        try:
            engine = create_engine(rewritten_url, connect_args={"connect_timeout": 2})
            try:
                with engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
                console.print(
                    f"  [bright_black]{GLYPH_OK} Postgres ready "
                    f"({int(time.monotonic() - started)}s)[/]"
                )
                return True
            finally:
                engine.dispose()
        except OperationalError as exc:
            # Expected during startup — keep polling.
            last_error = str(exc).splitlines()[0]
        except ArgumentError as exc:
            # A malformed URL is deterministic — polling for 30s
            # won't help and the operator deserves the real error
            # fast. Bail out immediately so the fall-through to the
            # manual recipe fires inside the user's attention span,
            # not 30s later.
            last_error = f"{type(exc).__name__}: {exc}"
            break
        except Exception as exc:  # pragma: no cover — defensive, non-OperationalError surface
            last_error = f"{type(exc).__name__}: {exc}"
            # Don't immediately bail on a non-OperationalError — could
            # be a transient DNS/network glitch. Keep polling.
        time.sleep(interval_s)

    suffix = f" Last error: {last_error}" if last_error else ""
    console.print(
        f"  [yellow]{GLYPH_WARN} Postgres didn't become ready within {int(timeout_s)}s.{suffix}[/]"
    )
    return False


def _docker_load_fixture(*, console: Console) -> bool:
    """Load the ecommerce fixture via ``docker run --rm psql``.

    Mirrors the shape of the pre-D2 manual command (uses
    ``--network host`` to reach 127.0.0.1:5433 from inside the
    container). The fixture path is resolved through
    ``schemabrain.eval.bundled.resolve_bundled_path`` — same helper
    that backs the ``schemabrain fixture-path`` CLI — so the path
    works whether the package is pip-installed or run from a cloned
    repo.

    Returns False (with a printed explanation) when:
    - ``resolve_bundled_path`` can't find the bundled fixture (means
      the wheel is broken — a loud failure beats silently falling
      back to the manual recipe)
    - the docker subprocess fails (most commonly: Postgres rejected
      the connection mid-load; less commonly: psql couldn't read the
      mounted file)
    """
    try:
        fixture_path = resolve_bundled_path(DEMO_FIXTURE_BUNDLED_NAME)
    except (OSError, ValueError) as exc:
        # OSError covers FileNotFoundError (wheel missing the asset) and
        # PermissionError (NFS, snap-confined Docker, macOS quarantine
        # flags on extracted wheel files). Both surface to the user with
        # the same recovery — the underlying message tells them which.
        console.print(
            f"  [yellow]{GLYPH_WARN} Couldn't resolve bundled ecommerce "
            f"fixture: {exc}. The installed wheel may be incomplete or "
            "unreadable — re-run "
            "[cyan]pip install --force-reinstall schemabrain[/].[/]"
        )
        return False

    console.print(
        f"  [bright_black]{GLYPH_PENDING} Loading ecommerce fixture (7 tables, ~1.2k rows)...[/]"
    )
    result = _safe_subprocess(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "host",
            "-v",
            f"{fixture_path}:/f.sql:ro",
            "-e",
            "PGPASSWORD=local",
            "postgres:16-alpine",
            "psql",
            "-h",
            "localhost",
            "-p",
            "5433",
            "-U",
            "postgres",
            "-d",
            "postgres",
            "-f",
            "/f.sql",
        ],
        timeout_s=_DOCKER_FIXTURE_TIMEOUT_S,
    )
    if result is not None and result.returncode == 0:
        console.print(f"  [bright_black]{GLYPH_OK} Fixture loaded[/]")
        return True
    _print_docker_failure(console, "couldn't load the ecommerce fixture", result)
    return False


def _safe_subprocess(
    argv: list[str], *, timeout_s: float
) -> subprocess.CompletedProcess[str] | None:
    """Run a subprocess with `capture_output=True`; return None on any failure.

    Centralises the try/except shape so each helper's failure path
    stays one line. Returns:
    - The completed process (operator inspects ``returncode`` /
      ``stderr``) on successful spawn — including non-zero exit.
    - ``None`` on any OS-level spawn failure (`FileNotFoundError`,
      `PermissionError`, broader `OSError`) or `TimeoutExpired`
      (subprocess hung past the deadline).

    Catches the broad `OSError` ancestor (not just
    `FileNotFoundError`) so a docker binary present on PATH but
    not executable (mode 0555 stripped, snap confinement, macOS
    quarantine, non-docker-group user on Linux) — which raises
    `PermissionError` — also falls through to the manual recipe
    rather than propagating. `OSError` covers both via inheritance
    plus future OS-level errors like `ENOMEM`.
    """
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_s,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _print_docker_failure(
    console: Console,
    summary: str,
    result: subprocess.CompletedProcess[str] | None,
) -> None:
    """Render a one-line failure summary + the subprocess's stderr first line.

    Used by `_docker_run_demo_postgres` and `_docker_load_fixture`
    when the underlying subprocess exited non-zero. The stderr first
    line surfaces what Docker said (image pull failure, port
    conflict, perm denied, daemon down) so the operator has a real
    diagnostic before the fallback recipe prints.
    """
    detail: str
    if result is None:
        detail = "subprocess didn't return (binary missing or not runnable, or hung past deadline)"
    elif result.stderr:
        detail = result.stderr.strip().splitlines()[0] if result.stderr.strip() else ""
    else:
        detail = f"exited {result.returncode} with no stderr"
    console.print(f"  [yellow]{GLYPH_ERR} {summary}: {detail}[/]")


def _try_auto_docker_demo(*, console: Console) -> bool:
    """Orchestrate the 3 auto-docker steps; True iff demo is ready end-to-end.

    Each step is independent — early-returns False on first failure
    so the caller can fall through to the manual recipe. The
    inverse ("first success = use that, fail others = recover") is
    NOT supported: a partial setup (container up, fixture missing)
    is more confusing than a clean fall-through to manual.
    """
    if not _docker_run_demo_postgres(console=console):
        return False
    if not _wait_for_postgres_ready(url=DEMO_DATABASE_URL, console=console):
        return False
    return _docker_load_fixture(console=console)


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

    # D2: try the auto-docker path first. Each step prints its own
    # progress; on any failure, falls through to the pre-D2 manual
    # copy-paste recipe below. The auto path is purely additive —
    # the manual UX remains the safe path for any environment where
    # auto-docker doesn't work (daemon down, port conflict, perm
    # denied, image pull failure).
    if _try_auto_docker_demo(console=console):
        console.print()
        console.print(f"  [bright_black]{GLYPH_OK} Demo Postgres ready: {DEMO_DATABASE_URL}[/]")
        return DEMO_DATABASE_URL

    # Fallback: auto-docker hit a failure. Print the copy-paste
    # recipe and let the operator run the commands manually. The
    # pre-D2 UX shape is preserved verbatim so operators familiar
    # with PR-1's flow recognise it.
    console.print()
    console.print(
        f"  [yellow]{GLYPH_WARN} Falling back to manual setup — run these in another terminal:[/]"
    )
    console.print()
    console.print(f"  [bold]{GLYPH_PENDING} Start Postgres:[/]")
    console.print(f"    [cyan]{DEMO_DOCKER_RUN_COMMAND}[/]")
    console.print()
    console.print(
        f"  [bold]{GLYPH_PENDING} Then load the ecommerce sample (7 tables, ~1.2k rows):[/]"
    )
    console.print(f"    [cyan]{DEMO_FIXTURE_LOAD_COMMAND}[/]")
    console.print()
    console.print(
        "  [bright_black]Both commands take ~30s on first run "
        "(image pull). If [cyan]schemabrain[/] is not on PATH in the "
        "shell you paste this into (e.g. virtual environment not "
        "activated), the fixture path expansion will be empty and "
        "psql will load an empty file — activate the venv first.[/]"
    )
    console.print("  [bright_black]Press Enter when both have finished.[/]")

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
