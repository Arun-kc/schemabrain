"""Per-host config-path resolution + snippet building + Claude Code shell-out.

Five host targets are supported:

  - **claude-desktop** (macOS / Windows). The MCP server entry is
    written to a JSON config file by `config_io.py`; this module only
    resolves the OS-standard path. Linux has no official Claude
    Desktop build, so the path resolver returns None there.
  - **claude-code**. Detected via `claude --version` on PATH.
    Registration shells out to `claude mcp add ...` because
    Anthropic's supported registration path is the CLI itself —
    editing `~/.claude.json` directly bypasses validation Claude Code
    does on registration, and is brittle when their schema evolves.
  - **cursor**. Global MCP config at ``~/.cursor/mcp.json`` (Cursor
    also supports project-local ``.cursor/mcp.json`` but init targets
    the global one — it's the "install once, use everywhere" path
    that matches the rest of the wizard's posture). Entries include
    a ``"type": "stdio"`` field; Cursor accepts the entry without
    it (defaulting to stdio) but explicit is safer when their
    schema evolves.
  - **windsurf**. Global MCP config at
    ``~/.codeium/windsurf/mcp_config.json``. JSON shape is identical
    to claude-desktop's ``mcpServers.{name}`` map — no extra fields.
  - **manual**. Always available. Doesn't write anywhere; the init
    flow prints the snippet for the user to paste into whatever
    host config they're targeting.

Auto-detect order:

  1. Claude Desktop, if its config directory exists.
  2. Claude Code, if `claude --version` succeeds.
  3. Cursor, if its config directory exists.
  4. Windsurf, if its config directory exists.
  5. Manual, as the always-available fallback.

Detection prioritizes Claude Desktop / Claude Code first because the
README's hero-path examples target them — auto-detect should match
what new users have already been steered toward. Cursor/Windsurf
fall through next so users with both Claude Desktop + Cursor
installed still land on Claude Desktop; they can override via
explicit `--host cursor`.

The snippet shape is locked: `--url-env VARNAME` is always used so DB
credentials live in the host's env block rather than in argv (where
they would be visible to any process listing). `uvx` is the default
runner because it isolates the server's Python env from the user's
project env; the absolute-path fallback to the installed schemabrain
entrypoint is used only when `uvx` is missing.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess  # nosec B404 — used only for fixed-argv shell-outs to `claude`
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

HostName = Literal["claude-desktop", "claude-code", "manual", "cursor", "windsurf"]


@dataclass(frozen=True)
class SchemabrainSnippet:
    """The MCP server entry to write to a host's mcpServers map.

    `command` is the executable; `args` is positional argv; `env` is
    the environment-variable block the host launches the server with.
    The env block is where DB credentials live (referenced by
    `--url-env`), keeping them out of argv.

    `extras` carries per-host extra keys merged into the MCP entry
    JSON (e.g., ``{"type": "stdio"}`` for Cursor). Defaults to empty
    so claude-desktop / claude-code / windsurf entries stay exactly
    the shape they were before — those hosts ignore unknown keys
    but adding fields they don't expect noises up diffs in
    operator-visible config files.
    """

    command: str
    args: tuple[str, ...]
    env: dict[str, str]
    extras: dict[str, str] = field(default_factory=dict)

    def to_mcp_entry(self) -> dict[str, object]:
        """Return a JSON-serialisable dict for the `mcpServers.schemabrain` slot.

        Returns fresh containers so callers can merge into a larger
        config and mutate without contaminating the snippet itself.
        Per-host `extras` are merged last so they can't accidentally
        overwrite the load-bearing ``command``/``args``/``env`` keys.
        """
        entry: dict[str, object] = {
            "command": self.command,
            "args": list(self.args),
            "env": dict(self.env),
        }
        entry.update(self.extras)
        return entry


@dataclass(frozen=True)
class ClaudeCodeInstallResult:
    """Outcome of attempting to install via `claude mcp add`.

    `command_run` is returned even on failure so the CLI can render
    a copy-paste fallback for the user when the shell-out couldn't
    complete (e.g. `claude` not on PATH, or registration rejected).
    """

    succeeded: bool
    command_run: tuple[str, ...]
    stderr: str = ""


def build_snippet(
    *,
    version_pin: str,
    env_var_name: str,
    store_path: Path,
    db_url: str,
    runner: str = "uvx",
    pii_block: tuple[str, ...] = (),
    host: HostName | None = None,
) -> SchemabrainSnippet:
    """Construct the schemabrain MCP entry.

    When `runner == "uvx"` (the default), the args use
    `uvx schemabrain==<pin> serve ...` — `uvx` isolates the server's
    Python env from the user's project env, and the explicit version
    pin makes restarts reproducible.

    When `runner` is anything else (typically the absolute path to the
    installed schemabrain entrypoint, used as fallback when `uvx`
    isn't on PATH), the args use `<runner> serve ...`; the binary
    itself is the implicit pin.

    `pii_block` is the comma-joined argument for `--pii-block`. Empty
    tuple means the flag is OMITTED from the args entirely (server
    runs with PII enforcement off). The init wizard prompts the
    operator for which categories to block and passes the chosen
    set through here — the server-side firewall is opt-in, but
    the wizard surfaces the choice rather than burying it.

    `host` lets per-host JSON quirks land in the snippet's ``extras``.
    Today only Cursor adds anything (``"type": "stdio"``); other
    hosts pass through unchanged. Defaults to None so call sites
    that don't care about the host quirk (tests, the manual-mode
    print path) keep working unchanged.

    Invariants enforced:

      - `store_path` must be absolute. Claude Desktop launches the
        MCP server from its own app-bundle cwd, not the user's
        terminal — a relative `.sb.db` would resolve to a directory
        the user can't find.
      - `version_pin`, `env_var_name`, and `db_url` must be non-empty.
    """
    if not version_pin:
        raise ValueError("version_pin must be non-empty")
    if not env_var_name:
        raise ValueError("env_var_name must be non-empty")
    if not db_url:
        raise ValueError("db_url must be non-empty")
    if not store_path.is_absolute():
        raise ValueError(f"store_path must be absolute; got {store_path!r}")

    serve_args: tuple[str, ...] = (
        "serve",
        "--url-env",
        env_var_name,
        "--store-path",
        str(store_path),
    )
    if pii_block:
        serve_args = (*serve_args, "--pii-block", ",".join(pii_block))
    if runner == "uvx":
        command = "uvx"
        args: tuple[str, ...] = (f"schemabrain=={version_pin}", *serve_args)
    else:
        command = runner
        args = serve_args
    extras: dict[str, str] = {}
    if host == "cursor":
        # Cursor reads MCP entries with implicit stdio default but
        # the explicit field is documented at
        # https://docs.cursor.com/context/model-context-protocol —
        # safer to write what Cursor's docs show than to rely on
        # an implicit default that could change.
        extras["type"] = "stdio"
    return SchemabrainSnippet(
        command=command,
        args=args,
        env={env_var_name: db_url},
        extras=extras,
    )


def is_postgres_url(url: str) -> bool:
    """Whether the URL is one of the Postgres scheme variants we recognise.

    Used by both `doctor` and `init` to gate the Postgres-only
    read-only session check. SQLite URLs have no equivalent.
    """
    return url.startswith(("postgresql:", "postgresql+", "postgres:"))


def claude_desktop_config_path() -> Path | None:
    """Return the OS-standard Claude Desktop config path, or None.

    Linux returns None — no official Claude Desktop build exists for
    Linux at this time, so init refuses with `--host claude-desktop`
    on Linux and suggests `--host manual` or `--host claude-code`.
    Windows without `APPDATA` set (rare; some unusual SYSTEM contexts)
    also returns None.
    """
    system = platform.system()
    if system == "Darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "Claude"
            / "claude_desktop_config.json"
        )
    if system == "Windows":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return None
        return Path(appdata) / "Claude" / "claude_desktop_config.json"
    return None


def cursor_config_path() -> Path:
    """Return Cursor's global MCP config path.

    Cursor reads ``~/.cursor/mcp.json`` (global) and project-local
    ``.cursor/mcp.json`` (per-repo). Init targets the global path
    because that matches the "install once, use everywhere" posture
    of the rest of the wizard; project-local entries are an
    operator-driven hand-edit.

    Returns the path unconditionally — Cursor runs on every OS the
    wizard cares about, so there's no per-platform None branch the
    way claude-desktop has on Linux.
    """
    return Path.home() / ".cursor" / "mcp.json"


def windsurf_config_path() -> Path:
    """Return Windsurf's MCP config path.

    Windsurf reads ``~/.codeium/windsurf/mcp_config.json`` — the
    Codeium namespace persists from before Windsurf split out as
    a standalone IDE. JSON shape matches claude-desktop's
    ``mcpServers.{name}`` map exactly.
    """
    return Path.home() / ".codeium" / "windsurf" / "mcp_config.json"


def claude_code_available() -> bool:
    """Check whether the `claude` CLI is on PATH and responsive.

    Returns False on any failure mode: not on PATH, non-zero exit,
    timeout, OS error invoking the subprocess. The caller treats any
    False as "fall through to the next host target".
    """
    if shutil.which("claude") is None:
        return False
    try:
        result = subprocess.run(  # nosec B603 B607 — constant argv, no shell, narrow timeout
            ["claude", "--version"],
            capture_output=True,
            check=False,
            timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return result.returncode == 0


def detect_host() -> HostName:
    """Auto-detect the most appropriate host target.

    Priority: Claude Desktop → Claude Code → Cursor → Windsurf →
    manual. The first two stay highest because the README's
    hero-path examples target them — auto-detect should match the
    host the user has already been steered toward. Cursor/Windsurf
    fall through next so operators with multiple IDEs installed
    land on the Anthropic-first option; explicit `--host cursor`
    or `--host windsurf` overrides the auto-pick.

    Each path check requires the config DIRECTORY to exist
    on disk — a stale path returned by the resolver (e.g. macOS
    user with Cursor never installed) falls through correctly.
    """
    desktop_path = claude_desktop_config_path()
    if desktop_path is not None and desktop_path.parent.exists():
        return "claude-desktop"
    if claude_code_available():
        return "claude-code"
    if cursor_config_path().parent.exists():
        return "cursor"
    if windsurf_config_path().parent.exists():
        return "windsurf"
    return "manual"


def _claude_mcp_add_command(snippet: SchemabrainSnippet) -> tuple[str, ...]:
    """Build the `claude mcp add` argv for the snippet.

    Claude Code's `mcp add` syntax is:

        claude mcp add [options] <name> <command> [args...]

    Critically, the schemabrain serve args themselves start with `--`
    (`--url-env`, `--store-path`). Without Claude Code's `--`
    separator before the subprocess command, Claude Code's CLI parser
    would try to interpret those as its own flags. The `--` tells it
    "stop parsing flags; the rest is the subprocess argv."

    Env vars are passed via `-e KEY=VALUE` before the separator (they
    are Claude Code's own flags). Uses the default `local` scope —
    users wanting user-wide registration can re-run with `--scope user`
    by hand.
    """
    env_args: tuple[str, ...] = tuple(
        item for k, v in snippet.env.items() for item in ("-e", f"{k}={v}")
    )
    return (
        "claude",
        "mcp",
        "add",
        *env_args,
        "schemabrain",
        "--",
        snippet.command,
        *snippet.args,
    )


def install_to_claude_code(snippet: SchemabrainSnippet) -> ClaudeCodeInstallResult:
    """Install the snippet by shelling out to `claude mcp add`.

    Never raises — failure is observable via `succeeded`. The command
    that was attempted is always returned so the CLI can print it as
    a copy-paste fallback if the shell-out couldn't complete.
    """
    command = _claude_mcp_add_command(snippet)
    if shutil.which("claude") is None:
        return ClaudeCodeInstallResult(
            succeeded=False,
            command_run=command,
            stderr="claude CLI not on PATH",
        )
    try:
        result = subprocess.run(  # nosec B603 — constant argv, no shell, narrow timeout
            list(command),
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return ClaudeCodeInstallResult(
            succeeded=False,
            command_run=command,
            stderr=f"{type(exc).__name__}: {exc}",
        )
    return ClaudeCodeInstallResult(
        succeeded=result.returncode == 0,
        command_run=command,
        stderr=result.stderr or "",
    )
