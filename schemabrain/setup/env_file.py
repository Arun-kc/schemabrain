"""Minimal `.env` reader + KEY=VALUE persister for the wizard's D4 flow.

Two contracts:

1. **Existing exports win.** ``load_env_file_into_environ`` only sets
   keys that are NOT already present in ``os.environ`` — never
   overrides an explicit ``export ANTHROPIC_API_KEY=...`` from the
   user's shell. This matches the conventional ``.env`` semantic
   (shell exports take precedence over file-level defaults) and
   prevents a stale ``.env`` from silently masking a freshly-rotated
   key the user pasted into their shell.

2. **Single-key updates preserve other entries.**
   ``persist_key_to_env_file`` reads any existing ``.env``, replaces
   the line for the named key (or appends if absent), and writes
   back via the same atomic-rename pattern the host-config writer
   uses (sibling tmp + ``os.replace``). Other keys, comments, and
   blank lines pass through unchanged.

The parser is intentionally minimal — supports ``KEY=VALUE``,
``KEY="quoted"``, ``KEY='single-quoted'``, ``#`` comments, blank
lines. Does NOT support variable interpolation (``${OTHER}``),
multi-line values, or ``export KEY=...`` shell prefixes — the
wizard's persistence flow writes one canonical line per key, so
none of those features are load-bearing for the one use case
(persisting ``ANTHROPIC_API_KEY``).

Lives under ``setup/`` because its only callers are the wizard's
stage 0 (persist) and ``cli.main()`` startup (load); it's not a
general-purpose env-file utility.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

# Match `KEY=VALUE` lines with optional surrounding whitespace and
# optional quoting. Capture groups: 1=key, 2=raw value (quotes
# stripped by post-processing).
_KEY_VALUE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def load_env_file_into_environ(path: Path) -> int:
    """Read ``path`` and set any keys NOT already present in ``os.environ``.

    Returns the number of keys loaded (zero if the file does not
    exist, is empty, or every key was already in the environment).
    No-op when the path is missing — silent so the wizard's "create
    a .env" step doesn't force every later run to also have one.

    Existing exports always win: a shell ``export FOO=bar`` is never
    overridden by a ``FOO=baz`` line in the file. This is the same
    contract ``python-dotenv``'s ``load_dotenv(override=False)``
    promises; we don't depend on that package because we only need
    the read + single-key write subset.

    Malformed lines (no ``=``, no valid identifier on the left) are
    silently skipped — the loader is a best-effort convenience, not
    a validator. Operators who want hard parse errors run their
    shell directly against the file.
    """
    if not path.exists():
        return 0
    loaded = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.lstrip()
        if not line or line.startswith("#"):
            continue
        match = _KEY_VALUE_RE.match(raw)
        if match is None:
            continue
        key = match.group(1)
        value = _unquote_value(match.group(2))
        if key in os.environ:
            continue
        os.environ[key] = value
        loaded += 1
    return loaded


def persist_key_to_env_file(
    *,
    key_name: str,
    key_value: str,
    env_path: Path,
) -> None:
    """Write ``key_name=key_value`` to ``env_path``, replacing any prior line.

    Read-modify-write: if ``env_path`` exists, reads it in, replaces
    the line for ``key_name`` (or appends a new line if absent),
    and writes back atomically via tmp+rename. Other keys and
    comments pass through byte-stable.

    The written line is always ``{key_name}={key_value}\\n`` — no
    quoting. ``ANTHROPIC_API_KEY`` values are ASCII-safe
    (``sk-ant-...``), so quoting would add visual noise without
    correctness benefit. If a future caller needs to persist a key
    containing spaces or shell metacharacters, this helper needs
    a quoting upgrade before that callsite lands.

    The file is created with mode ``0o600`` (owner read/write only)
    because it contains secrets — group/world should never see it.
    On read-modify-write, the existing mode is preserved (the
    operator may have hardened it further).
    """
    env_path.parent.mkdir(parents=True, exist_ok=True)
    new_line = f"{key_name}={key_value}"
    existing_lines: list[str] = []
    if env_path.exists():
        existing_lines = env_path.read_text(encoding="utf-8").splitlines()
    replaced = False
    out_lines: list[str] = []
    for raw in existing_lines:
        match = _KEY_VALUE_RE.match(raw)
        if match is not None and match.group(1) == key_name:
            out_lines.append(new_line)
            replaced = True
        else:
            out_lines.append(raw)
    if not replaced:
        out_lines.append(new_line)
    body = "\n".join(out_lines) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{env_path.name}.",
        suffix=".tmp",
        dir=str(env_path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        # Preserve the existing mode if any, else lock to 0o600
        # (owner-only) so the freshly-written secret never lands
        # group/world-readable.
        existing_mode = env_path.stat().st_mode & 0o777 if env_path.exists() else 0o600
        os.chmod(tmp_path, existing_mode)
        os.replace(tmp_path, env_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def is_path_in_gitignore(*, target: Path, gitignore: Path) -> bool:
    """True iff ``target.name`` appears as a literal entry in ``gitignore``.

    Best-effort check used by the persistence consent flow's warning
    path — we want to surface "your .env will be committed if you
    save here" before the operator accidentally checks in a key,
    not after.

    Matches against the file's basename (``.env``) only — does NOT
    interpret glob patterns, negations (``!``), or directory
    anchors. A literal ``.env`` line in ``.gitignore`` returns True;
    a glob like ``*.env`` returns False even though git would honor
    it. This is intentional: we want to warn aggressively when the
    operator's setup is non-standard, even at the cost of a
    spurious warning when their pattern is glob-clever.

    Returns False when ``gitignore`` doesn't exist — the project
    isn't using git ignore at all, so the warning is warranted.
    """
    if not gitignore.exists():
        return False
    target_name = target.name
    for raw in gitignore.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Strip leading slash (anchored pattern) and trailing slash
        # (directory marker) for the basename comparison. Anything
        # else, treat as literal — false negative is the safe
        # default per the docstring above.
        normalized = line.removeprefix("/").removesuffix("/")
        if normalized == target_name:
            return True
    return False


def _unquote_value(raw: str) -> str:
    """Strip a single layer of matching outer quotes from a `.env` value.

    Supports ``"..."`` and ``'...'``. Mismatched, partial, or absent
    quotes return the value unchanged — same posture as
    ``python-dotenv``'s default loader.
    """
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'"):
        return raw[1:-1]
    return raw


__all__ = [
    "is_path_in_gitignore",
    "load_env_file_into_environ",
    "persist_key_to_env_file",
]
