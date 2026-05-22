"""Per-host config-path resolution + snippet building + Claude Code shell-out.

Three host targets are supported:

  - **claude-desktop** (macOS / Windows). The MCP server entry is
    written to a JSON config file by `config_io.py`; this module only
    resolves the OS-standard path. Linux has no official Claude
    Desktop build, so the path resolver returns None there.
  - **claude-code**. Detected via `claude --version` on PATH.
    Registration shells out to `claude mcp add ...` because
    Anthropic's supported registration path is the CLI itself —
    editing `~/.claude.json` directly bypasses validation Claude Code
    does on registration, and is brittle when their schema evolves.
  - **manual**. Always available. Doesn't write anywhere; the init
    flow prints the snippet for the user to paste into whatever
    host config they're targeting.

Auto-detect order:

  1. Claude Desktop, if its config directory exists.
  2. Claude Code, if `claude --version` succeeds.
  3. Manual, as the always-available fallback.

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
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

HostName = Literal["claude-desktop", "claude-code", "manual"]


@dataclass(frozen=True)
class SchemabrainSnippet:
    """The MCP server entry to write to a host's mcpServers map.

    `command` is the executable; `args` is positional argv; `env` is
    the environment-variable block the host launches the server with.
    The env block is where DB credentials live (referenced by
    `--url-env`), keeping them out of argv.
    """

    command: str
    args: tuple[str, ...]
    env: dict[str, str]

    def to_mcp_entry(self) -> dict[str, object]:
        """Return a JSON-serialisable dict for the `mcpServers.schemabrain` slot.

        Returns fresh containers so callers can merge into a larger
        config and mutate without contaminating the snippet itself.
        """
        return {
            "command": self.command,
            "args": list(self.args),
            "env": dict(self.env),
        }


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
    return SchemabrainSnippet(
        command=command,
        args=args,
        env={env_var_name: db_url},
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

    Priority: Claude Desktop (most common; the README points here),
    then Claude Code, then manual. The desktop check requires the
    config directory to actually exist on disk — a stale path
    returned by `claude_desktop_config_path()` (e.g. macOS user with
    Claude Desktop never installed) falls through correctly.
    """
    desktop_path = claude_desktop_config_path()
    if desktop_path is not None and desktop_path.parent.exists():
        return "claude-desktop"
    if claude_code_available():
        return "claude-code"
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
