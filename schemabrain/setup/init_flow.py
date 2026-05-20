"""`schemabrain init` orchestration — the activation gate.

`init` wires Schema Brain to an MCP host. The non-interactive flow
takes resolved arguments (already validated upstream by the CLI for
URL conflicts, env-var resolution, etc.) and:

  1. Picks a runner — `uvx` if available, the installed entrypoint as
     fallback. Refuses if neither is invocable.
  2. Validates the source URL is reachable; on Postgres also checks
     the session is read-only. (The same checks `doctor` uses, but
     init refuses on first failure rather than gathering all
     diagnostics.)
  3. Validates the store path is writable and (if it exists) carries
     the right schema version, and — unless `skip_index=True` — that
     it holds at least one entity.
  4. Resolves the host config target (Claude Desktop) or pre-flights
     the shell-out (Claude Code), or prepares a manual print-only
     output.
  5. Builds the snippet via `build_snippet` and installs it:
     - claude-desktop: `config_io.write_mcp_config_atomic` with the
       backup-once + namespace-isolation contract.
     - claude-code: shell out via `install_to_claude_code`.
     - manual: return the snippet for the CLI to print.

Refusals raise `InitRefusal` (carrying a `GuidedError`) so the CLI
can render the standard guided-error block + map to exit code 2.
Non-failure outcomes return an `InitResult` that the CLI uses to
decide what to print to the user.

This orchestrator is non-interactive; the CLI wraps it with a
TTY-gated prompt for the two recoverable refusal kinds (existing
entry + empty store).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sqlalchemy import create_engine, text

from schemabrain import __version__
from schemabrain.errors import GuidedError
from schemabrain.setup.config_io import (
    MalformedConfigError,
    format_mcp_entry_diff,
    merge_schemabrain_entry,
    read_mcp_config,
    schemabrain_entry_in,
    write_mcp_config_atomic,
)
from schemabrain.setup.hosts import (
    ClaudeCodeInstallResult,
    HostName,
    SchemabrainSnippet,
    build_snippet,
    claude_desktop_config_path,
    install_to_claude_code,
    is_postgres_url,
)

InitState = Literal[
    "written",
    "unchanged",
    "shell_out_succeeded",
    "shell_out_failed",
    "printed_only",
]


class InitRefusal(Exception):
    """Raised when init's preconditions are not met.

    Carries a `GuidedError` so the CLI can render the standard
    error/why/fix/next block instead of a bare exception message.
    The CLI maps every `InitRefusal` to exit code 2 (operational
    refusal before init could run).
    """

    def __init__(self, error: GuidedError) -> None:
        super().__init__(error.message)
        self.error = error


@dataclass(frozen=True)
class InitResult:
    """The outcome of a successful (non-refusing) init run.

    `host` is the host that was targeted. `snippet` is what was (or
    would have been) installed — included in every result so the CLI
    can fall back to printing it (claude-code shell-out failure,
    manual mode, idempotent no-op).

    `config_path` is set only for claude-desktop (the JSON file that
    was written or would have been written). `backup_made` is set
    when `write_mcp_config_atomic` reports that a `.bak` sibling was
    created on this run.

    `shell_out_command` and `shell_out_stderr` are set only when the
    host was claude-code, capturing what was attempted so the CLI
    can print the fallback copy-paste command on failure.
    """

    host: HostName
    snippet: SchemabrainSnippet
    state: InitState
    config_path: Path | None = None
    backup_made: bool = False
    shell_out_command: tuple[str, ...] | None = None
    shell_out_stderr: str | None = None
    # True when the store was empty + `--skip-index` was set or accepted
    # interactively; the CLI renderer uses this to add an "index before
    # querying" line to the next-step block.
    skip_index: bool = False


ClaudeDesktopEntryVerdict = Literal[
    "new",
    "unchanged",
    "differs_store_path_only",
    "differs",
]
"""Result of comparing the new claude-desktop snippet against the existing config.

