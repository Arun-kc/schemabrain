"""Atomic read-merge-write of host MCP configs with single-shot backup.

Three contracts:

1. **Namespace isolation.** Only `mcpServers.schemabrain` is read or
   written. All other top-level keys and all other `mcpServers.*`
   entries pass through byte-stable across the round-trip — a user
   who has manually added other MCP servers (or unrelated top-level
   settings) gets them back exactly as they were.

2. **Atomic writes.** New contents land via a sibling tmp file
   that is fsynced and then renamed over the target. A crash
   mid-write leaves the previous file intact rather than producing
   a partial write that the host would fail to parse on next launch.

3. **Backup once.** On the first write to an existing config, a
   `.bak` sibling captures the pre-write contents. Subsequent writes
   do NOT overwrite that backup — re-running `init` must never
   destroy the original rollback target, even after the user has
   intentionally edited the live config in between.

`read_mcp_config` raises `MalformedConfigError` rather than blindly
overwriting a file the parser can't make sense of. The error carries
the path and the underlying JSON decode error so the CLI can surface
a precise location to the user.
"""

from __future__ import annotations

import difflib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from schemabrain.setup.hosts import SchemabrainSnippet


class MalformedConfigError(Exception):
    """Raised when an existing MCP host config file is not valid JSON.

    Carries `path` + `decode_error` so the CLI can format a
    precise-location error message instead of a generic parse failure.
    """

    def __init__(self, path: Path, decode_error: json.JSONDecodeError) -> None:
        super().__init__(
            f"existing config at {path} is not valid JSON: {decode_error.msg} "
            f"(line {decode_error.lineno}, col {decode_error.colno})"
        )
        self.path = path
        self.decode_error = decode_error


def read_mcp_config(path: Path) -> dict[str, Any] | None:
    """Read and parse a host MCP config file.

    Returns the parsed dict, or `None` if the file does not exist.
    Raises `MalformedConfigError` on JSON parse failure — we refuse
    to write over a file we can't parse.
    """
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise MalformedConfigError(path, exc) from exc


def merge_schemabrain_entry(
    existing: dict[str, Any] | None,
    snippet: SchemabrainSnippet,
) -> dict[str, Any]:
    """Return a new config with `snippet` merged into `mcpServers.schemabrain`.

    Pure function — does not mutate `existing`. When `existing` is
    None, returns a minimal config holding only the schemabrain entry.
    When `existing` already has a `schemabrain` entry, it is
    overwritten; all other entries and all other top-level keys
    pass through untouched.
    """
    if existing is None:
        return {"mcpServers": {"schemabrain": snippet.to_mcp_entry()}}
    new = dict(existing)
    servers_raw = new.get("mcpServers")
    servers: dict[str, Any] = dict(servers_raw) if isinstance(servers_raw, dict) else {}
    servers["schemabrain"] = snippet.to_mcp_entry()
    new["mcpServers"] = servers
    return new


def write_mcp_config_atomic(
    path: Path,
    config: dict[str, Any],
    *,
    create_backup: bool = True,
) -> bool:
    """Write `config` to `path` atomically. Return True if a backup was created.

    Process:

      1. If `create_backup` and `path` exists and the `.bak` sibling
         does NOT exist yet, copy `path` to `<path>.bak` (preserving
         metadata via `shutil.copy2`).
      2. Open a sibling tmp file via `tempfile.mkstemp` (same dir so
         the subsequent rename is atomic on POSIX).
      3. Write the JSON, flush, fsync, close.
      4. `os.replace` the tmp file over `path`.

    Any failure between steps 2 and 4 unlinks the tmp file before
    propagating the exception, so the user's config directory never
    accumulates `.tmp` residue.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_made = False
    backup_path = path.parent / (path.name + ".bak")
    if create_backup and path.exists():
        # TOCTOU-safe: open with O_CREAT|O_EXCL so two concurrent init
        # invocations can't both observe "no .bak" and race to copy
        # over each other. Whoever loses the race sees FileExistsError
        # and respects the existing backup.
        try:
            bak_fd = os.open(
                backup_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            bak_fd = None
        if bak_fd is not None:
            os.close(bak_fd)
            shutil.copy2(path, backup_path)
            backup_made = True
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return backup_made


def format_mcp_entry_diff(
    *,
    existing_entry: dict[str, Any] | None,
    new_entry: dict[str, Any],
    config_path: Path,
) -> str:
    """Render a unified-diff preview of one schemabrain MCP entry change.

    Pretty-prints both entries as indented JSON with sorted keys, then
    runs them through ``difflib.unified_diff`` with the host config
    path in both headers (suffixed with ``(current)`` and
    ``(after init)``) so the operator can see the exact byte-level
    delta — env-var rename, command pin, db_url shift, store-path move
    — before approving the overwrite at stage 6.

    When ``existing_entry`` is ``None`` the diff renders the new entry
    as a pure addition (``+`` lines only) so the same renderer can
    cover the "fresh-write" path without a None-check at the call site.

    Pure function: no I/O, no host clock dependence, no mutation. The
    wizard's ``_render_overwrite_diff_summary`` formats this through
    Rich; tests assert on the raw text returned here.
    """
    existing_text = (
        json.dumps(existing_entry, indent=2, sort_keys=True) + "\n"
        if existing_entry is not None
        else ""
    )
    new_text = json.dumps(new_entry, indent=2, sort_keys=True) + "\n"
    fromfile = f"{config_path} (current)"
    tofile = f"{config_path} (after init)"
    diff_iter = difflib.unified_diff(
        existing_text.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile=fromfile,
        tofile=tofile,
        n=3,
    )
    return "".join(diff_iter)


def schemabrain_entry_in(config: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the existing `mcpServers.schemabrain` entry, or None.

    Returns None for any of: `config is None`, no `mcpServers` key,
    `mcpServers` is not a dict, no `schemabrain` entry under it, or
    the `schemabrain` entry is not a dict. Used by the init flow to
    feed the present/identical/different state matrix before deciding
    whether to prompt for overwrite.
    """
    if config is None:
        return None
    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        return None
    entry = servers.get("schemabrain")
    if not isinstance(entry, dict):
        return None
    return entry
