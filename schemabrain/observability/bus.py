"""`JsonlEventBus` — append-only event store on a JSONL file with
size-based rotation.

Contract:

  - One JSON line per `Event`. UTF-8.
  - Append-only. Single writer (the `serve` process) per file.
  - Rotation: when the active file exceeds `max_bytes`, it is renamed
    to `<path>.1`, replacing any prior `.1`. A fresh active file
    starts on the next emit.
  - File permissions: 0700 on the directory, 0600 on the file.
  - Failure handling: any IO error in `emit()` is caught, logged
    once per error-kind to stderr, and the event is dropped. The
    caller (tool execution path) never sees the error.

The interface `EventBus` is small so a `NullEventBus` can be passed
when emission is disabled (`--no-events` on serve, or tests that
don't want a side effect).
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path
from typing import Final, Protocol

from schemabrain.observability.event import Event

DEFAULT_MAX_BYTES: Final[int] = 10 * 1024 * 1024  # 10 MiB
_DIR_MODE: Final[int] = 0o700
_FILE_MODE: Final[int] = 0o600


class EventBus(Protocol):
    """Anything that can absorb an `Event` and not raise."""

    def emit(self, event: Event) -> None: ...

    def close(self) -> None: ...


class NullEventBus:
    """No-op bus for tests + `--no-events` runs."""

    def emit(self, event: Event) -> None:
        return

    def close(self) -> None:
        return


class JsonlEventBus:
    """Append-only JSONL bus with size-based rotation."""

    def __init__(
        self,
        path: Path | str,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError(f"max_bytes must be positive, got {max_bytes}")
        self._path = Path(path).expanduser()
        self._max_bytes = max_bytes
        self._error_kinds_logged: set[str] = set()
        self._ensure_dir()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    def emit(self, event: Event) -> None:
        try:
            line = event.to_json_line().encode("utf-8")
            self._rotate_if_needed(extra=len(line))
            self._append(line)
        except OSError as exc:
            self._log_once(type(exc).__name__, exc)
        except Exception as exc:  # pragma: no cover — defence in depth
            self._log_once(type(exc).__name__, exc)

    def close(self) -> None:
        # Stateless writer — every emit opens + closes its own fd.
        return

    def _ensure_dir(self) -> None:
        parent = self._path.parent
        parent.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
        # mkdir(exist_ok=True) silently ACCEPTS a pre-existing dir
        # regardless of its mode. Re-chmod here so the intended 0700
        # posture holds even when the dir was created by an earlier
        # version, another tool, or a system package manager that
        # used a looser mode.
        # If we don't own the directory we can't tighten it. Better
        # to proceed than to refuse — the file mode of 0600 is the
        # real protection on the data itself.
        with contextlib.suppress(OSError):  # pragma: no cover
            os.chmod(parent, _DIR_MODE)

    def _append(self, line: bytes) -> None:
        # Open per-emit so multiple processes don't share an offset and
        # the kernel handles O_APPEND atomicity for lines < PIPE_BUF.
        fd = os.open(
            self._path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            _FILE_MODE,
        )
        try:
            os.write(fd, line)
        finally:
            os.close(fd)

    def _rotate_if_needed(self, *, extra: int) -> None:
        try:
            current = self._path.stat().st_size
        except FileNotFoundError:
            return
        if current + extra <= self._max_bytes:
            return
        rotated = self._path.with_name(self._path.name + ".1")
        # os.replace is atomic on POSIX and overwrites the prior .1
        os.replace(self._path, rotated)

    def _log_once(self, kind: str, exc: BaseException) -> None:
        if kind in self._error_kinds_logged:
            return
        self._error_kinds_logged.add(kind)
        print(
            f"schemabrain observability: dropping event ({kind}: {exc})",
            file=sys.stderr,
        )