* ``"new"`` — no existing schemabrain entry (file missing or no
  ``mcpServers.schemabrain`` key). Safe to write without prompting.
* ``"unchanged"`` — existing entry matches the new one byte-for-byte
  in command + args + env. ``init()`` no-ops (state=``"unchanged"``).
* ``"differs_store_path_only"`` — the only difference is the value
  passed to ``--store-path``. Common case (operator moved their
  store), auto-acceptable without a prompt.
* ``"differs"`` — some other field (env var name, db_url, version
  pin, runner) differs. Surface a prompt so the operator confirms
  the overwrite is intentional.
"""


@dataclass(frozen=True)
class ClaudeDesktopEntryComparison:
    """Verdict + diff summary returned by ``compare_existing_claude_desktop_entry``.

    Used by the wizard's stage 6 (``_stage_wire_host``) to decide
    whether to prompt the operator before calling ``init()`` —
    moving the prompt INTO the stage handler (F3 fix) so it doesn't
    fire as an orphaned line BETWEEN the hero panel and the 7-stage
    table.

    ``existing_store_path`` and ``new_store_path`` are populated for
    every state EXCEPT ``"new"`` so the renderer can surface a
    ``replaced /old → /new`` line when the auto-accept fires.

    ``diff_preview`` is the raw unified-diff text (from
    ``format_mcp_entry_diff``) of the two entries, populated only for
    ``"differs"`` and ``"differs_store_path_only"``. Empty string for
    ``"new"`` and ``"unchanged"`` (nothing meaningful to diff). The
    wizard renders this inside a Panel before the inline overwrite
    prompt fires (D3) so operators see exactly which JSON bytes are
    about to change before they approve.
    """

    state: ClaudeDesktopEntryVerdict
    config_path: Path
    differing_field_names: tuple[str, ...] = ()
    existing_store_path: str | None = None
    new_store_path: str | None = None
    diff_preview: str = ""


def compare_existing_claude_desktop_entry(
    *,
    snippet: SchemabrainSnippet,
    config_path: Path,
) -> ClaudeDesktopEntryComparison:
    """Compare ``snippet`` against the existing claude-desktop config.

    Pure read — does NOT mutate the config file or shell out. Used
    by the wizard's stage 6 to decide whether to prompt the operator
    before calling ``init()`` (the F3 fix that moved the overwrite
    prompt INTO stage 6, eliminating the orphaned prompt between
    the hero panel and the 7-stage table).

    Field-level diff:

    * Compares ``command`` and ``env`` strictly.
    * Strips the ``--store-path PATH`` pair from ``args`` before
      comparing the rest, so a store-path-only difference reports as
      ``"differs_store_path_only"`` (auto-acceptable) rather than
      generic ``"differs"`` (prompt-required).

    Raises ``InitRefusal`` if the config file is present but
    malformed — wraps ``read_mcp_config``'s ``MalformedConfigError``
    so the wizard's ``except InitRefusal`` guard at stage 6 catches
    it and the operator sees a guided error rather than a raw Python
    traceback. (An earlier shape raised ``MalformedConfigError``
    directly, which escaped both ``_stage_wire_host``'s ``except
    InitRefusal`` and ``_install_to_claude_desktop``'s, crashing
    init on any malformed ``claude_desktop_config.json``.)
    """
    if not config_path.exists():
        return ClaudeDesktopEntryComparison(
            state="new",
            config_path=config_path,
            new_store_path=_extract_store_path_from_args(snippet.args),
        )
    try:
        existing = read_mcp_config(config_path)
    except MalformedConfigError as exc:
        raise InitRefusal(
            GuidedError(
                kind="init_host_config_malformed",
                message=f"existing host config at {config_path} is not valid JSON",
                why=f"{exc.decode_error.msg} (line {exc.decode_error.lineno}, "
                f"col {exc.decode_error.colno}) — re-running init would "
                "destroy the unparseable contents",
                fix="inspect the file or restore from the .bak sibling, then re-run init",
                next_step=None,
            )
        ) from exc
    existing_entry = schemabrain_entry_in(existing)
    new_entry = snippet.to_mcp_entry()
    new_store_path = _extract_store_path_from_args(snippet.args)
    if existing_entry is None:
        return ClaudeDesktopEntryComparison(
            state="new",
            config_path=config_path,
            new_store_path=new_store_path,
        )
    existing_store_path = _extract_store_path_from_args(tuple(existing_entry.get("args", ())))
    if existing_entry == new_entry:
        return ClaudeDesktopEntryComparison(
            state="unchanged",
            config_path=config_path,
            existing_store_path=existing_store_path,
            new_store_path=new_store_path,
        )
    differing_fields = _diff_mcp_entry_fields(existing_entry, new_entry)
    # When the only differing field is the store-path arg, treat it
    # as auto-acceptable. Operators commonly move their store
    # between projects; forcing a prompt for that case turns into
    # noise. Any other diff (env-var name, db_url, version pin,
    # runner change) keeps the prompt.
    if differing_fields == ("args[--store-path]",):
        state: ClaudeDesktopEntryVerdict = "differs_store_path_only"
    else:
        state = "differs"
    diff_preview = format_mcp_entry_diff(
        existing_entry=existing_entry,
        new_entry=new_entry,
        config_path=config_path,
    )
    return ClaudeDesktopEntryComparison(
        state=state,
        config_path=config_path,
        differing_field_names=differing_fields,
        existing_store_path=existing_store_path,
        new_store_path=new_store_path,
        diff_preview=diff_preview,
    )


def _extract_store_path_from_args(args: tuple[str, ...]) -> str | None:
    """Return the value passed to ``--store-path`` in ``args``, or ``None``.

    Used by the entry-comparison renderer so the auto-accept message
    can show ``replaced /old → /new``. Returns ``None`` when
    ``--store-path`` is absent (defensive — every snippet we build
    today includes it via ``build_snippet``).
    """
    for idx, token in enumerate(args):
        if token == "--store-path" and idx + 1 < len(args):
            return args[idx + 1]
    return None


def _diff_mcp_entry_fields(existing: dict[str, object], new: dict[str, object]) -> tuple[str, ...]:
    """Return the canonical names of fields that differ between two MCP entries.

    Returns a stable-ordered tuple of identifier strings:

    * ``"command"`` — the executable changed
    * ``"env"`` — the env-var block changed (any key)
    * ``"args[--store-path]"`` — only the store-path value changed
    * ``"args"`` — args differ in a way beyond just the store-path

    Used to drive the verdict logic in
    ``compare_existing_claude_desktop_entry`` — the wizard's
    auto-accept fires only when the diff is exactly
    ``("args[--store-path]",)`` (one cell, store-path only).
    """
    diffs: list[str] = []
    if existing.get("command") != new.get("command"):
        diffs.append("command")
    existing_args = tuple(existing.get("args", ()))
    new_args = tuple(new.get("args", ()))
    if existing_args != new_args:
        existing_store = _extract_store_path_from_args(existing_args)
        new_store = _extract_store_path_from_args(new_args)
        existing_args_without_store = _strip_store_path_pair(existing_args)
        new_args_without_store = _strip_store_path_pair(new_args)
        if (
            existing_args_without_store == new_args_without_store
            and existing_store is not None
            and new_store is not None
            and existing_store != new_store
        ):
            diffs.append("args[--store-path]")
        else:
            diffs.append("args")
    if existing.get("env") != new.get("env"):
        diffs.append("env")
    return tuple(diffs)


def _strip_store_path_pair(args: tuple[str, ...]) -> tuple[str, ...]:
    """Return ``args`` with the ``--store-path PATH`` pair removed.

    Helper for ``_diff_mcp_entry_fields``: lets the diff detect
    store-path-only differences by comparing the residual args.
    """
    out: list[str] = []
    skip_next = False
    for token in args:
        if skip_next:
            skip_next = False
            continue
        if token == "--store-path":
            skip_next = True
            continue
        out.append(token)
    return tuple(out)


def peek_claude_desktop_overwrite(
    *,
    source_url: str,
    store_path: Path,
    env_var_name: str,
) -> ClaudeDesktopEntryComparison | None:
    """Resolve the config path, build the snippet, and compare against existing.

    Returns the comparison verdict, or ``None`` when no config path
    can be resolved (claude-desktop config dir missing on this
    platform — ``init()`` will raise ``InitRefusal`` later with a
    guided error, so we early-out here without prompting).

    Propagates ``InitRefusal`` from ``_resolve_runner`` so the
    "neither uvx nor schemabrain on PATH" error surfaces to the
    operator as a failed StageOutcome rather than being silently
    swallowed. The wizard's stage 6 calls this BEFORE calling
    ``init()`` so the inline overwrite prompt (F3) can fire with
    the same `claude_desktop_config_path` / `build_snippet` /
    `_resolve_runner` bindings tests already monkeypatch on this
    module — keeping the test surface small.
    """
    config_path = claude_desktop_config_path()
    if config_path is None:
        return None
    runner = _resolve_runner()
    snippet = build_snippet(
        version_pin=__version__,
        env_var_name=env_var_name,
        store_path=store_path.expanduser().resolve(),
        db_url=source_url,
        runner=runner,
    )
    return compare_existing_claude_desktop_entry(snippet=snippet, config_path=config_path)


def init(
    *,
    source_url: str,
    store_path: Path,
    host: HostName,
    env_var_name: str,
    skip_index: bool,
    assume_yes: bool,
) -> InitResult:
    """Run the non-interactive init pipeline.

    Validates preconditions in order; refuses on first failure
    via `InitRefusal`. On success returns an `InitResult` capturing
    what was installed.

    `assume_yes` only matters when the host config already has a
    different `schemabrain` entry — `True` overwrites it (with the
    backup-once contract from config_io), `False` refuses.
    """
    runner = _resolve_runner()
    _validate_source_reachable(source_url)
    if is_postgres_url(source_url):
        _validate_source_read_only(source_url)
    _validate_store(store_path=store_path, skip_index=skip_index)
    config_path = _resolve_host_target(host)

    snippet = build_snippet(
        version_pin=__version__,
        env_var_name=env_var_name,
        store_path=store_path.expanduser().resolve(),
        db_url=source_url,
        runner=runner,
    )

    if host == "manual":
        return InitResult(
            host="manual",
            snippet=snippet,
            state="printed_only",
            skip_index=skip_index,
        )
    if host == "claude-desktop":
        # _resolve_host_target above already raised InitRefusal when
        # config_path was None; this guard is defensive against future
        # refactor that decouples the two.
        if config_path is None:  # pragma: no cover
            raise RuntimeError("claude-desktop host without resolved config_path")
        return _install_to_claude_desktop(
            snippet=snippet,
            config_path=config_path,
            assume_yes=assume_yes,
            skip_index=skip_index,
        )
    # host == "claude-code"
    return _install_to_claude_code_host(snippet=snippet, skip_index=skip_index)


# ----- runner resolution -----------------------------------------------------


def _resolve_runner() -> str:
    """Pick `uvx` if available, else fall back to the installed entrypoint."""
    if shutil.which("uvx") is not None:
        return "uvx"
    fallback = shutil.which("schemabrain")
    if fallback is not None:
        return fallback
    raise InitRefusal(
        GuidedError(
            kind="init_runner_missing",
            message="neither `uvx` nor an installed `schemabrain` is on PATH",
            why="Schema Brain needs a runner the host can invoke to launch the MCP server",
            fix="install uv (`pip install uv`) for the recommended `uvx` path, "
            "or ensure `schemabrain` is on PATH in the env the host will launch from",
            next_step="see docs/setup.md for the canonical install instructions",
        )
    )


# ----- source validation -----------------------------------------------------


def _validate_source_reachable(source_url: str) -> None:
    try:
        engine = create_engine(source_url)
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        finally:
            engine.dispose()
    except Exception as exc:
        msg = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        raise InitRefusal(
            GuidedError(
                kind="init_source_unreachable",
                message="could not reach the source database",
                why=msg,
                fix="verify the connection URL and that the database is reachable",
                next_step="run `schemabrain doctor --source $URL` for a deeper diagnostic",
            )
        ) from exc


def _validate_source_read_only(source_url: str) -> None:
    try:
        engine = create_engine(
            source_url,
            connect_args={"options": "-c default_transaction_read_only=on"},
        )
        try:
            with engine.connect() as conn:
                result = conn.execute(text("SHOW default_transaction_read_only"))
                value = result.scalar()
        finally:
            engine.dispose()
    except Exception as exc:
        msg = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        raise InitRefusal(
            GuidedError(
                kind="init_source_read_only_check_failed",
                message="could not verify read-only session on the source",
                why=msg,
                fix="ensure the URL points at Postgres and the role can SET session params",
                next_step=None,
            )
        ) from exc
    if value != "on":
        raise InitRefusal(
            GuidedError(
                kind="init_source_not_read_only",
                message=f"source session reports default_transaction_read_only={value!r}",
                why="Schema Brain requires a read-only session as a defense against agent-driven writes",
                fix="connect with a role/URL that can enforce session-level read-only, "
                "or grant the role permission to SET it",
                next_step=None,
            )
        )


# ----- store validation ------------------------------------------------------


def _validate_store(*, store_path: Path, skip_index: bool) -> None:
    if not store_path.parent.exists():
        raise InitRefusal(
            GuidedError(
                kind="init_store_path_unwritable",
                message=f"store parent directory does not exist: {store_path.parent}",
                why="Schema Brain needs to create or open the store at the supplied path",
                fix=f"create {store_path.parent}, or re-run with --store-path pointing somewhere writable",
                next_step=None,
            )
        )
    if not store_path.exists():
        # No store yet — caller must pass --skip-index to acknowledge.
        if not skip_index:
            raise InitRefusal(
                GuidedError(
                    kind="init_store_empty",
                    message=f"no store at {store_path} and --skip-index was not passed",
                    why="init needs an indexed store before it can wire the MCP server",
                    fix="run `schemabrain index --url-env $VAR --store-path $PATH` first, "
                    "or pass --skip-index if you'll index later",
                    next_step=None,
                )
            )
        return
    # Defer import — SQLiteStore is heavy.
    from schemabrain.core.store import SchemaVersionMismatchError, SQLiteStore

    try:
        with SQLiteStore(path=store_path) as store:
            entity_count = len(store.list_entities())
    except SchemaVersionMismatchError as exc:
        raise InitRefusal(
            GuidedError(
                kind="init_store_schema_mismatch",
                message=str(exc),
                why="this store file was created by a different schemabrain release",
                fix="delete the store file and re-run `schemabrain index`, "
                "or install the matching schemabrain version",
                next_step=None,
            )
        ) from exc
    if entity_count == 0 and not skip_index:
        raise InitRefusal(
            GuidedError(
                kind="init_store_empty",
                message=f"store at {store_path} has no entities indexed",
                why="init needs at least one entity in the store before it can wire the MCP server",
                fix="run `schemabrain index --url-env $VAR --store-path $PATH` first, "
                "or pass --skip-index if you'll index later",
                next_step=None,
            )
        )


# ----- host target resolution -----------------------------------------------


def _resolve_host_target(host: HostName) -> Path | None:
    if host == "manual":
        return None
    if host == "claude-code":
        return None
    # host == "claude-desktop"
    cfg = claude_desktop_config_path()
    if cfg is None:
        raise InitRefusal(
            GuidedError(
                kind="init_host_unavailable",
                message="Claude Desktop is not supported on this platform",
                why="no official Claude Desktop build exists for this OS (Linux), "
                "or APPDATA is unset on Windows",
                fix="re-run with `--host claude-code` or `--host manual`",
                next_step=None,
            )
        )
    if not cfg.parent.exists():
        raise InitRefusal(
            GuidedError(
                kind="init_host_unavailable",
                message=f"Claude Desktop config directory not found at {cfg.parent}",
                why="Claude Desktop doesn't look installed at the expected path",
                fix="install Claude Desktop, or re-run with `--host claude-code` or `--host manual`",
                next_step=None,
            )
        )
    return cfg


# ----- install: claude-desktop ----------------------------------------------


def _install_to_claude_desktop(
    *,
    snippet: SchemabrainSnippet,
    config_path: Path,
    assume_yes: bool,
    skip_index: bool,
) -> InitResult:
    existing = None
    if config_path.exists():
        try:
            existing = read_mcp_config(config_path)
        except MalformedConfigError as exc:
            # Mirror of the wrap in
            # `compare_existing_claude_desktop_entry` above — without
            # this wrap, a malformed pre-existing config would crash
            # `init` even when the wizard's pre-check had passed
            # (e.g., file corrupted between pre-check and write, or
            # in the `assume_yes` path that bypasses the pre-check).
            raise InitRefusal(
                GuidedError(
                    kind="init_host_config_malformed",
                    message=f"existing host config at {config_path} is not valid JSON",
                    why=f"{exc.decode_error.msg} (line {exc.decode_error.lineno}, "
                    f"col {exc.decode_error.colno})",
                    fix="inspect the file or restore from the .bak sibling, then re-run init",
                    next_step=None,
                )
            ) from exc
    existing_entry = schemabrain_entry_in(existing)
    new_entry = snippet.to_mcp_entry()
    if existing_entry == new_entry:
        # Idempotent no-op.
        return InitResult(
            host="claude-desktop",
            snippet=snippet,
            state="unchanged",
            config_path=config_path,
            backup_made=False,
            skip_index=skip_index,
        )
    if existing_entry is not None and not assume_yes:
        raise InitRefusal(
            GuidedError(
                kind="init_entry_exists",
                message=f"a different schemabrain entry already exists in {config_path.name}",
                why="re-running init would overwrite the existing entry without your confirmation",
                fix="re-run with --yes to overwrite the entry (a .bak sibling is preserved)",
                next_step="or hand-edit the file if you want to merge the two entries",
            )
        )
    merged = merge_schemabrain_entry(existing, snippet)
    backup_made = write_mcp_config_atomic(config_path, merged)
    return InitResult(
        host="claude-desktop",
        snippet=snippet,
        state="written",
        config_path=config_path,
        backup_made=backup_made,
        skip_index=skip_index,
    )


# ----- install: claude-code -------------------------------------------------


def _install_to_claude_code_host(*, snippet: SchemabrainSnippet, skip_index: bool) -> InitResult:
    outcome: ClaudeCodeInstallResult = install_to_claude_code(snippet)
    state: InitState = "shell_out_succeeded" if outcome.succeeded else "shell_out_failed"
    return InitResult(
        host="claude-code",
        snippet=snippet,
        state=state,
        skip_index=skip_index,
        shell_out_command=outcome.command_run,
        shell_out_stderr=outcome.stderr,
    )


__all__ = [
    "ClaudeDesktopEntryComparison",
    "ClaudeDesktopEntryVerdict",
    "InitRefusal",
    "InitResult",
    "InitState",
    "compare_existing_claude_desktop_entry",
    "init",
]
